"""One bounded read-only large-catalogue comparison, never a relevance benchmark.

Reads a quiet existing library with mode=ro&immutable=1/query_only and no Library
constructor. Nonempty WALs or changed main/index identities reject the run.
Each case executes the preserved old SQL once, then current Library.search once.
SQLite VM deadlines and an overall budget record timeouts without equivalence
or speedup credit. No models, source migrations, live writes or broad repeats.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import runpy
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from book_rag.library import Library

PROTOTYPE = runpy.run_path(str(Path(__file__).with_name("benchmark_lexical_plan.py")))


def file_state(path: Path) -> tuple[int, int, int, int, int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular nonsymlink input: {path.name}")
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


class SourceGuard:
    """Empty WAL/SHM sidecars are transient readers, not canonical input bytes."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.expanduser().resolve(strict=True)
        self.database = self.directory / "library.sqlite3"
        self.watched = [self.database]
        self.generation: str | None = None
        active = self.directory / "vectors/active.json"
        self.active = active
        self.had_active = active.exists()
        if active.exists():
            if file_state(active)[2] > 32 * 1024**2:
                raise ValueError("Unexpectedly large active index metadata")
            metadata = json.loads(active.read_text(encoding="utf-8"))
            generation = metadata.get("generation")
            if not isinstance(generation, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}", generation):
                raise ValueError("Invalid active generation name")
            folder = active.parent / generation
            if folder.is_symlink() or not folder.is_dir():
                raise ValueError("Expected a local immutable generation directory")
            self.generation = generation
            self.watched.append(active)
            for name in (
                "embeddings.npy",
                "ids.npy",
                "metadata.json",
                "checkpoint.json",
                "vector-reuse.json",
                "window-keys.sqlite3",
            ):
                candidate = folder / name
                if candidate.exists():
                    self.watched.append(candidate)
        self.before = {path: file_state(path) for path in self.watched}
        self.ensure()

    def ensure(self) -> None:
        wal = self.database.with_name(self.database.name + "-wal")
        if wal.is_symlink() or (wal.exists() and wal.stat().st_size):
            raise ValueError("The source must have no nonempty WAL during immutable reads")
        if self.active.exists() != self.had_active:
            raise RuntimeError("Active generation availability changed during the comparison")
        if any(file_state(path) != identity for path, identity in self.before.items()):
            raise RuntimeError("Main database or active generation changed during the comparison")


class ReadOnlyLibrary(Library):
    """Search-only adapter: deliberately skips Library.__init__ and all writes."""

    def __init__(self, guard: SourceGuard, overall_deadline: float) -> None:
        self.guard = guard
        self.data_dir = guard.directory
        self.db_path = guard.database
        self.overall_deadline = overall_deadline
        self.phase_deadline = overall_deadline
        self.statement: str | None = None

    def captured_statement(self) -> str | None:
        return self.statement

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        self.guard.ensure()
        connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0.2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")

        def trace(statement: str) -> None:
            if statement.startswith("WITH candidates AS MATERIALIZED") or statement.startswith(
                "SELECT passage_fts.rowid id,rank score FROM passage_fts"
            ):
                self.statement = statement

        connection.set_trace_callback(trace)
        connection.set_progress_handler(
            lambda: int(time.monotonic() >= min(self.phase_deadline, self.overall_deadline)), 1000
        )
        try:
            yield connection
        finally:
            connection.close()
            self.guard.ensure()


def phase(
    library: ReadOnlyLibrary, name: str, operation: Callable[[], Any], query_seconds: float
) -> tuple[dict[str, Any], Any]:
    now = time.monotonic()
    if now >= library.overall_deadline:
        return {"status": "skipped_overall_budget"}, None
    library.phase_deadline = min(now + query_seconds, library.overall_deadline)
    print(json.dumps({"phase": name, "status": "starting"}), flush=True)
    try:
        result = operation()
        elapsed = time.monotonic() - now
        if time.monotonic() > library.phase_deadline:
            return {"status": "timeout_wall_overrun", "elapsed_ms": round(elapsed * 1000, 3)}, None
        return {"status": "completed", "elapsed_ms": round(elapsed * 1000, 3)}, result
    except sqlite3.OperationalError as error:
        elapsed = time.monotonic() - now
        timed_out = "interrupted" in str(error).lower() and time.monotonic() >= library.phase_deadline
        return {
            "status": "timeout" if timed_out else "sqlite_error",
            "elapsed_ms": round(elapsed * 1000, 3),
            "error": str(error),
        }, None


def run(
    source: Path, *, query_seconds: float = 18, budget_seconds: float = 110, include_russian: bool = False
) -> dict[str, Any]:
    if not 0 < query_seconds <= 20 or not 0 < budget_seconds <= 115:
        raise ValueError("Use positive query deadlines <=20s and total budget <=115s")
    started = time.monotonic()
    guard = SourceGuard(source)
    library = ReadOnlyLibrary(guard, started + budget_seconds)
    case_type = PROTOTYPE["Case"]
    cases = [
        case_type("global-man", "man"),
        case_type("scoped-meditations-man", "man", book_id="pg2680-full"),
        case_type("scoped-darwin-struggle", "struggle for existence", book_id="pg1228-full"),
        case_type("baseline-white-rabbit", "White Rabbit", mode="baseline"),
    ]
    if include_russian:
        cases.append(case_type("global-russian-life", "жизнь"))
    report: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "kind": "bounded_readonly_sql_plan_comparison_not_relevance_or_throughput",
        "source_name": guard.directory.name,
        "generation": guard.generation,
        "sqlite_version": sqlite3.sqlite_version,
        "per_query_deadline_seconds": query_seconds,
        "overall_budget_seconds": budget_seconds,
        "cases": [],
        "source_guards_passed": False,
        "limitations": [
            "One old-control then current-core execution; the second may benefit from warm OS caches.",
            "No repeated/isolated cold-cache estimates or large-corpus relevance claims.",
            "SQLite progress callbacks cannot interrupt time inside a single blocking C/filesystem operation.",
            "A wall overrun or timeout receives no equivalence/speedup credit, even if another phase completed.",
            "Input guards check file identities and nonempty WALs, not full 5GB database/vector hashes.",
            "Empty transient WAL/SHM sidecars may be created by external readers; this adapter never creates them.",
            "Misowned rows intentionally differ from the old control; mismatches are reported as failures.",
            "No model, vector matrix, Library constructor, corpus write, migration, or held-out fixture is used.",
        ],
    }
    try:
        for case in cases:

            def control(selected: Any = case) -> list[sqlite3.Row]:
                with library.connect() as connection:
                    return PROTOTYPE["execute"](connection, selected, "control")

            control_status, control_rows = phase(library, case.name + ":control", control, query_seconds)
            observed: list[dict[str, Any]] = []
            original_result = Library._result

            def result(
                row: sqlite3.Row,
                captured: list[dict[str, Any]] = observed,
                convert: Callable[[sqlite3.Row], dict[str, Any]] = original_result,
            ) -> dict[str, Any]:
                captured.append(dict(row))
                return convert(row)

            library.statement = None
            with patch.object(Library, "_result", side_effect=result):
                actual_status, actual = phase(
                    library,
                    case.name + ":integrated",
                    lambda selected=case: library.search(
                        selected.query, selected.mode, selected.book_id, selected.language, selected.limit
                    ),
                    query_seconds,
                )
            record: dict[str, Any] = {
                "case": case.__dict__,
                "control": control_status,
                "integrated": actual_status,
                "equivalence": "not_compared",
                "speedup": None,
            }
            if control_status["status"] == actual_status["status"] == "completed":
                try:
                    verification = PROTOTYPE["guard"](case, control_rows, observed)
                    if actual["results"] != PROTOTYPE["final_results"](case, control_rows):
                        raise AssertionError("Current final result differs from old-control evidence")
                    record["equivalence"] = "exact"
                    record["guard"] = verification
                    record["speedup"] = round(control_status["elapsed_ms"] / actual_status["elapsed_ms"], 3)
                except AssertionError as error:
                    record["equivalence"] = "mismatch"
                    record["error"] = str(error)
            statement = library.captured_statement()
            if statement:
                record["actual_sql_sha256"] = hashlib.sha256(statement.encode()).hexdigest()
                if time.monotonic() < library.overall_deadline:
                    library.phase_deadline = library.overall_deadline
                    with library.connect() as connection:
                        record["actual_query_plan"] = [
                            row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + statement)
                        ]
            report["cases"].append(record)
            print(
                json.dumps(
                    {
                        "case": case.name,
                        **{key: record[key] for key in ("control", "integrated", "equivalence", "speedup")},
                    }
                ),
                flush=True,
            )
        guard.ensure()
        report["source_guards_passed"] = True
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        report["source_error"] = str(error)
        # An unstable immutable input invalidates the comparison's claims.
        for record in report["cases"]:
            record["equivalence"] = "invalid_source_guard"
            record["speedup"] = None
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["status"] = (
        "passed"
        if report["source_guards_passed"] and all(record["equivalence"] == "exact" for record in report["cases"])
        else "incomplete_or_failed"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--query-seconds", type=float, default=18)
    parser.add_argument("--budget-seconds", type=float, default=110)
    parser.add_argument("--include-russian", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("data/bench-lexical-large-v1/report.json"))
    arguments = parser.parse_args()
    if arguments.output.expanduser().resolve().is_relative_to(arguments.source.expanduser().resolve()):
        parser.error("Output must remain outside the source library")
    report = run(
        arguments.source,
        query_seconds=arguments.query_seconds,
        budget_seconds=arguments.budget_seconds,
        include_russian=arguments.include_russian,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({"status": report["status"], "seconds": report["elapsed_seconds"], "output": str(arguments.output)})
    )


if __name__ == "__main__":
    main()
