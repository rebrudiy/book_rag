"""Comparator safety/receipts use only a tiny owned synthetic CC0 catalogue."""

from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPARE = runpy.run_path(str(ROOT / "scripts/compare_native_search.py"))


def arguments(tmp_path: Path) -> SimpleNamespace:
    source = tmp_path / "owned-synthetic"
    source.mkdir()
    COMPARE["PROTOTYPE"]["seed"](source / "library.sqlite3", passages_per_book=2)
    queries = [
        {
            "id": f"synthetic-{index}",
            "query": "ambercommon" if index % 3 else '"Пример 🦉 café"',
            "filters": {"book_id": "scattered"} if index % 2 else {},
        }
        for index in range(54)
    ]
    dataset = tmp_path / "synthetic-dev.json"
    dataset.write_text(json.dumps({"split": "dev", "queries": queries}), encoding="utf-8")
    return SimpleNamespace(
        source=source,
        dataset=dataset,
        control=ROOT / "src/book_rag/library.py",
        candidate=ROOT / "src/book_rag/library.py",
        output=tmp_path / "output",
        expected_books=8,
        modes=["lexical", "baseline"],
        query_seconds=2,
        control_seconds=20,
        budget_seconds=30,
    )


def test_prepared_comparator_exact_on_owned_synthetic_catalogue(tmp_path: Path) -> None:
    args = arguments(tmp_path)
    database = args.source / "library.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    report = COMPARE["run"](args)
    assert report["status"] == "complete" and report["validated"]
    assert report["source_guards_passed"]
    assert len(report["cases"]) == 108
    assert all(case["equivalence"] == "exact" and case["guard"]["all_exact"] for case in report["cases"])
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not database.with_name(database.name + "-wal").exists()
    assert len(list((args.output / "unvalidated-control").glob("*.json"))) == 108


def test_timeout_never_receives_equivalence_or_speedup_credit(tmp_path: Path) -> None:
    args = arguments(tmp_path)
    args.modes = ["baseline"]
    actual_phase = COMPARE["LARGE"]["phase"]

    def timeout_once(library, name, operation, deadline):
        if name == "control:baseline:synthetic-0":
            return {"status": "timeout", "elapsed_ms": 1}, None
        return actual_phase(library, name, operation, deadline)

    with patch.dict(COMPARE["LARGE"], {"phase": timeout_once}):
        report = COMPARE["run"](args)
    assert report["status"] == "incomplete" and not report["validated"]
    first = report["cases"][0]
    assert first["equivalence"] == "not_compared" and first["speedup"] is None
    assert report["source_guards_passed"]


@pytest.mark.parametrize("split", ["final", "heldout"])
def test_rejects_nondevelopment_fixture_before_source_open(tmp_path: Path, split: str) -> None:
    args = arguments(tmp_path)
    payload = json.loads(args.dataset.read_text())
    payload["split"] = split
    args.dataset.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="54-query development"):
        COMPARE["run"](args)
    assert not args.output.exists()


@pytest.mark.parametrize("modes", [["hybrid"], ["lexical", "lexical"], []])
def test_direct_runner_rejects_model_or_duplicate_modes(tmp_path: Path, modes: list[str]) -> None:
    args = arguments(tmp_path)
    args.modes = modes
    with pytest.raises(ValueError, match="model modes are excluded"):
        COMPARE["run"](args)
    assert not args.output.exists()
