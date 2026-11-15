"""Real API/Library/CC0 ingestion/index guards; only model inference is substituted.

No live library, ready index or model cache is accessed. These integration tests
check transport and metadata scope, not real-model ranking quality.
"""

from __future__ import annotations

import re
from collections.abc import Generator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from book_rag import answers
from book_rag import book_discovery as discovery
from book_rag.api import create_app
from book_rag.library import Library


class NoGeneration:
    calls = 0

    def generate(
        self, messages: list[dict[str, str]], limits: Any = None, *, response_schema: Any = None
    ) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError("Metadata discovery must not invoke answer generation")


class FixtureTokenizer:
    def __call__(self, text: str, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["truncation"] is False
        return {"offset_mapping": [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]}

    def encode(self, text: str, **kwargs: Any) -> list[int]:
        assert kwargs["truncation"] is False
        return [1] * (len(text.split()) + (2 if kwargs.get("add_special_tokens") else 0))

    def num_special_tokens_to_add(self, **kwargs: Any) -> int:
        return 2


class FixtureEncoder:
    """A small deterministic boundary double, never a mocked API/search result."""

    max_seq_length = 32
    tokenizer = FixtureTokenizer()

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str], **kwargs: Any) -> np.ndarray:
        assert kwargs["normalize_embeddings"] and kwargs["convert_to_numpy"]
        self.calls.append(texts)
        vectors = np.zeros((len(texts), discovery.DIMENSION), dtype=np.float32)
        for row, text in enumerate(texts):
            topic = 0 if "justice" in text.casefold() else 1 if "chemistry" in text.casefold() else 2
            vectors[row, topic] = 1
        return vectors


def add_cc0(library: Library, name: str, language: str, subject: str) -> str:
    source = library.data_dir / (name + ".txt")
    source.write_text(
        f"An original CC0 test story about {name}. "
        "The silver lantern stays beside the blue jar. These sentences were authored for this test."
    )
    result = library.import_book(
        source,
        {
            "id": name,
            "title": name,
            "author": "Original CC0 test contributors",
            "edition": "CC0 API integration fixture1",
            "language": language,
            "work_id": "fixture-work-" + name,
            "source_url": "https://example.org/cc0/" + name,
            "rights": "CC0-1.0",
            "rights_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "subjects": [subject],
            "coverage": "full",
            "work_type": "book_or_text",
            "work_identity_status": "curated",
            "illustrations_included": False,
            "coverage_scope": "text_only_without_illustrations",
        },
    )
    assert result["status"] == "indexed"
    return name


@pytest.fixture
def api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[TestClient, Library, FixtureEncoder, NoGeneration]]:
    generator = NoGeneration()
    app = create_app(tmp_path / "library", answer_generator=generator)
    library: Library = app.state.library
    add_cc0(library, "lantern-council", "en", "justice")
    add_cc0(library, "water-laboratory", "fr", "chemistry")
    encoder = FixtureEncoder()
    monkeypatch.setattr(discovery, "cached_model_path", lambda library: tmp_path / "model-boundary-double")
    monkeypatch.setattr(discovery, "model_identity", lambda path: {"model": "fixture-only", "revision": "test-pinned"})
    monkeypatch.setattr(discovery, "_encoder", lambda path, device: encoder)

    def prohibited(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Metadata route must not call ask, research, or passage search")

    monkeypatch.setattr(answers, "ask", prohibited)
    monkeypatch.setattr(library, "search", prohibited)
    monkeypatch.setattr(library, "research", prohibited)
    with TestClient(app) as client:
        yield client, library, encoder, generator
    assert generator.calls == 0


def test_actual_discovery_api_returns_metadata_cards_and_reachable_cc0_reader(
    api: tuple[TestClient, Library, FixtureEncoder, NoGeneration],
) -> None:
    client, library, encoder, generator = api
    receipt = discovery.build_book_discovery(library, batch_size=1)
    before = library.stats()
    response = client.get("/api/discover/books", params={"q": "justice", "limit": 1})
    assert response.status_code == 200
    result = response.json()
    assert result["kind"] == "metadata_semantic" and result["status"] == "ready"
    assert result["coverage"] == {"indexed_editions": 2, "searchable_editions": 2}
    assert result["warnings"] == result["warning_codes"] == []
    assert len(result["books"]) == 1
    card = result["books"][0]
    assert card["id"] == "lantern-council" and card["rights"] == "CC0-1.0"
    assert card["metadata_evidence"]["subjects"] == ["justice"]
    assert card["matched_fields"] == ["title", "author", "subjects"]
    assert card["sections"] > 0 and card["passages"] > 0
    assert card["coverage"] == "full" and card["illustrations_included"] is False
    assert card["coverage_scope"] == "text_only_without_illustrations"
    assert card["reader_url"] == "/?book=lantern-council"
    assert client.get(card["reader_url"]).status_code == 200
    opened = client.get("/api/books/" + card["id"]).json()
    assert opened["rights"] == "CC0-1.0"
    assert any("silver lantern" in section["text"] for section in opened["sections"])
    assert not {"claims", "sources", "answer", "citations"} & result.keys()
    assert "text" not in card and "quote" not in card and "citation" not in card
    assert "silver lantern" not in response.text
    assert not receipt["passage_semantic_coverage_claimed"]
    assert encoder.calls and generator.calls == 0 and library.stats() == before


@pytest.mark.parametrize("query", ["", " ", "\t\n"])
def test_blank_description_is400_before_encoder_or_generation(
    api: tuple[TestClient, Library, FixtureEncoder, NoGeneration],
    query: str,
) -> None:
    client, _, encoder, generator = api
    response = client.get("/api/discover/books", params={"q": query})
    assert response.status_code == 400
    assert response.json()["detail"]
    assert encoder.calls == [] and generator.calls == 0


def test_missing_index_is_unavailable_with_clear_catalog_fallback_warning(
    api: tuple[TestClient, Library, FixtureEncoder, NoGeneration],
) -> None:
    client, _, encoder, _ = api
    response = client.get("/api/discover/books", params={"q": "justice"})
    assert response.status_code == 200
    result = response.json()
    assert result["kind"] == "metadata_semantic" and result["status"] == "unavailable"
    assert not result["books"] and result["coverage"] == {"indexed_editions": 0, "searchable_editions": 2}
    assert result["warning_codes"] == ["metadata_discovery_unavailable_or_stale"]
    assert "недоступен" in result["warnings"][0] and "Каталог" in result["warnings"][0]
    assert "metadata_" not in result["warnings"][0] and encoder.calls == []


def test_missing_description_is400_without_touching_encoder(
    api: tuple[TestClient, Library, FixtureEncoder, NoGeneration],
) -> None:
    client, _, encoder, generator = api
    response = client.get("/api/discover/books")
    assert response.status_code == 400
    assert response.json()["detail"]
    assert not encoder.calls and generator.calls == 0


def test_optional_backend_import_failure_returns_clear_unavailable_response(
    api: tuple[TestClient, Library, FixtureEncoder, NoGeneration],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    original = builtins.__import__

    def unavailable(
        name: str, globals: Any = None, locals: Any = None, fromlist: tuple[str, ...] = (), level: int = 0
    ) -> Any:
        if name == "book_rag.book_discovery" and "discover_books" in fromlist:
            raise ImportError("Explicit optional-backend boundary failure")
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    client, _, encoder, _ = api
    response = client.get("/api/discover/books", params={"q": "justice"})
    assert response.status_code == 200
    result = response.json()
    assert result["kind"] == "metadata_semantic" and result["status"] == "unavailable" and not result["books"]
    assert "недоступен" in result["warnings"][0] and "каталог" in result["warnings"][0]
    assert "ImportError" not in response.text and not encoder.calls


def test_language_and_limit_filter_metadata_books_without_passage_scope(
    api: tuple[TestClient, Library, FixtureEncoder, NoGeneration],
) -> None:
    client, library, _, _ = api
    discovery.build_book_discovery(library)
    french = client.get("/api/discover/books", params={"q": "chemistry", "language": "fr", "limit": 1}).json()
    assert [book["id"] for book in french["books"]] == ["water-laboratory"]
    assert all(book["language"] == "fr" for book in french["books"])
    russian = client.get("/api/discover/books", params={"q": "chemistry", "language": "ru"}).json()
    assert russian["status"] == "ready" and russian["books"] == []
    assert russian["coverage"] == {"indexed_editions": 2, "searchable_editions": 2}
    for limit in [0, 49]:
        assert client.get("/api/discover/books", params={"q": "chemistry", "limit": limit}).status_code == 422


def test_partial_addition_discloses_coverage_in_russian_and_covered_update_fails_closed(
    api: tuple[TestClient, Library, FixtureEncoder, NoGeneration],
) -> None:
    client, library, _, _ = api
    discovery.build_book_discovery(library)
    add_cc0(library, "new-not-indexed-metadata", "en", "history")
    partial = client.get("/api/discover/books", params={"q": "justice"}).json()
    assert partial["status"] == "ready" and len(partial["books"]) == 2
    assert partial["coverage"] == {"indexed_editions": 2, "searchable_editions": 3}
    assert partial["warning_codes"] == ["metadata_index_partial_coverage"]
    assert "не все издания" in partial["warnings"][0] and "покрытие" in partial["warnings"][0]
    with library.connect() as db:
        db.execute("UPDATE books SET title=? WHERE id=?", ("Changed title", "lantern-council"))
    stale = client.get("/api/discover/books", params={"q": "justice"}).json()
    assert stale["status"] == "stale" and stale["books"] == []
    assert stale["coverage"]["indexed_editions"] == 0
    assert "metadata_discovery_unavailable_or_stale" in stale["warning_codes"]
    assert any("Каталог" in warning for warning in stale["warnings"])


def test_long_description_has_clear_warning_instead_of_silent_truncation(
    api: tuple[TestClient, Library, FixtureEncoder, NoGeneration],
) -> None:
    client, library, _, _ = api
    discovery.build_book_discovery(library)
    response = client.get("/api/discover/books", params={"q": "word " * 100})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "query_limit" and not result["books"]
    assert result["warning_codes"] == ["metadata_query_exceeds_three_token_windows"]
    assert "Описание слишком длинное" in result["warnings"][0]
    assert result["query_token_info"]["token_truncated"]
