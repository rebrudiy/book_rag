"""SQL equivalence checks use only an owned original CC0 synthetic database."""

from __future__ import annotations

import hashlib
import runpy
from pathlib import Path
from unittest.mock import patch

import pytest

BENCH = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/benchmark_lexical_plan.py"))


def test_all_plans_preserve_exact_records_scores_citations_and_frozen_bytes(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    BENCH["seed"](database, passages_per_book=12)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    case = BENCH["Case"]
    cases = [
        case("unscoped", "ambercommon"),
        case("scoped", "ambercommon", book_id="book-2"),
        case("interleaved", "ambercommon", book_id="scattered", mode="baseline"),
        case("language", "ambercommon", language="de"),
        case("phrase", '"ambercommon copper"'),
        case("no-match", "missingterm"),
        case("missing-book", "ambercommon", book_id="absent"),
        case("mismatched-language", "ambercommon", book_id="book-2", language="de"),
    ]
    with patch("book_rag.library.Library.__init__", side_effect=AssertionError("Do not initialize source Library")):
        report = BENCH["run"](database, cases, 1)
    assert report["source_unchanged"]
    for result in report["cases"]:
        for variant in result["variants"].values():
            assert variant["guard"]["all_exact"]
            assert variant["instrumented_quote_evaluations"] >= variant["guard"]["returned_candidates"]
    assert before == hashlib.sha256(database.read_bytes()).hexdigest()


def test_eligibility_before_limit_prevents_stale_fts_starvation(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    BENCH["seed"](database, passages_per_book=12)
    case = BENCH["Case"]("baseline", "ambercommon", mode="baseline", limit=5)
    with BENCH["readonly"](database) as connection:
        unsafe = connection.execute(
            "SELECT rowid,bm25(passage_fts,1.0,2.0,1.5,1.2) score FROM passage_fts "
            "WHERE passage_fts MATCH ? ORDER BY score,rowid LIMIT 5",
            (case.expression,),
        ).fetchall()
        assert {9001, 9002, 9003}.issubset({row[0] for row in unsafe})
        control = BENCH["execute"](connection, case, "control")
        assert len(control) == 5 and not {9001, 9002, 9003}.intersection({row["id"] for row in control})
        candidate = BENCH["execute"](connection, case, "pure_fts_eligible")
        assert BENCH["guard"](case, control, candidate)["all_exact"]


def test_noncontiguous_envelope_keeps_exact_book_filter_and_order(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    BENCH["seed"](database, passages_per_book=12)
    case = BENCH["Case"]("interleaved", "ambercommon", book_id="scattered", mode="baseline", limit=5)
    with BENCH["readonly"](database) as connection:
        control = BENCH["execute"](connection, case, "control")
        candidate = BENCH["execute"](connection, case, "materialized_envelope")
        assert len(candidate) == 5
        assert all(row["book_id"] == "scattered" for row in candidate)
        assert BENCH["guard"](case, control, candidate)["all_exact"]
        low, high = connection.execute("SELECT MIN(id),MAX(id) FROM passages WHERE book_id='scattered'").fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM passages WHERE id BETWEEN ? AND ? AND book_id!='scattered'", (low, high)
        ).fetchone()[0] > len(candidate)


def test_guards_reject_changed_scores_or_quotes_and_keep_corruption_suppression(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    BENCH["seed"](database, passages_per_book=12)
    case = BENCH["Case"]("de", "ambercommon", language="de")
    with BENCH["readonly"](database) as connection:
        rows = BENCH["execute"](connection, case, "control")
        assert 1001 in {row["id"] for row in rows}
        final = BENCH["final_results"](case, rows)
        assert all(row["id"] != 1001 and row["verified"] for row in final)
        changed = [dict(row) for row in rows]
        changed[0]["score"] += 0.00001
        with pytest.raises(AssertionError, match="ordered IDs/scores"):
            BENCH["guard"](case, rows, changed)
        changed = [dict(row) for row in rows]
        changed[0]["section_quote"] += " altered"
        with pytest.raises(AssertionError, match="citation text"):
            BENCH["guard"](case, rows, changed)
