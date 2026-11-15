"""Generic explicit-book route must preserve global, legacy and RU21 dispatch."""

from __future__ import annotations

import json
import sys
from types import ModuleType
from typing import Any

import pytest
from test_semantic_overlay_integration import legacy_library as legacy_library
from test_semantic_overlay_integration import without_timing

from book_rag import semantic
from book_rag.library import Library


def route(monkeypatch: pytest.MonkeyPatch, name: str, function: str, callback: Any) -> None:
    module = ModuleType(name)
    vars(module)[function] = callback
    monkeypatch.setitem(sys.modules, name, module)


def generic(monkeypatch: pytest.MonkeyPatch, callback: Any) -> None:
    route(monkeypatch, "book_rag.semantic_batch_overlay", "batch_overlay_search", callback)


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
@pytest.mark.parametrize("language", [None, "en", "ru"])
def test_registry_does_not_change_global_or_language_only_results(
    legacy_library: Library,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    language: str | None,
) -> None:
    control = legacy_library.search("needle", mode, language=language)
    generic(monkeypatch, lambda *args: pytest.fail("No registry work for global or language-only requests"))
    monkeypatch.setenv("BOOKRAG_SEMANTIC_BATCH_REGISTRY", "/not/read/global-registry.json")
    result = legacy_library.search("needle", mode, language=language)
    assert without_timing(result) == without_timing(control)


@pytest.mark.parametrize("mode", ["baseline", "lexical"])
def test_registry_never_enters_word_search(
    legacy_library: Library,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    generic(monkeypatch, lambda *args: pytest.fail("Word search must not enter registry"))
    monkeypatch.setenv("BOOKRAG_SEMANTIC_BATCH_REGISTRY", "/not/read/word-registry.json")
    result = legacy_library.search("needle", mode, "en-outside")
    assert result["mode"] == mode and result["results"]


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_uncovered_book_declines_to_unchanged_primary_response(
    legacy_library: Library,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    control = legacy_library.search("needle", mode, "en-outside")
    called = []

    def decline(*args: Any) -> None:
        called.append(args)

    generic(monkeypatch, decline)
    monkeypatch.setenv("BOOKRAG_SEMANTIC_BATCH_REGISTRY", "/configured/registry.json")
    result = legacy_library.search("needle", mode, "en-outside")
    assert len(called) == 1
    assert without_timing(result) == without_timing(control)


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_registered_book_dispatches_before_primary_index_availability(
    legacy_library: Library,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    source = legacy_library.search("needle", "lexical", "en-outside")["results"][0]
    (legacy_library.data_dir / "vectors/active.json").unlink()
    calls = []

    def serve(*args: Any) -> dict[str, Any]:
        calls.append(args)
        return {
            "query": args[1],
            "mode": args[2],
            "results": [source],
            "warnings": [],
            "elapsed_ms": 1,
            "semantic_coverage": {
                "scope": "book-batch-overlay",
                "scope_book_id": "en-outside",
                "indexed_books": 1,
                "searchable_books": 2,
                "active": True,
                "requested_book_covered": True,
            },
        }

    generic(monkeypatch, serve)
    monkeypatch.setenv("BOOKRAG_SEMANTIC_BATCH_REGISTRY", "/configured/registry.json")
    monkeypatch.setattr(semantic, "_model", lambda *args: pytest.fail("Primary index must not load a model"))
    result = legacy_library.search("needle", mode, "en-outside", "en", 5)
    assert len(calls) == 1 and calls[0] == (legacy_library, "needle", mode, "en-outside", "en", 5)
    assert result["mode"] == mode and result["results"][0]["verified"]
    assert result["semantic_coverage"]["scope"] == "book-batch-overlay"


@pytest.mark.parametrize("book,language", [("ru-covered", None), (None, "ru")])
def test_existing_ru21_precedence_is_preserved(
    legacy_library: Library,
    monkeypatch: pytest.MonkeyPatch,
    book: str | None,
    language: str | None,
) -> None:
    source = legacy_library.search("needle", "lexical", "ru-covered")["results"][0]
    response = {
        "query": "needle",
        "mode": "semantic",
        "results": [source],
        "warnings": [],
        "elapsed_ms": 1,
        "semantic_coverage": {"scope": "ru21-scoped-overlay", "indexed_books": 1, "active": True},
    }
    route(monkeypatch, "book_rag.semantic_overlay", "overlay_search", lambda *args: response)
    generic(monkeypatch, lambda *args: pytest.fail("RU21 must retain its existing precedence"))
    monkeypatch.setenv("BOOKRAG_SEMANTIC_OVERLAY", "/configured/ru21.json")
    monkeypatch.setenv("BOOKRAG_SEMANTIC_BATCH_REGISTRY", "/configured/registry.json")
    result = legacy_library.search("needle", "semantic", book, language)
    assert result["semantic_coverage"]["scope"] == "ru21-scoped-overlay"


def test_generic_boundary_failure_keeps_verified_lexical_and_hides_private_error(
    legacy_library: Library,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failure(*args: Any) -> None:
        raise RuntimeError("private source/runtime details")

    generic(monkeypatch, failure)
    monkeypatch.setenv("BOOKRAG_SEMANTIC_BATCH_REGISTRY", "/configured/registry.json")
    result = legacy_library.search("needle", "hybrid", "en-outside")
    assert result["mode"] == "lexical" and result["warnings"]
    assert all(row["verified"] for row in result["results"])
    assert "private source" not in json.dumps(result)
