"""The bounded runner is exercised only on owned CC0 sources, never live data."""

from __future__ import annotations

import hashlib
import runpy
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from book_rag.library import Library

BENCH = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/benchmark_lexical_large.py"))


def seed(tmp_path: Path) -> Path:
    library = Library(tmp_path / "library")
    for identity, text in (
        ("pg2680-full", "A man remembers a White Rabbit in this original CC0 test scene."),
        ("pg1228-full", "A man describes a struggle for existence in a separate original CC0 test scene."),
    ):
        source = tmp_path / f"{identity}.txt"
        source.write_text(text, encoding="utf-8")
        library.import_book(
            source,
            {
                "id": identity,
                "title": "Original CC0 runner fixture; ID is a filter alias only",
                "author": "Book Atlas CC0 fixture author",
                "language": "en",
                "rights": "CC0 1.0 · original runner text",
            },
        )
    return library.data_dir


def test_readonly_real_core_comparison_does_not_initialize_source(tmp_path: Path) -> None:
    directory = seed(tmp_path)
    database = directory / "library.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with patch("book_rag.library.Library.__init__", side_effect=AssertionError("No source constructor")):
        report = BENCH["run"](directory, budget_seconds=3, query_seconds=1)
    assert report["status"] == "passed" and report["source_guards_passed"]
    assert len(report["cases"]) == 4
    for case in report["cases"]:
        assert case["equivalence"] == "exact" and case["guard"]["returned_candidates"] > 0
        assert case["control"]["status"] == case["integrated"]["status"] == "completed"
        assert case["actual_query_plan"]
    assert before == hashlib.sha256(database.read_bytes()).hexdigest()


def test_sql_progress_deadline_interrupts_and_does_not_credit_results(tmp_path: Path) -> None:
    directory = seed(tmp_path)
    library = BENCH["ReadOnlyLibrary"](BENCH["SourceGuard"](directory), time.monotonic() + 2)

    def expensive() -> object:
        with library.connect() as connection:
            return connection.execute(
                "WITH RECURSIVE count(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM count WHERE x<1000000000) "
                "SELECT sum(x) FROM count"
            ).fetchone()

    status, result = BENCH["phase"](library, "owned-timeout", expensive, 0.005)
    assert status["status"] == "timeout" and result is None
    with library.connect() as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM passages")
    library.overall_deadline = time.monotonic() - 1
    status, result = BENCH["phase"](library, "skipped", lambda: pytest.fail("Do not run after budget"), 1)
    assert status["status"] == "skipped_overall_budget" and result is None


def test_nonempty_wal_and_main_identity_changes_fail_closed(tmp_path: Path) -> None:
    directory = seed(tmp_path)
    guard = BENCH["SourceGuard"](directory)
    # Empty reader sidecars do not alter canonical input; nonempty WALs do.
    wal = directory / "library.sqlite3-wal"
    wal.write_bytes(b"")
    guard.ensure()
    wal.write_bytes(b"uncheckpointed owned fixture")
    with pytest.raises(ValueError, match="nonempty WAL"):
        guard.ensure()
    wal.unlink()
    with (directory / "library.sqlite3").open("ab") as stream:
        stream.write(b"owned corruption")
    with pytest.raises(RuntimeError, match="changed"):
        guard.ensure()
