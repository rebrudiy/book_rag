"""One authorized bounded global-man pair: current CTE versus native FTS rank.

No Library constructor, corpus writes, models, broad loops or held-out labels.
Source identities/WAL must stay quiet. Per-phase20s/overall50s; timeout receives
no equivalence or speedup credit. E5-base GPU contention is explicitly recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from book_rag.library import Library

DIRECTORY = Path(__file__).resolve().parent
LARGE = runpy.run_path(str(DIRECTORY / "benchmark_lexical_large.py"))
NATIVE = runpy.run_path(str(DIRECTORY / "benchmark_native_rank.py"))


def gpu_context() -> dict[str, Any]:
    path = DIRECTORY.parent / "data/snapshots/curated20-e5base-v1/build-receipt.json"
    if not path.exists():
        return {"receipt_present": False}
    receipt = json.loads(path.read_text())
    alive = False
    try:
        os.kill(int(receipt["pid"]), 0)
        alive = True
    except (ProcessLookupError, PermissionError, KeyError, ValueError):
        pass
    return {"receipt_status": receipt.get("status"), "pid": receipt.get("pid"), "pid_alive": alive}


def run(
    source: Path, expected_books: int = 4001, query_seconds: float = 20, budget_seconds: float = 50
) -> dict[str, Any]:
    if not 0 < query_seconds <= 20 or not 0 < budget_seconds <= 50:
        raise ValueError("Use query deadlines <=20s and overall budget <=50s")
    started = time.monotonic()
    guard = LARGE["SourceGuard"](source)
    library = LARGE["ReadOnlyLibrary"](guard, started + budget_seconds)
    case = NATIVE["PROTOTYPE"]["Case"]("global-man", "man")
    with library.connect() as connection:
        books = connection.execute("SELECT COUNT(*) FROM books WHERE status='indexed'").fetchone()[0]
    if books != expected_books:
        raise ValueError("Source catalogue count differs from the explicitly authorized input")
    report: dict[str, Any] = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": "one_bounded_native_rank_pair_not_relevance_or_promotion",
        "indexed_editions": books,
        "generation": guard.generation,
        "case": case.__dict__,
        "gpu_context_at_start": gpu_context(),
        "per_phase_seconds": query_seconds,
        "overall_budget_seconds": budget_seconds,
        "source_guards_passed": False,
        "equivalence": "not_compared",
        "speedup": None,
        "limitations": [
            "One current-CTE then native pair; the second may benefit from warmed OS caches.",
            "E5-base embedding may run concurrently on GPU; CPU/I/O and unified memory are not isolated.",
            "Python heap is bounded; SQLite internal rank/cache memory is not bounded or measured by that fact.",
            "All boundary ties must be consumed; a huge tie group can scan the whole eligible ranking.",
            "SQL progress callbacks cannot immediately interrupt a single blocking C/filesystem operation.",
            "Timeout or source changes receive no equivalence/speedup credit.",
        ],
        "implementation_sha256": {
            str(path.relative_to(DIRECTORY.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                Path(__file__),
                DIRECTORY / "benchmark_native_rank.py",
                DIRECTORY.parent / "src/book_rag/library.py",
            ]
        },
    }
    control_rows: list[dict[str, Any]] = []
    original = Library._result

    def observe(row: sqlite3.Row) -> dict[str, Any]:
        control_rows.append(dict(row))
        return original(row)

    try:
        with patch.object(Library, "_result", new=staticmethod(observe)):
            control_status, control = LARGE["phase"](
                library, "global-man:current-CTE", lambda: library.search("man", mode="lexical", limit=5), query_seconds
            )

        def native() -> tuple[list[sqlite3.Row], list[dict[str, Any]], dict[str, Any]]:
            with library.connect() as connection:
                rows, statistics = NATIVE["native"](connection, case)
                final = NATIVE["PROTOTYPE"]["final_results"](case, rows)
            return rows, final, statistics

        native_status, candidate = LARGE["phase"](library, "global-man:native", native, query_seconds)
        report.update(control=control_status, native=native_status)
        if native_status["status"] == "completed":
            report["stream"] = candidate[2]
        if control_status["status"] == native_status["status"] == "completed":
            rows, final, statistics = candidate
            try:
                verification = NATIVE["PROTOTYPE"]["guard"](case, control_rows, rows)
                if final != control["results"]:
                    raise AssertionError("Final verified citation/reranking differs from current core")
                report.update(
                    equivalence="exact",
                    guard=verification,
                    stream=statistics,
                    speedup=round(control_status["elapsed_ms"] / native_status["elapsed_ms"], 3),
                )
            except AssertionError as error:
                report.update(equivalence="mismatch", error=str(error))
        if time.monotonic() < library.overall_deadline:
            library.phase_deadline = library.overall_deadline
            with library.connect() as connection:
                statement, parameters = NATIVE["native_statement"](case)
                report["native_query_plan"] = [
                    row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + statement, parameters)
                ]
        guard.ensure()
        report["source_guards_passed"] = True
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        report.update(source_error=str(error), equivalence="invalid_source_guard", speedup=None)
    report.update(
        elapsed_seconds=round(time.monotonic() - started, 3),
        gpu_context_at_end=gpu_context(),
    )
    report["status"] = (
        "passed" if report["source_guards_passed"] and report["equivalence"] == "exact" else "incomplete_or_failed"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--expected-books", type=int, default=4001)
    parser.add_argument("--query-seconds", type=float, default=20)
    parser.add_argument("--budget-seconds", type=float, default=50)
    parser.add_argument("--output", type=Path, default=Path("data/bench-native-large-v1/report.json"))
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.source.resolve()):
        parser.error("Output must stay outside the source library")
    report = run(args.source, args.expected_books, args.query_seconds, args.budget_seconds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in ["status", "control", "native", "equivalence", "speedup", "stream", "elapsed_seconds"]
            }
        )
    )


if __name__ == "__main__":
    main()
