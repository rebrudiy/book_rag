"""Exploratory score-floor selection with an explicit internal validation split.

This is an empirical acceptance policy experiment, not probability calibration
or proof that a retrieved passage entails an answer.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class Example:
    """A higher-is-better score and an independently labeled top-result outcome."""

    id: str
    score: float | None
    answerable: bool
    top_result_relevant: bool


def _examples(examples: Sequence[Mapping[str, Any] | Example]) -> list[Example]:
    result = []
    ids = set()
    for value in examples:
        item = (
            value
            if isinstance(value, Example)
            else Example(
                id=value["id"],
                score=value["score"],
                answerable=value["answerable"],
                top_result_relevant=value["top_result_relevant"],
            )
        )
        if not isinstance(item.id, str) or not item.id.strip() or item.id in ids:
            raise ValueError("Example IDs must be unique nonempty strings")
        if type(item.answerable) is not bool or type(item.top_result_relevant) is not bool:
            raise ValueError("Answerability and top-result relevance must be explicit booleans")
        if item.score is not None and (type(item.score) not in {int, float} or not math.isfinite(item.score)):
            raise ValueError("Scores must be finite real numbers or None for no result")
        if item.top_result_relevant and (not item.answerable or item.score is None):
            raise ValueError("A relevant top result requires an answerable query and an actual score")
        ids.add(item.id)
        result.append(item)
    return result


def wilson_interval(successes: int, total: int) -> list[float] | None:
    """Return a descriptive 95% Wilson interval, undefined for zero observations."""
    if type(successes) is not int or type(total) is not int or not 0 <= successes <= total:
        raise ValueError("Expected integer counts satisfying 0 <= successes <= total")
    if not total:
        return None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return [max(0.0, center - radius), min(1.0, center + radius)]


def evaluate_threshold(examples: Sequence[Mapping[str, Any] | Example], threshold: float | None) -> dict[str, Any]:
    """Measure a fixed score floor; None is a deliberate abstain-all policy.

    A wrong top result is a false acceptance even when its query is answerable.
    Missing scores always abstain, at every finite threshold.
    """
    rows = _examples(examples)
    if threshold is not None and (type(threshold) not in {int, float} or not math.isfinite(threshold)):
        raise ValueError("Threshold must be finite or None for abstain-all")
    accepted = [row for row in rows if threshold is not None and row.score is not None and row.score >= threshold]
    positive = sum(row.answerable for row in rows)
    negative = len(rows) - positive
    correct_top = sum(row.top_result_relevant for row in rows)
    correct_accept = sum(row.top_result_relevant for row in accepted)
    false_accept = len(accepted) - correct_accept
    negative_accept = sum(not row.answerable for row in accepted)
    wrong_top_accept = false_accept - negative_accept
    return {
        "policy": "abstain_all" if threshold is None else "score_at_least",
        "threshold": threshold,
        "queries": len(rows),
        "answerable_queries": positive,
        "negative_queries": negative,
        "correct_top_results": correct_top,
        "no_result_queries": sum(row.score is None for row in rows),
        "accepted": len(accepted),
        "abstained": len(rows) - len(accepted),
        "true_accept": correct_accept,
        "false_accept": false_accept,
        "negative_false_accept": negative_accept,
        "wrong_top_false_accept": wrong_top_accept,
        "precision": correct_accept / len(accepted) if accepted else None,
        "precision_interval_95": wilson_interval(correct_accept, len(accepted)),
        "recall": correct_accept / positive if positive else None,
        "correct_top_retention": correct_accept / correct_top if correct_top else None,
        "false_accept_rate": false_accept / (len(rows) - correct_top) if len(rows) != correct_top else None,
        "negative_abstention": (negative - negative_accept) / negative if negative else None,
        "negative_abstention_interval_95": wilson_interval(negative - negative_accept, negative),
        "coverage": len(accepted) / len(rows) if rows else None,
    }


def threshold_table(examples: Sequence[Mapping[str, Any] | Example]) -> list[dict[str, Any]]:
    """Sweep observed score cut points inclusively, preserving tied scores."""
    rows = _examples(examples)
    cuts = sorted({float(row.score) for row in rows if row.score is not None})
    return [evaluate_threshold(rows, threshold) for threshold in cuts] + [evaluate_threshold(rows, None)]


def select_threshold(
    examples: Sequence[Mapping[str, Any] | Example],
    *,
    objective: Literal["precision", "utility"] = "precision",
    target_precision: float = 0.9,
    min_accepted: int = 3,
    true_accept_value: float = 1.0,
    false_accept_cost: float = 5.0,
    missed_answer_cost: float = 0.0,
) -> dict[str, Any]:
    """Choose a policy using only supplied training examples and declared goals.

    Precision selection maximizes retained correct results among policies meeting
    the observed precision target and sample-count minimum. Utility selection
    uses explicit per-result rewards/costs; it can legitimately choose abstain-all.
    No feasible precision policy is reported as an unmet target, not 100% precision.
    """
    if objective not in {"precision", "utility"} or not 0 < target_precision <= 1:
        raise ValueError("Specify precision or utility objective and target_precision in (0, 1]")
    if type(min_accepted) is not int or min_accepted < 1:
        raise ValueError("min_accepted must be a positive integer")
    for cost in (true_accept_value, false_accept_cost, missed_answer_cost):
        if type(cost) not in {int, float} or not math.isfinite(cost) or cost < 0:
            raise ValueError("Utility rewards and costs must be finite nonnegative numbers")
    table = threshold_table(examples)
    if objective == "precision":
        eligible = [row for row in table if row["accepted"] >= min_accepted and row["precision"] >= target_precision]
        chosen = (
            max(eligible, key=lambda row: (row["true_accept"], row["precision"], row["threshold"]))
            if eligible
            else table[-1]
        )
        status = "training_target_met" if eligible else "training_target_not_met"
    else:

        def utility(row: dict[str, Any]) -> float:
            return (
                row["true_accept"] * true_accept_value
                - row["false_accept"] * false_accept_cost
                - (row["answerable_queries"] - row["true_accept"]) * missed_answer_cost
            )

        chosen = max(table, key=lambda row: (utility(row), -row["false_accept"], -row["accepted"]))
        status = "training_utility_maximized"
    return {
        "objective": objective,
        "target_precision": target_precision,
        "min_accepted": min_accepted,
        "utility_parameters": {
            "true_accept_value": true_accept_value,
            "false_accept_cost": false_accept_cost,
            "missed_answer_cost": missed_answer_cost,
        },
        "status": status,
        "threshold": chosen["threshold"],
        "training_metrics": chosen,
    }


def deterministic_split(
    examples: Sequence[Mapping[str, Any] | Example],
    *,
    validation_fraction: float = 0.3,
    seed: str = "book-rag-score-floor-v1",
) -> tuple[list[Example], list[Example]]:
    """Split by a deterministic hash ordering of IDs, without examining labels.

    Both partitions are nonempty for at least two examples. Because labels are
    not stratified, a small validation partition may contain few/no negatives.
    """
    rows = _examples(examples)
    if not 0 < validation_fraction < 1:
        raise ValueError("validation_fraction must be in (0, 1)")
    ordered = sorted(rows, key=lambda row: (hashlib.sha256((seed + "\0" + row.id).encode()).hexdigest(), row.id))
    count = max(1, min(len(rows) - 1, round(len(rows) * validation_fraction))) if len(rows) > 1 else 0
    validation = ordered[:count]
    training = ordered[count:]
    return training, validation


def calibrate(
    examples: Sequence[Mapping[str, Any] | Example],
    *,
    target_precision: float = 0.9,
    min_accepted: int = 3,
    objective: Literal["precision", "utility"] = "precision",
    validation_fraction: float = 0.3,
    seed: str = "book-rag-score-floor-v1",
    false_accept_cost: float = 5.0,
) -> dict[str, Any]:
    """Separate an optimistic in-sample table from fixed-policy validation."""
    rows = _examples(examples)
    train, validation = deterministic_split(rows, validation_fraction=validation_fraction, seed=seed)
    parameters: dict[str, Any] = {
        "objective": objective,
        "target_precision": target_precision,
        "min_accepted": min_accepted,
        "false_accept_cost": false_accept_cost,
    }
    selection = select_threshold(train, **parameters)
    return {
        "score_direction": "higher_is_better",
        "threshold_comparison": "score >= threshold",
        "no_result_policy": "score=None always abstains; threshold=None means abstain-all",
        "partition": {
            "seed": seed,
            "validation_fraction": validation_fraction,
            "training_ids": [row.id for row in train],
            "validation_ids": [row.id for row in validation],
        },
        "in_sample_diagnostic": {
            "selection": select_threshold(rows, **parameters),
            "table": threshold_table(rows),
            "warning": "Selected and scored on the same rows; optimistic, not independent validation.",
        },
        "training_selection": selection,
        "training_table": threshold_table(train),
        "validation_metrics": evaluate_threshold(validation, selection["threshold"]),
        "validation_available": bool(validation),
        "limitations": [
            "Internal development split; not an independent final-test estimate.",
            "Score floors do not establish entailment or calibrated answer probabilities.",
            "Small curated samples and score distributions depend on language, corpus, model and candidate generation.",
            "Wilson intervals describe finite fixture counts; the curated queries are not an IID population sample.",
            "Wrong-top results for answerable queries are false acceptances; no-result rows always abstain.",
        ],
    }
