"""Core dispatch boundaries; actual overlay proof/native tests live separately."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

from book_rag import semantic
from book_rag.library import Library


@pytest.fixture
def legacy_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Library:
    library = Library(tmp_path / "library")
    for identity, language, text in [
        ("ru-covered", "ru", "Needle найден в русском оригинале 🦉."),
        ("en-outside", "en", "Needle in the original English edition."),
    ]:
        path = tmp_path / (identity + ".txt")
        path.write_text(text)
        library.import_book(
            path,
            {
                "id": identity,
                "language": language,
                "title": identity,
                "author": "Original CC0 fixture author",
                "rights": "CC0",
            },
        )
    with library.connect() as connection:
        ids = [row[0] for row in connection.execute("SELECT id FROM passages ORDER BY id")]
    assert len(ids) == 2
    folder = library.data_dir / "vectors/legacy"
    folder.mkdir(parents=True)
    np.save(folder / "embeddings.npy", np.array([[1.0, 0.0], [0.9, np.sqrt(0.19)]], dtype="float32"))
    np.save(folder / "ids.npy", np.array(ids, dtype="int64"))
    (folder.parent / "active.json").write_text(
        json.dumps(
            {
                "model": semantic.DEFAULT_MODEL,
                "generation": "legacy",
                "corpus_fingerprint": semantic.corpus_fingerprint(library),
            }
        )
    )

    class QueryModel:
        def encode(self, texts: list[str], **kwargs: Any) -> np.ndarray:
            return np.array([[1.0, 0.0]], dtype="float32")

    monkeypatch.setattr(semantic, "_model", lambda *args: QueryModel())
    monkeypatch.delenv("BOOKRAG_SEMANTIC_OVERLAY", raising=False)
    return library


def overlay_spy(monkeypatch: pytest.MonkeyPatch, callback: Any) -> None:
    # Test the dispatch boundary independently of optional native dependencies.
    module = ModuleType("book_rag.semantic_overlay")
    vars(module)["overlay_search"] = callback
    monkeypatch.setitem(sys.modules, "book_rag.semantic_overlay", module)


def without_timing(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key != "elapsed_ms"}


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
@pytest.mark.parametrize("language", [None, "en"])
def test_unscoped_global_order_scores_citations_and_metadata_unchanged(
    legacy_library: Library,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    language: str | None,
) -> None:
    library = legacy_library
    control = library.search("needle", mode, language=language)
    active = library.data_dir / "vectors/active.json"
    before = active.read_bytes()
    overlay_spy(monkeypatch, lambda *args: pytest.fail("Global queries must not consult an overlay"))
    monkeypatch.setenv("BOOKRAG_SEMANTIC_OVERLAY", "/not/opened/global.json")
    result = library.search("needle", mode, language=language)
    assert without_timing(result) == without_timing(control)
    assert active.read_bytes() == before


@pytest.mark.parametrize("mode", ["lexical", "baseline"])
def test_word_search_never_uses_overlay(
    legacy_library: Library,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    overlay_spy(monkeypatch, lambda *args: pytest.fail("Lexical search cannot start semantic work"))
    monkeypatch.setenv("BOOKRAG_SEMANTIC_OVERLAY", "/not/opened/lexical.json")
    result = legacy_library.search("needle", mode, book_id="ru-covered", language="ru")
    assert result["mode"] == mode and result["results"]
    assert all(row["verified"] and row["book_id"] == "ru-covered" for row in result["results"])


def test_missing_optional_configuration_preserves_book_scoped_primary_index(
    legacy_library: Library,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay_spy(monkeypatch, lambda *args: pytest.fail("No configuration means no overlay work"))
    result = legacy_library.search("needle", "semantic", book_id="ru-covered")
    assert result["mode"] == "semantic" and len(result["results"]) == 1
    assert "semantic_coverage" not in result


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_noncovered_scope_keeps_primary_response_when_overlay_declines(
    legacy_library: Library,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    control = legacy_library.search("needle", mode, book_id="en-outside")
    called = []

    def decline(*args: Any) -> None:
        called.append(args)

    overlay_spy(monkeypatch, decline)
    monkeypatch.setenv("BOOKRAG_SEMANTIC_OVERLAY", "/configured/overlay.json")
    result = legacy_library.search("needle", mode, book_id="en-outside")
    assert len(called) == 1 and called[0][3:5] == ("en-outside", None)
    assert without_timing(result) == without_timing(control)


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
@pytest.mark.parametrize("book_id,language", [("ru-covered", None), (None, "ru")])
def test_overlay_scope_dispatch_precedes_missing_primary_index(
    legacy_library: Library,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    book_id: str | None,
    language: str | None,
) -> None:
    library = legacy_library
    (library.data_dir / "vectors/active.json").unlink()
    source = library.search("needle", "lexical", "ru-covered")["results"][0]
    calls = []

    def serve(*args: Any) -> dict[str, Any]:
        calls.append(args)
        return {
            "query": args[1],
            "mode": args[2],
            "results": [source],
            "elapsed_ms": 1,
            "warnings": [],
            "semantic_coverage": {
                "indexed_books": 1,
                "searchable_books": 2,
                "active": True,
                "requested_book_covered": True if args[3] else None,
                "scope": "ru21-scoped-overlay",
                "scope_language": "ru",
                "scope_searchable_books": 1,
            },
        }

    overlay_spy(monkeypatch, serve)
    monkeypatch.setenv("BOOKRAG_SEMANTIC_OVERLAY", "/configured/overlay.json")
    monkeypatch.setattr(semantic, "_model", lambda *args: pytest.fail("Primary model must not be loaded"))
    result = library.search("needle", mode, book_id=book_id, language=language)
    assert len(calls) == 1 and calls[0] == (library, "needle", mode, book_id, language, 10)
    assert result["mode"] == mode and result["results"][0]["text"] == source["text"]
    assert result["semantic_coverage"]["scope"] == "ru21-scoped-overlay"


def test_overlay_boundary_failure_fails_closed_without_private_error_text(
    legacy_library: Library,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: Any) -> None:
        raise RuntimeError("PRIVATE native/runtime/source details")

    overlay_spy(monkeypatch, unavailable)
    monkeypatch.setenv("BOOKRAG_SEMANTIC_OVERLAY", "/configured/overlay.json")
    result = legacy_library.search("needle", "hybrid", book_id="ru-covered")
    assert result["mode"] == "lexical" and result["results"]
    assert all(source["verified"] for source in result["results"])
    assert "PRIVATE" not in json.dumps(result)
    assert result["warnings"] and "semantic_coverage" not in result
