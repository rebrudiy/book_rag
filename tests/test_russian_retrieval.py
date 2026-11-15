"""Original CC0 inflection/collision/negative cases, no model or network."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from html import escape
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from book_rag.library import Library
from book_rag.russian_retrieval import (
    CANDIDATE_LIMIT,
    MAX_PREFIX_VARIANTS,
    MAX_TERMS,
    prepare_russian_fallback,
    scoped_russian_fallback,
    stem,
)


def corpus(tmp_path: Path, sections: list[str], *, language: str = "ru") -> Library:
    library = Library(tmp_path / "library")
    source = tmp_path / "cc0-original.html"
    source.write_text(
        "<h1>Оригинальный опыт CC0</h1>"
        + "".join(f"<h2>Сцена {index}</h2><p>{escape(text)}</p>" for index, text in enumerate(sections)),
        encoding="utf-8",
    )
    assert (
        library.import_book(
            source,
            {
                "id": "cc0-inflection",
                "title": "Оригинальный опыт CC0",
                "author": "Book Atlas test fixture",
                "edition": "Original CC0 fixture, version1",
                "language": language,
                "rights": "CC0 1.0",
                "source_url": "https://example.org/cc0/inflection-fixture",
            },
        )["status"]
        == "indexed"
    )
    return library


def candidate(library: Library, query: str, **kwargs: Any) -> dict[str, Any]:
    with patch("book_rag.russian_retrieval.scoped_russian_fallback", return_value={"used": False}):
        normal = library.search(query, "lexical", "cc0-inflection", limit=10)
    assert not normal["results"], "This case must exercise empty-result fallback, not replace an existing hit"
    return scoped_russian_fallback(library, query, "cc0-inflection", prior_results=normal["results"], **kwargs)


@pytest.mark.parametrize(
    ("text", "query"),
    [
        ("Путник беседовал с садовником о кораблях.", "садовниками кораблями"),
        ("Кузнец говорил с ремесленником о переправе.", "кузнецами ремесленниками"),
        ("Лётчики осматривали ёлки возле пристани.", "летчиками елками"),
        ("Читатель любовался фонарём и дорожками сада 🦉.", "фонарями дорожках"),
    ],
)
def test_inflections_recover_only_verified_canonical_source(tmp_path: Path, text: str, query: str) -> None:
    library = corpus(tmp_path, [text, "Посторонний текст описывает облака и дождь."])
    result = candidate(library, query)
    assert result["used"] and len(result["results"]) == 1
    hit = result["results"][0]
    assert hit["verified"] and hit["text"] == text and hit["stem_coverage"] == 1
    assert hit["book_id"] == "cc0-inflection" and hit["edition"] == "Original CC0 fixture, version1"
    canonical = library.passage(hit["id"])
    assert canonical and canonical["text"] == hit["text"] and canonical["citation"] == hit["citation"]
    with library.connect() as connection:
        section = connection.execute("SELECT text FROM sections WHERE id=?", (hit["section_id"],)).fetchone()[0]
    assert section[hit["start"] : hit["end"]] == hit["text"]


def test_prefix_collision_is_not_counted_as_equal_stem(tmp_path: Path) -> None:
    library = corpus(tmp_path, ["Путник размышлял о мире.", "Миротворцы обсуждали далёкие поселения."])
    result = candidate(library, "миром")
    assert result["candidates"] == 2 and len(result["results"]) == 1
    assert result["results"][0]["text"] == "Путник размышлял о мире."


@pytest.mark.parametrize(
    "query",
    [
        "кораблями термоядерными реакторами",
        "квантовыми марсианскими двигателями",
        "кораблями пристанями 2024",
        "кораблями пристанями не",
        "могила",
        "пила",
    ],
)
def test_unrelated_numeric_and_negation_constraints_remain_empty(tmp_path: Path, query: str) -> None:
    library = corpus(
        tmp_path,
        [
            "Корабли подходили к пристаням в 1820 году.",
            "Путник не описывал облака и дождь.",
            "Он мог прочесть карту. Затем он пил воду.",
        ],
    )
    result = candidate(library, query)
    assert result["used"] and result["results"] == []


class NoDatabase:
    def connect(self):
        pytest.fail("Ineligible requests must not access a database")


@pytest.mark.parametrize(
    "query", ['"садовниками кораблями"', "«садовниками кораблями»", 'садовниками "кораблями', "'садовниками' кораблями"]
)
def test_quotes_including_unbalanced_quotes_stay_strict(query: str) -> None:
    result = scoped_russian_fallback(NoDatabase(), query, "book", prior_results=[])
    assert not result["used"] and result["reason"] == "quoted_query_unchanged"


@pytest.mark.parametrize(
    "extra",
    [{"mode": "baseline"}, {"book_id": None}, {"book_id": ""}, {"language": "en"}, {"prior_results": [{"id": 1}]}],
)
def test_baseline_global_nonempty_and_other_language_are_unchanged(extra: dict[str, Any]) -> None:
    args: dict[str, Any] = {"book_id": "book", "prior_results": []} | extra
    assert not scoped_russian_fallback(NoDatabase(), "садовниками кораблями", **args)["used"]


def test_other_book_language_or_unavailable_book_is_not_expanded(tmp_path: Path) -> None:
    library = corpus(tmp_path, ["Садовник увидел корабли."], language="en")
    assert candidate(library, "садовниками кораблями")["reason"] == "book_not_indexed_russian"
    assert not scoped_russian_fallback(library, "садовниками", "missing", prior_results=[])["used"]


def test_bounded_syntax_and_constraints_not_silently_truncated() -> None:
    plan = prepare_russian_fallback("садовниками кораблями OR NEAR звездолётами")
    assert plan and plan.expression.count("*") <= MAX_TERMS * MAX_PREFIX_VARIANTS
    assert '"садовник"*' in plan.expression and '"or"' in plan.expression
    assert CANDIDATE_LIMIT == 200
    assert prepare_russian_fallback(" ".join(f"кораблями{index}" for index in range(MAX_TERMS + 1))) is None
    assert prepare_russian_fallback("не без 2024") is None


def test_thread_local_stemmers_are_safe_with_shared_bounded_cache() -> None:
    words = ["садовниками", "кораблями", "лётчиками", "ёлками"] * 50
    expected = [stem(word) for word in words]
    stem.cache_clear()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(stem, words)) == expected
