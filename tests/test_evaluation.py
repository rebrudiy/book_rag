"""Evaluation checks must catch corrupted evidence, not echo a verified flag."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from book_rag.evaluation import evaluate, load_dataset, verify_result

BOOK: dict[str, Any] = {
    "id": "book-1",
    "work_id": "work-1",
    "title": "Книга",
    "author": "Автор",
    "edition": "Издание 1900",
    "sections": [{"id": "chapter-1", "title": "Глава", "text": "До. Точная цитата. После."}],
}
RESULT: dict[str, Any] = {
    "id": "passage-1",
    "book_id": "book-1",
    "title": "Книга",
    "author": "Автор",
    "edition": "Издание 1900",
    "section_id": "chapter-1",
    "section_title": "Глава",
    "text": "Точная цитата.",
    "start": 4,
    "end": 18,
    "citation": "Автор. Книга. Издание 1900. Глава.",
    "reader_url": "/?book=book-1&section=chapter-1&passage=passage-1",
    "verified": True,
}


def test_exact_unicode_quote_and_metadata() -> None:
    assert verify_result(RESULT, BOOK) == {
        "quote_exact": True,
        "metadata_exact": True,
        "reader_anchor_exact": True,
    }


@pytest.mark.parametrize(
    ("mutation", "check"),
    [
        ({"text": "Выдуманная цитата", "verified": True}, "quote_exact"),
        ({"start": -1}, "quote_exact"),
        ({"end": 999}, "quote_exact"),
        ({"start": True}, "quote_exact"),
        ({"end": 4}, "quote_exact"),
        ({"author": "Другой автор"}, "metadata_exact"),
        ({"edition": "Другое издание"}, "metadata_exact"),
        ({"citation": "Без сведений о книге"}, "metadata_exact"),
        ({"section_title": "Другая глава"}, "metadata_exact"),
        ({"reader_url": "/?book=book-1&section=chapter-2&passage=passage-1"}, "reader_anchor_exact"),
        ({"reader_url": "/?book=book-1&section=chapter-1&passage=passage-1&passage=other"}, "reader_anchor_exact"),
    ],
)
def test_corruption_is_detected(mutation: dict[str, Any], check: str) -> None:
    assert not verify_result(RESULT | mutation, BOOK)[check]


def test_unknown_book_or_section_fails_all_checks() -> None:
    assert not any(verify_result(RESULT, None).values())
    assert not any(verify_result(RESULT | {"book_id": "wrong"}, BOOK).values())
    assert not any(verify_result(RESULT | {"section_id": "wrong"}, BOOK).values())


class FakeLibrary:
    def search(
        self,
        query: str,
        mode: str = "lexical",
        book_id: str | None = None,
        language: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        results = [] if query == "unanswerable" else [deepcopy(RESULT)]
        if query == "second rank":
            results.insert(0, RESULT | {"id": "other", "book_id": "missing"})
        return {"results": results}

    def get_book(self, book_id: str) -> dict[str, Any] | None:
        return deepcopy(BOOK) if book_id == BOOK["id"] else None

    def stats(self) -> dict[str, Any]:
        return {"books": 1, "works": 1, "passages": 1}

    def books(self, language: str | None = None, q: str = "") -> list[dict[str, Any]]:
        return [deepcopy(BOOK)]


def write_fixture(tmp_path: Path, queries: list[dict[str, Any]], **kwargs: Any) -> Path:
    path = tmp_path / "evaluation.json"
    path.write_text(json.dumps({"version": 1, "split": "dev", "queries": queries} | kwargs), encoding="utf-8")
    return path


def test_metrics_use_relevant_work_rank_and_negative_abstention(tmp_path: Path) -> None:
    path = write_fixture(
        tmp_path,
        [
            {"id": "q1", "query": "first rank", "answerable": True, "relevant_work_ids": ["work-1"]},
            {"id": "q2", "query": "second rank", "answerable": True, "relevant_work_ids": ["work-1"]},
            {"id": "q3", "query": "unanswerable", "answerable": False},
        ],
    )
    report = evaluate(FakeLibrary(), path, limit=5)
    assert report["metrics"]["mrr_at_k"] == 0.75
    assert report["metrics"]["hit_at_k"] == 1.0
    assert report["metrics"]["work_recall_at_k"] == 1.0
    assert report["metrics"]["negative_abstention_accuracy"] == 1.0
    assert report["metrics"]["quote_exact_rate"] == pytest.approx(2 / 3)
    assert len(report["dataset_sha256"]) == 64
    assert report["corpus"]["books"] == 1


def test_evidence_phrase_required_even_if_work_matches(tmp_path: Path) -> None:
    path = write_fixture(
        tmp_path,
        [
            {
                "id": "q",
                "query": "first rank",
                "answerable": True,
                "relevant_work_ids": ["work-1"],
                "evidence_contains": ["A different scene"],
            }
        ],
    )
    report = evaluate(FakeLibrary(), path)
    assert report["metrics"]["mrr_at_k"] == 0
    assert report["metrics"]["work_recall_at_k"] == 1


@pytest.mark.parametrize(
    "queries",
    [
        [{"id": "q", "query": "text", "answerable": True}],
        [{"id": "q", "query": "text", "answerable": "yes"}],
        [{"id": "q", "query": " ", "answerable": False}],
        [{"id": "q", "query": "text", "answerable": False, "evidence_contains": "not a list"}],
        [{"id": "q", "query": "one", "answerable": False}, {"id": "q", "query": "two", "answerable": False}],
    ],
)
def test_bad_dataset_is_rejected(tmp_path: Path, queries: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError):
        load_dataset(write_fixture(tmp_path, queries))


def test_empty_dataset_has_no_invented_scores(tmp_path: Path) -> None:
    report = evaluate(FakeLibrary(), write_fixture(tmp_path, []))
    assert report["metrics"]["mrr_at_k"] is None
    assert report["metrics"]["quote_exact_rate"] is None
    assert report["metrics"]["negative_abstention_accuracy"] is None


def test_invalid_k_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        evaluate(FakeLibrary(), write_fixture(tmp_path, []), limit=0)


def test_report_comparison_requires_same_fixture_and_corpus(tmp_path: Path) -> None:
    from book_rag.evaluation import compare_reports

    report = evaluate(FakeLibrary(), write_fixture(tmp_path, []))
    with pytest.raises(ValueError, match="dataset_sha256"):
        compare_reports(report, report | {"dataset_sha256": "different"})
    with pytest.raises(ValueError, match="corpus_sha256"):
        compare_reports(report, report | {"corpus_sha256": "different"})
    with pytest.raises(ValueError, match="passages"):
        compare_reports(report, report | {"corpus": {"books": 1, "works": 1, "passages": 2}})
    comparison = compare_reports(report, report)
    assert comparison["metrics"]["latency_p50_ms"]["delta"] == 0
    assert comparison["metrics"]["latency_p50_ms"]["higher_is_better"] is False


def test_fixture_ground_truth_is_present_in_demo_source() -> None:
    from book_rag.parsers import parse_book

    root = Path(__file__).resolve().parents[1]
    texts = {
        path.stem: "\n\n".join(section.text for section in parse_book(path).sections).casefold()
        for path in (root / "corpus" / "demo").glob("*.html")
    }
    assert len(texts) >= 8
    splits = [load_dataset(root / "evals" / f"{split}.json") for split in ("dev", "heldout")]
    dev_queries = {case["query"] for case in splits[0]["queries"]}
    assert not dev_queries.intersection(case["query"] for case in splits[1]["queries"])
    for fixture in splits:
        for case in fixture["queries"]:
            targets = case.get("relevant_work_ids", [])
            assert set(targets).issubset(texts), case["id"]
            for fragment in case.get("evidence_contains", []):
                assert any(fragment.casefold() in texts[work] for work in targets), (case["id"], fragment)


@pytest.mark.parametrize(
    ("start", "end"),
    [(0, 999), (-1, 999), (0, True), (0, 0)],
)
def test_invalid_offsets_are_rejected_even_when_python_slice_would_match(start: int, end: int) -> None:
    source = BOOK["sections"][0]["text"]
    # Python clamps an excessive end, accepts negative offsets and treats bool
    # as an integer. Equal sliced text therefore cannot establish valid bounds.
    result = RESULT | {"start": start, "end": end, "text": source[start:end]}
    assert source[start:end] == result["text"]
    assert verify_result(result, BOOK)["quote_exact"] is False
