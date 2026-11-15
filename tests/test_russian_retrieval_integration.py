"""Public search/Ask integration without invoking an actual model."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from test_answers import Fake
from test_russian_retrieval import candidate, corpus

from book_rag.api import create_app

QUERY = "садовниками кораблями"
TEXT = "Путник беседовал с садовником о кораблях."


def test_search_recovers_empty_scope_with_exact_quote_and_explicit_metadata(tmp_path: Path) -> None:
    library = corpus(tmp_path, [TEXT])
    expected = candidate(library, QUERY)["results"]
    with TestClient(create_app(library.data_dir)) as client:
        response = client.get("/api/search", params={"q": QUERY, "mode": "lexical", "book_id": "cc0-inflection"})
        assert response.status_code == 200
        result = response.json()
        assert result["mode"] == "lexical" and result["results"] == expected
        assert result["lexical_fallback"]["method"] == "russian-snowball-prefix-v1"
        assert result["warnings"] and result["results"][0]["text"] == TEXT
        source = result["results"][0]
        book = client.get("/api/books/cc0-inflection").json()
        section = next(section for section in book["sections"] if section["id"] == source["section_id"])
        assert section["text"][source["start"] : source["end"]] == TEXT
        assert source["verified"] and client.get(source["reader_url"]).status_code == 200
        for parameters in (
            {"q": QUERY, "mode": "baseline", "book_id": "cc0-inflection"},
            {"q": QUERY, "mode": "lexical"},
            {"q": f"«{QUERY}»", "mode": "lexical", "book_id": "cc0-inflection"},
        ):
            unchanged = client.get("/api/search", params=parameters).json()
            assert unchanged["results"] == [] and "lexical_fallback" not in unchanged
        existing = client.get(
            "/api/search", params={"q": "садовником", "mode": "lexical", "book_id": "cc0-inflection"}
        ).json()
        assert existing["results"] and "lexical_fallback" not in existing


@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
def test_ask_soft_question_quotes_reach_morphology_but_model_sees_original(tmp_path: Path, mode: str) -> None:
    library = corpus(tmp_path, [TEXT])
    question = f"Что сказано о «{QUERY}»?"
    fake = Fake({"answerable": True, "claims": [{"text": TEXT, "source_ids": ["S1"]}]})
    with (
        patch("book_rag.semantic._model", side_effect=AssertionError("No real encoder may be used")),
        TestClient(create_app(library.data_dir, answer_generator=fake)) as client,
    ):
        result = client.post(
            "/api/ask", json={"query": question, "book_ids": ["cc0-inflection"], "language": "ru", "mode": mode}
        ).json()
        assert result["status"] == "answered", result
        assert result["query"] == question and fake.calls[0]["question"] == question
        assert result["retrieval"]["actual_modes"] == ["lexical"]
        source = result["sources"][0]
        assert source["verified"] and source["text"] == TEXT
        assert result["claims"][0]["supporting_quotes"][0]["quote"] in TEXT
        assert client.get(source["reader_url"]).status_code == 200
        assert (
            client.get("/api/search", params={"q": question, "mode": "lexical", "book_id": "cc0-inflection"}).json()[
                "results"
            ]
            == []
        )


def test_expanded_corrupt_citation_remains_suppressed_and_warning_retained(tmp_path: Path) -> None:
    library = corpus(tmp_path, [TEXT])
    with library.connect() as connection:
        connection.execute("UPDATE passages SET sha256='corrupt-fixture'")
    result = library.search(QUERY, "lexical", "cc0-inflection")
    assert result["results"] == [] and "Corrupt citation suppressed" in " ".join(result["warnings"])
    assert result["lexical_fallback"]["reason"] == "no_supported_stem_matches"


def test_expansion_cannot_attribute_foreign_sections_to_selected_book(tmp_path: Path) -> None:
    library = corpus(tmp_path, [TEXT])
    other = tmp_path / "other.txt"
    other.write_text("Посторонний оригинальный текст CC0 описывает дождь.")
    library.import_book(
        other,
        {"id": "other", "title": "Другой источник", "author": "Other CC0 author", "language": "ru", "rights": "CC0"},
    )
    with library.connect() as connection:
        connection.execute("UPDATE passages SET book_id='other' WHERE book_id='cc0-inflection'")
    assert library.search(QUERY, "lexical", "other")["results"] == []
