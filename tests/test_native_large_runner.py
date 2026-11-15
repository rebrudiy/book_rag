"""The one-pair runner is tested only against owned CC0 text."""

import runpy
from pathlib import Path
from unittest.mock import patch

from book_rag.library import Library

RUNNER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/benchmark_native_large.py"))


def seed(tmp_path: Path) -> Path:
    library = Library(tmp_path / "library")
    text = tmp_path / "original.txt"
    text.write_text("An original CC0 scene: a man opens a book and returns to its context.")
    library.import_book(text, {"id": "cc0", "title": "Original CC0 scene", "author": "CC0 fixture", "language": "en"})
    return library.data_dir


def test_real_core_native_pair_exact_and_no_source_constructor(tmp_path: Path) -> None:
    directory = seed(tmp_path)
    with patch("book_rag.library.Library.__init__", side_effect=AssertionError("Do not initialize source")):
        report = RUNNER["run"](directory, expected_books=1, query_seconds=1, budget_seconds=3)
    assert report["status"] == "passed" and report["equivalence"] == "exact"
    assert report["source_guards_passed"] and report["guard"]["returned_candidates"] == 1
    assert report["stream"]["selected"] == 1 and report["stream"]["maximum_heap_entries"] == 1


def test_timeout_never_credits_equivalence_or_speedup(tmp_path: Path) -> None:
    directory = seed(tmp_path)
    helper = RUNNER["run"].__globals__["LARGE"]
    results = [({"status": "timeout", "elapsed_ms": 20}, None), ({"status": "skipped_overall_budget"}, None)]
    with patch.dict(helper, {"phase": lambda *args, **kwargs: results.pop(0)}):
        report = RUNNER["run"](directory, expected_books=1, query_seconds=1, budget_seconds=3)
    assert report["status"] == "incomplete_or_failed"
    assert report["equivalence"] == "not_compared" and report["speedup"] is None
