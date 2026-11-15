"""Bounded read-only old/new scope SQL comparison on the pinned frozen1308 DB.

Reads only IDs and bibliographic/ownership metadata; no Library constructor,
models, vector scoring, corpus text, source migrations or live database access.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_rag.semantic import _scoped_passage_query

ROOT = Path(__file__).resolve().parents[1]
PIN = "2d77675ba2a68f63e25f507355272e3206db8f9abbe959f326cea96f774939f0"
OLD_SQL = (
    "SELECT p.id FROM passages p JOIN books b ON b.id=p.book_id "
    "JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
    "WHERE (? IS NULL OR b.id=?) AND (? IS NULL OR b.language=?) AND b.status='indexed'"
)


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while part := handle.read(1024 * 1024):
            digest.update(part)
    return digest.hexdigest()


def state(path: Path) -> list[int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected regular immutable source")
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def run(source: Path, *, query_seconds: float = 15) -> dict[str, Any]:
    if not 0 < query_seconds <= 20:
        raise ValueError("Query deadline must be positive and at most20s")
    source = source.resolve(strict=True)
    database = source / "library.sqlite3"
    wal = database.with_name(database.name + "-wal")
    if wal.is_symlink() or (wal.exists() and wal.stat().st_size):
        raise ValueError("Pinned source must have no nonempty WAL")
    before = state(database)
    digest_before = sha(database)
    if digest_before != PIN:
        raise ValueError("This experiment accepts only the pinned frozen1308 input")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.row_factory = sqlite3.Row
    cases = [("pg11-full", None), ("pg2680-full", None), (None, "de")]
    report: dict[str, Any] = {
        "kind": "bounded_frozen_scope_SQL_equivalence_not_relevance_or_end_to_end_latency",
        "created_at": datetime.now(UTC).isoformat(),
        "source_sha256_before": digest_before,
        "source_identity_before": before,
        "source_name": source.name,
        "script_sha256": sha(Path(__file__)),
        "scoped_SQL_builder_sha256": hashlib.sha256(inspect.getsource(_scoped_passage_query).encode()).hexdigest(),
        "sqlite_version": sqlite3.sqlite_version,
        "query_deadline_seconds": query_seconds,
        "cases": [],
        "source_guards_passed": False,
    }
    started = time.monotonic()
    try:
        count = connection.execute("SELECT COUNT(*) FROM books WHERE status='indexed'").fetchone()[0]
        if count != 1308:
            raise ValueError("Unexpected frozen edition count")
        for book_id, language in cases:
            new_sql, new_parameters = _scoped_passage_query(book_id, language)
            record: dict[str, Any] = {"book_id": book_id, "language": language, "methods": {}}
            if book_id:
                header = dict(
                    connection.execute(
                        "SELECT id,title,author,language,edition,status,artifact_sha256,content_sha256 "
                        "FROM books WHERE id=?",
                        (book_id,),
                    ).fetchone()
                )
                record["selected_book_header_sha256"] = hashlib.sha256(
                    json.dumps(header, sort_keys=True).encode()
                ).hexdigest()
            values: dict[str, list[int] | None] = {}
            for name, sql, parameters in (
                ("old_OR_predicates", OLD_SQL, (book_id, book_id, language, language)),
                ("direct_books_first", new_sql, new_parameters),
            ):
                progress_calls = 0
                phase_start = time.perf_counter()
                deadline = time.monotonic() + query_seconds

                def progress(phase_deadline: float = deadline) -> int:
                    nonlocal progress_calls
                    progress_calls += 1
                    return int(time.monotonic() >= phase_deadline)

                connection.set_progress_handler(progress, 1000)
                print(
                    json.dumps({"book_id": book_id, "language": language, "method": name, "status": "starting"}),
                    flush=True,
                )
                try:
                    result = [row[0] for row in connection.execute(sql, parameters)]
                    elapsed = (time.perf_counter() - phase_start) * 1000
                    status = "completed" if time.monotonic() < deadline else "timeout_wall_overrun"
                except sqlite3.OperationalError as error:
                    if "interrupted" not in str(error).lower():
                        raise
                    result = None
                    elapsed = (time.perf_counter() - phase_start) * 1000
                    status = "timeout"
                finally:
                    connection.set_progress_handler(None, 0)
                values[name] = result if status == "completed" else None
                record["methods"][name] = {
                    "status": status,
                    "elapsed_ms": round(elapsed, 3),
                    "VM_progress_steps_lower_bound": progress_calls * 1000,
                    "query_plan": [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)],
                    "ordered_ID_sha256": hashlib.sha256(json.dumps(result).encode()).hexdigest()
                    if result is not None
                    else None,
                }
            old, new = values["old_OR_predicates"], values["direct_books_first"]
            record["equivalence"] = "not_compared"
            if old is not None and new is not None:
                record.update(
                    equivalence="exact"
                    if old == new
                    else "same_membership"
                    if sorted(old) == sorted(new)
                    else "mismatch",
                    ordered_IDs_equal=old == new,
                    sorted_IDs_equal=sorted(old) == sorted(new),
                    passages=len(new),
                    ID_range=[min(new), max(new)] if new else None,
                )
            report["cases"].append(record)
    finally:
        connection.close()
    digest_after = sha(database)
    after = state(database)
    report.update(
        source_sha256_after=digest_after,
        source_identity_after=after,
        source_guards_passed=before == after
        and digest_before == digest_after
        and not (wal.exists() and wal.stat().st_size),
    )
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["status"] = (
        "passed"
        if report["source_guards_passed"] and all(case["equivalence"] == "exact" for case in report["cases"])
        else "incomplete_or_failed"
    )
    report["limitations"] = [
        "One old-then-new pair per scope; full initial SHA warms cache and concurrent host work is not isolated.",
        "This measures metadata eligibility SQL only, not query encoding, scoring, retrieval or answer generation.",
        "Read-only pinned1308 input; no whole-live-catalogue freeze, Library construction, model or source text.",
        "Progress-step counts are lower bounds at1000VM-instruction intervals, not row-read or byte counters.",
        "Timeout/wall overrun receives no equivalence or speedup credit; "
        "complete input SHA/identity guards are mandatory.",
    ]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/snapshots/library1308-v1")
    parser.add_argument("--report", type=Path, default=ROOT / "experiments/semantic-eligibility-v1.json")
    parser.add_argument("--query-seconds", type=float, default=15)
    args = parser.parse_args()
    if args.report.exists() or args.report.resolve().is_relative_to(args.source.resolve()):
        parser.error("Use a new report outside the source")
    result = run(args.source, query_seconds=args.query_seconds)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
