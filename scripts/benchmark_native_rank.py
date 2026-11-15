"""Prototype native FTS5 rank streaming with exact bounded score/ID tie handling.

Only owned CC0 fixtures and optional frozen20 are accepted. Core code, live large
libraries, weights and final-test data are untouched. No model imports/inference.
Native rank + strict eligibility + bounded heap + hydration share one explicit
read transaction. A boundary tie is fully scanned, never arbitrarily truncated.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import runpy
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from book_rag.library import Library

PROTOTYPE = runpy.run_path(str(Path(__file__).with_name("benchmark_lexical_plan.py")))
RANK_FUNCTION = "bm25(1.0,2.0,1.5,1.2)"


class FrozenSource(Library):
    """No Library initialization; every database connection is immutable/query-only."""

    def __init__(self, database: Path, deadline: float) -> None:
        self.db_path = database.resolve(strict=True)
        self.data_dir = self.db_path.parent
        self.deadline = deadline

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        with PROTOTYPE["readonly"](self.db_path) as connection:
            connection.set_progress_handler(lambda: int(time.monotonic() >= self.deadline), 1000)
            yield connection


def native_statement(case: Any) -> tuple[str, tuple[Any, ...]]:
    eligibility = (
        "EXISTS (SELECT 1 FROM passages p JOIN books b ON b.id=p.book_id "
        "JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
        "WHERE p.id=passage_fts.rowid AND b.status='indexed' "
        "AND (? IS NULL OR b.id=?) AND (? IS NULL OR b.language=?)) "
    )
    envelope = ""
    parameters: tuple[Any, ...] = (case.expression, case.book_id, case.book_id, case.language, case.language)
    if case.book_id is not None:
        envelope = (
            "AND passage_fts.rowid>=COALESCE((SELECT id FROM passages WHERE book_id=? ORDER BY id LIMIT 1),0) "
            "AND passage_fts.rowid<=COALESCE((SELECT id FROM passages WHERE book_id=? ORDER BY id DESC LIMIT 1),-1) "
        )
        parameters += (case.book_id, case.book_id)
    return (
        "SELECT passage_fts.rowid id,rank score FROM passage_fts WHERE passage_fts MATCH ? "
        f"AND rank MATCH '{RANK_FUNCTION}' AND {eligibility}{envelope}ORDER BY rank",
        parameters,
    )


def exact_pool(rows: Iterable[Any], limit: int) -> tuple[list[tuple[int, float]], dict[str, Any]]:
    """Keep only N keys, exhaust boundary-score ties, stop on a strictly worse score."""
    if not 1 <= limit <= 200:
        raise ValueError("Diagnostic pool limit must be in 1..200")
    heap: list[tuple[float, int, int, float]] = []
    seen = 0
    maximum = 0
    previous = -math.inf
    stopped_on_worse = False
    boundary_ties = 0
    for row in rows:
        identity, score = int(row["id"]), float(row["score"])
        if not math.isfinite(score) or score < previous:
            raise ValueError("Native rank stream is nonfinite or not monotonically ordered")
        previous = score
        seen += 1
        if len(heap) == limit and score > heap[0][3]:
            stopped_on_worse = True
            break
        if len(heap) == limit and score == heap[0][3]:
            boundary_ties += 1
        entry = (-score, -identity, identity, score)
        if len(heap) < limit:
            heapq.heappush(heap, entry)
        elif (score, identity) < (heap[0][3], heap[0][2]):
            heapq.heapreplace(heap, entry)
        maximum = max(maximum, len(heap))
    selected = [(entry[2], entry[3]) for entry in sorted(heap, key=lambda entry: (entry[3], entry[2]))]
    return selected, {
        "eligible_rank_rows_seen": seen,
        "boundary_tie_rows_after_pool_filled": boundary_ties,
        "stopped_on_strictly_worse_score": stopped_on_worse,
        "maximum_heap_entries": maximum,
        "selected": len(selected),
    }


def hydrate(connection: sqlite3.Connection, case: Any, selected: list[tuple[int, float]]) -> list[sqlite3.Row]:
    if not selected:
        return []
    values = ",".join("(?,?)" for _ in selected)
    parameters: list[Any] = [value for identity, score in selected for value in (identity, score)]
    parameters.extend((case.book_id, case.book_id, case.language, case.language))
    select, pipeline, roles = PROTOTYPE["projection"](connection, "c.score")
    return connection.execute(
        f"WITH selected(id,score) AS (VALUES {values}) SELECT {select} FROM selected c "
        "JOIN passages p ON p.id=c.id JOIN books b ON b.id=p.book_id "
        f"{pipeline}JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id {roles}"
        "WHERE b.status='indexed' AND (? IS NULL OR b.id=?) AND (? IS NULL OR b.language=?) "
        "ORDER BY c.score,p.id",
        parameters,
    ).fetchall()


def native(connection: sqlite3.Connection, case: Any) -> tuple[list[sqlite3.Row], dict[str, Any]]:
    if connection.in_transaction:
        raise ValueError("Use a fresh connection so the diagnostic owns its read transaction")
    statement, parameters = native_statement(case)
    connection.execute("BEGIN")
    try:
        cursor = connection.execute(statement, parameters)
        try:
            selected, statistics = exact_pool(cursor, case.candidate_limit)
        finally:
            cursor.close()
        records = hydrate(connection, case, selected)
        statistics["hydrated_records"] = len(records)
        return records, statistics
    finally:
        if connection.in_transaction:
            connection.rollback()


def compare(database: Path, cases: list[Any], seconds: float = 5) -> list[dict[str, Any]]:
    source = FrozenSource(database, time.monotonic() + seconds)
    reports = []
    for case in cases:
        captured: list[dict[str, Any]] = []
        original = Library._result

        def observe(
            row: sqlite3.Row,
            rows: list[dict[str, Any]] = captured,
            convert: Callable[[sqlite3.Row], dict[str, Any]] = original,
        ) -> dict[str, Any]:
            rows.append(dict(row))
            return convert(row)

        source.deadline = time.monotonic() + seconds
        started = time.perf_counter()
        with patch.object(Library, "_result", new=staticmethod(observe)):
            control = source.search(case.query, case.mode, case.book_id, case.language, case.limit)
        control_ms = (time.perf_counter() - started) * 1000
        source.deadline = time.monotonic() + seconds
        started = time.perf_counter()
        with source.connect() as connection:
            records, statistics = native(connection, case)
            final = PROTOTYPE["final_results"](case, records)
        candidate_ms = (time.perf_counter() - started) * 1000
        verification = PROTOTYPE["guard"](case, captured, records)
        if final != control["results"]:
            raise AssertionError("Native pool changed current core final results")
        source.deadline = time.monotonic() + seconds
        with source.connect() as connection:
            statement, parameters = native_statement(case)
            plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + statement, parameters)]
        reports.append(
            {
                "case": case.__dict__,
                "control_current_core_ms": round(control_ms, 3),
                "native_ms": round(candidate_ms, 3),
                "guard": verification,
                "stream": statistics,
                "native_query_plan": plan,
                "sql_sha256": hashlib.sha256(statement.encode()).hexdigest(),
            }
        )
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen20", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/bench-native-rank-v1/report.json"))
    args = parser.parse_args()
    if args.frozen20 and args.output.resolve().is_relative_to(args.frozen20.resolve()):
        parser.error("Output must remain outside frozen input")
    case_type = PROTOTYPE["Case"]
    cases = [
        case_type("global-common", "ambercommon"),
        case_type("baseline-ties", "ambercommon", mode="baseline"),
        case_type("scoped-contiguous", "ambercommon", book_id="book-2"),
        case_type("scoped-scattered", "ambercommon", book_id="scattered"),
        case_type("language", "ambercommon", language="de"),
        case_type("phrase", '"ambercommon copper"'),
        case_type("missing-book", "ambercommon", book_id="absent"),
    ]
    with tempfile.TemporaryDirectory(prefix="book-atlas-native-rank-") as temporary:
        database = Path(temporary) / "library.sqlite3"
        PROTOTYPE["seed"](database)
        # A valid FK with foreign section ownership must be excluded as well.
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE passages SET book_id='book-2' WHERE id=1002")
        report: dict[str, Any] = {
            "kind": "native_fts_rank_candidate_prototype_no_production_promotion",
            "sqlite_version": sqlite3.sqlite_version,
            "synthetic_cc0": compare(database, cases),
        }
    if args.frozen20:
        database = args.frozen20 / "library.sqlite3"
        with PROTOTYPE["readonly"](database) as connection:
            books = connection.execute("SELECT COUNT(*) FROM books WHERE status='indexed'").fetchone()[0]
            count = connection.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
        if books != 20 or count != 12108:
            raise ValueError("Only the known frozen20 corpus is allowed; large corpus needs a separate authorization")
        if shutil.disk_usage(tempfile.gettempdir()).free < database.stat().st_size + 4 * 1024**3:
            raise OSError("An owned compatibility copy requires its bytes plus a 4GiB free reserve")
        # The old published schema lacks later source_pipeline/translator fields.
        # Migrate only an owned copy, never initialize Library on frozen input.
        with tempfile.TemporaryDirectory(prefix="book-atlas-native-frozen20-") as temporary:
            owned = Path(temporary)
            with PROTOTYPE["readonly"](database) as original, sqlite3.connect(owned / "library.sqlite3") as target:
                original.backup(target)
            Library(owned)
            report["frozen20"] = compare(
                owned / "library.sqlite3",
                [
                    case_type("global-man", "man"),
                    case_type("scoped-meditations", "man", book_id="pg2680-full"),
                    case_type("darwin-struggle", "struggle for existence", book_id="pg1228-full"),
                    case_type("baseline-white-rabbit", "White Rabbit", mode="baseline"),
                ],
            )
        report["frozen20_compatibility_copy"] = True
    report["limitations"] = [
        "One current-core then candidate measurement per case; second may benefit from warm OS caches.",
        "Bounded heap memory does not bound tie-group scan time; all cutoff-score ties must be consumed.",
        "Native FTS5 may compute ranks before yielding rows; small timings do not prove large speedup.",
        "Rank SQL has no LIMIT before eligibility and no secondary SQL sort by rowid.",
        "Candidate ranking and bounded hydration share explicit BEGIN/ROLLBACK read snapshot.",
        "Core, large live corpus, indexes, models and held-out labels were not changed or used.",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for label in ("synthetic_cc0", "frozen20"):
        for record in report.get(label, []):
            print(
                label, record["case"]["name"], record["control_current_core_ms"], record["native_ms"], record["stream"]
            )
    print("Report:", args.output)


if __name__ == "__main__":
    main()
