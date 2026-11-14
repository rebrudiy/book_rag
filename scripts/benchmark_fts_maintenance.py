"""Measure FTS maintenance on an owned copy; never change a frozen control."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

QUERIES = ["ocean", "curiosity", '"natural selection"', "freedom", "society", "жизнь", "raison", "Verwandlung"]


def file_sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def file_identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def fingerprint(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for identity, value in connection.execute("SELECT id,sha256 FROM passages ORDER BY id"):
        digest.update(f"{identity}:{value}\n".encode())
    return digest.hexdigest()


def measure(connection: sqlite3.Connection, database: Path) -> dict[str, Any]:
    queries = []
    for query in QUERIES:
        started = time.perf_counter()
        hits = connection.execute(
            "SELECT p.id,bm25(passage_fts) score FROM passage_fts JOIN passages p ON p.id=passage_fts.rowid "
            "JOIN books b ON b.id=p.book_id WHERE passage_fts MATCH ? AND b.status='indexed' "
            "ORDER BY score,p.id LIMIT 50",
            (query,),
        ).fetchall()
        queries.append({"query": query, "hits": hits, "elapsed_ms": (time.perf_counter() - started) * 1000})
    return {
        "database_bytes": database.stat().st_size,
        "indexed_books": connection.execute("SELECT COUNT(*) FROM books WHERE status='indexed'").fetchone()[0],
        "fts_rows": connection.execute("SELECT COUNT(*) FROM passage_fts_docsize").fetchone()[0],
        "passages": connection.execute("SELECT COUNT(*) FROM passages").fetchone()[0],
        "passage_fingerprint": fingerprint(connection),
        "foreign_key_errors": connection.execute("PRAGMA foreign_key_check").fetchall(),
        "queries": queries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output_dir.resolve()
    if output.exists() or output.is_relative_to(source.parent):
        parser.error("Use a new experiment directory outside the source library")
    report_path = args.report.resolve() if args.report else output / "report.json"
    if report_path.exists() or report_path.is_relative_to(source.parent):
        parser.error("Use a new report outside the source library")
    wal = source.with_name(source.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        parser.error("This bounded experiment requires a static source with an empty/absent WAL")
    source_stamp = file_identity(source)
    source_sha = file_sha(source)
    existing_parent = output.parent
    while not existing_parent.exists():
        existing_parent = existing_parent.parent
    if shutil.disk_usage(existing_parent).free < 4 * 1024**3 + source_stamp[2] * 3:
        parser.error("Insufficient experiment space above the 4 GiB reserve")
    output.mkdir(parents=True)
    database = output / "library.sqlite3"
    shutil.copyfile(source, database)
    if (
        file_identity(source) != source_stamp
        or (wal.exists() and wal.stat().st_size)
        or file_sha(database) != source_sha
    ):
        raise RuntimeError("Source changed during capture; use a frozen control")
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=DELETE")
    before = measure(connection, database)
    started = time.perf_counter()
    connection.execute("INSERT INTO passage_fts(passage_fts) VALUES ('optimize')")
    connection.commit()
    connection.execute("VACUUM")
    optimized_seconds = time.perf_counter() - started
    optimized = measure(connection, database)
    started = time.perf_counter()
    # Contentless FTS has no external content source for its rebuild command.
    # Explicitly republish unchanged IDs/text/metadata from the canonical tables.
    with connection:
        connection.execute("INSERT INTO passage_fts(passage_fts) VALUES ('delete-all')")
        connection.execute(
            "INSERT INTO passage_fts(rowid,text,title,author,section_title) "
            "SELECT p.id,p.text,b.title,CASE WHEN a.section_author IS NOT NULL AND a.section_author!=b.author "
            "THEN b.author||'; '||a.section_author ELSE b.author END,s.title "
            "FROM passages p JOIN books b ON b.id=p.book_id JOIN sections s ON s.id=p.section_id "
            "LEFT JOIN section_roles a ON a.section_id=s.id WHERE b.status='indexed'"
        )
        connection.execute("INSERT INTO passage_fts(passage_fts) VALUES ('integrity-check')")
    connection.execute("VACUUM")
    rebuilt_seconds = time.perf_counter() - started
    rebuilt = measure(connection, database)
    connection.close()
    assert before["passage_fingerprint"] == optimized["passage_fingerprint"] == rebuilt["passage_fingerprint"]
    unchanged_ids = all(
        [hit[0] for hit in left["hits"]] == [hit[0] for hit in right["hits"]]
        for left, right in zip(before["queries"], rebuilt["queries"], strict=True)
    )
    unchanged_scores = all(
        left["hits"] == right["hits"] for left, right in zip(before["queries"], rebuilt["queries"], strict=True)
    )
    assert file_sha(source) == source_sha and file_identity(source) == source_stamp
    assert not wal.exists() or not wal.stat().st_size
    report: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "source_sha256": source_sha,
        "source_unchanged": True,
        "sqlite_version": sqlite3.sqlite_version,
        "before": before,
        "optimize_vacuum": optimized | {"maintenance_seconds": optimized_seconds},
        "explicit_rebuild_vacuum": rebuilt | {"maintenance_seconds": rebuilt_seconds},
        "probe_ranking_ids_identical": unchanged_ids,
        "probe_scores_identical": unchanged_scores,
        "limitations": [
            "Eight structural/ranking probes, not a relevance evaluation or held-out test.",
            "One frozen source snapshot; larger live-library capacity is not established.",
            "Owned database only; no production maintenance or default change.",
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {key: report[key] for key in ("source_unchanged", "probe_ranking_ids_identical", "probe_scores_identical")}
        )
    )
    print(
        json.dumps(
            {key: report[key]["database_bytes"] for key in ("before", "optimize_vacuum", "explicit_rebuild_vacuum")}
        )
    )


if __name__ == "__main__":
    main()
