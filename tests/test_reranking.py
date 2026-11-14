"""Cross-encoder integration contracts without downloading weights in CI."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from book_rag import reranking


class FakeTokenizer:
    def encode(self, text: str, **kwargs: Any) -> list[str]:
        return text.split()

    def decode(self, ids: list[str], **kwargs: Any) -> str:
        return " ".join(ids)

    def num_special_tokens_to_add(self, pair: bool) -> int:
        return 4


class FakeModel:
    tokenizer = FakeTokenizer()
    max_seq_length = 32
    device = "fake"

    def __init__(self) -> None:
        self.pairs: list[tuple[str, str]] = []

    def predict(self, pairs: list[tuple[str, str]], **kwargs: Any) -> list[float]:
        self.pairs = pairs
        return [float(index) - 10 for index, pair in enumerate(pairs)]


def candidates(count: int = 2) -> list[dict[str, Any]]:
    return [
        {
            "id": index,
            "text": f"Exact source quote {index} — 🧭",
            "score": 0.1,
            "citation": "Author. Book. Edition.",
            "source_url": "https://example.org/book",
            "start": 7,
            "end": 30,
            "verified": True,
        }
        for index in range(count)
    ]


def test_rank_preserves_evidence_and_negative_logits_without_mutation() -> None:
    rows = candidates()
    original = deepcopy(rows)
    ranked = reranking.rank_scored_results(rows, [-9.1, -2.3])
    assert [row["id"] for row in ranked] == [1, 0]
    assert ranked[0]["score"] == -2.3
    assert ranked[0]["retrieval_score"] == 0.1
    for result in ranked:
        assert {key: result[key] for key in original[result["id"]] if key != "score"} == {
            key: value for key, value in original[result["id"]].items() if key != "score"
        }
    assert rows == original


def test_equal_logits_preserve_retrieval_order() -> None:
    assert [row["id"] for row in reranking.rank_scored_results(candidates(), [0.2, 0.2])] == [0, 1]


@pytest.mark.parametrize("scores", [[1.0], [1.0, float("nan")], [float("inf"), 0.0], [True, 0.0]])
def test_invalid_scores_are_rejected(scores: list[float]) -> None:
    with pytest.raises(ValueError):
        reranking.rank_scored_results(candidates(), scores)


def test_public_rerank_caps_candidates_and_preserves_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = FakeModel()
    monkeypatch.setattr(reranking, "_load_model", lambda *args: model)
    rows = candidates(60)
    result = reranking.rerank("Navigation", rows, tmp_path, limit=3)
    assert result["available"] is True and result["scored_candidates"] == 50
    assert [row["id"] for row in result["results"]] == [49, 48, 47]
    assert len(model.pairs) == 50 and result["warnings"]
    assert result["revision"] == reranking.DEFAULT_REVISION
    assert result["results"][0]["text"] == rows[49]["text"]


def test_pair_input_is_bounded_but_returned_evidence_is_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = FakeModel()
    monkeypatch.setattr(reranking, "_load_model", lambda *args: model)
    text = " ".join(f"passage{index}" for index in range(100))
    result = reranking.rerank(" ".join(f"query{index}" for index in range(100)), [{"text": text}], tmp_path)
    query, passage = model.pairs[0]
    assert len(query.split()) + len(passage.split()) + 4 <= model.max_seq_length
    assert len(result["warnings"]) == 2
    assert result["results"][0]["text"] == text


def test_missing_optional_dependency_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any) -> Any:
        raise ModuleNotFoundError("sentence_transformers")

    monkeypatch.setattr(reranking, "_load_model", missing)
    result = reranking.rerank("question", candidates(), tmp_path)
    assert result["available"] is False and result["results"] == []
    assert "semantic" in result["warnings"][0]


@pytest.mark.parametrize("error", [OSError("offline"), RuntimeError("inference failed")])
def test_load_or_inference_failure_is_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def failed(*args: Any) -> Any:
        raise error

    monkeypatch.setattr(reranking, "_load_model", failed)
    result = reranking.rerank("question", candidates(), tmp_path)
    assert result["available"] is False and result["results"] == [] and result["warnings"]


def test_empty_candidates_do_not_load_optional_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unwanted(*args: Any) -> Any:
        pytest.fail("Empty retrieval must not download a model")

    monkeypatch.setattr(reranking, "_load_model", unwanted)
    assert reranking.rerank("question", [], tmp_path)["scored_candidates"] == 0


@pytest.mark.parametrize(
    "query,limit,rows", [(" ", 5, []), ("x" * 4001, 5, []), ("q", 51, []), ("q", 5, [{"text": ""}])]
)
def test_invalid_inputs(query: str, limit: int, rows: list[dict[str, Any]], tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        reranking.rerank(query, rows, tmp_path, limit=limit)
