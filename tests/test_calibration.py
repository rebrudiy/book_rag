"""Score-floor metrics must count wrong-top answers and avoid vacuous precision."""

from __future__ import annotations

from typing import Any

import pytest

from book_rag.calibration import calibrate, deterministic_split, evaluate_threshold, select_threshold, threshold_table


def examples() -> list[dict[str, Any]]:
    return [
        {"id": "right-high", "score": 0.9, "answerable": True, "top_result_relevant": True},
        {"id": "right-low", "score": 0.6, "answerable": True, "top_result_relevant": True},
        {"id": "wrong-top", "score": 0.8, "answerable": True, "top_result_relevant": False},
        {"id": "negative", "score": 0.7, "answerable": False, "top_result_relevant": False},
        {"id": "missing-positive", "score": None, "answerable": True, "top_result_relevant": False},
        {"id": "missing-negative", "score": None, "answerable": False, "top_result_relevant": False},
    ]


def test_false_accepts_include_wrong_top_for_answerable_queries() -> None:
    metrics = evaluate_threshold(examples(), 0.6)
    assert metrics["true_accept"] == 2 and metrics["false_accept"] == 2
    assert metrics["wrong_top_false_accept"] == 1 and metrics["negative_false_accept"] == 1
    assert metrics["precision"] == 0.5 and metrics["recall"] == 0.5
    assert metrics["correct_top_retention"] == 1
    assert metrics["negative_abstention"] == 0.5 and metrics["no_result_queries"] == 2


def test_abstain_all_and_empty_data_never_invent_perfect_precision() -> None:
    for rows in (examples(), []):
        metrics = evaluate_threshold(rows, None)
        assert metrics["accepted"] == 0 and metrics["precision"] is None
        assert metrics["precision_interval_95"] is None
    assert evaluate_threshold(examples(), -100)["accepted"] == 4


def test_threshold_is_inclusive_and_ties_cannot_be_split() -> None:
    rows = examples()[:2]
    rows[1]["score"] = 0.9
    table = threshold_table(rows)
    assert len(table) == 2 and table[0]["accepted"] == 2
    assert table[1]["policy"] == "abstain_all"


def test_unmet_precision_target_is_explicit() -> None:
    selected = select_threshold(examples(), target_precision=0.99, min_accepted=3)
    assert selected["status"] == "training_target_not_met"
    assert selected["threshold"] is None and selected["training_metrics"]["precision"] is None
    selected = select_threshold(examples(), target_precision=0.99, min_accepted=1)
    assert selected["threshold"] == 0.9


def test_explicit_utility_can_prefer_abstaining() -> None:
    rows = [{"id": "bad", "score": 10.0, "answerable": True, "top_result_relevant": False}]
    selected = select_threshold(rows, objective="utility", false_accept_cost=2)
    assert selected["threshold"] is None


def test_split_is_order_independent_label_blind_and_disjoint() -> None:
    train, validation = deterministic_split(examples())
    reversed_train, reversed_validation = deterministic_split(list(reversed(examples())))
    assert train == reversed_train and validation == reversed_validation
    assert {e.id for e in train}.isdisjoint(e.id for e in validation)
    relabeled = [row | {"answerable": False, "top_result_relevant": False} for row in examples()]
    train2, validation2 = deterministic_split(relabeled)
    assert [e.id for e in train] == [e.id for e in train2]
    assert [e.id for e in validation] == [e.id for e in validation2]


def test_validation_uses_training_policy_without_refit() -> None:
    report = calibrate(examples(), min_accepted=1)
    _, validation = deterministic_split(examples())
    assert report["validation_metrics"] == evaluate_threshold(validation, report["training_selection"]["threshold"])
    assert "optimistic" in report["in_sample_diagnostic"]["warning"]


@pytest.mark.parametrize(
    "change",
    [
        {"score": float("nan")},
        {"score": float("inf")},
        {"score": True},
        {"answerable": "yes"},
        {"score": None, "top_result_relevant": True},
        {"answerable": False, "top_result_relevant": True},
    ],
)
def test_invalid_or_inconsistent_examples_are_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        threshold_table([examples()[0] | change])


def test_duplicate_ids_and_bad_thresholds_are_rejected() -> None:
    with pytest.raises(ValueError):
        threshold_table([examples()[0], examples()[0]])
    with pytest.raises(ValueError):
        evaluate_threshold(examples(), float("inf"))


def test_small_negative_sample_exposes_wide_uncertainty() -> None:
    metrics = evaluate_threshold(examples(), 0.9)
    interval = metrics["negative_abstention_interval_95"]
    assert metrics["negative_abstention"] == 1
    assert interval[0] < 0.4 and interval[1] == pytest.approx(1)


def test_tiny_or_no_result_fixture_has_explicit_limits() -> None:
    row = {"id": "none", "score": None, "answerable": False, "top_result_relevant": False}
    report = calibrate([row])
    assert report["validation_available"] is False
    assert report["training_selection"]["status"] == "training_target_not_met"
