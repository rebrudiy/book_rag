#!/usr/bin/env python3
"""Reconstruct completed bulk-batch denominators from actual receipts and read-only indexed timestamps."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def summarize(plan_path: Path, database: Path) -> dict:
    plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    entries = json.loads(plan_path.with_suffix(".local-manifest.json").read_text())
    groups: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        receipt = entry["bulk_acquisition"]
        if receipt["plan_sha256"] == plan_sha:
            groups[receipt["retrieved_at"]].append(entry)
    input_sets = {
        frozenset(path.read_text().splitlines()): path
        for path in plan_path.parent.glob(plan_path.stem + ".batch-*.files-from.txt")
    }
    ids = json.dumps([entry["id"] for entries_in_batch in groups.values() for entry in entries_in_batch])
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        db.execute("BEGIN")
        rows = {
            r[0]: {"status": r[1], "indexed_at": r[2], "stage_updated_at": r[3]}
            for r in db.execute(
                "SELECT b.id,b.status,b.indexed_at,p.updated_at FROM books b "
                "JOIN source_pipeline p ON p.book_id=b.id WHERE b.id IN(SELECT value FROM json_each(?))",
                (ids,),
            )
        }
        global_books, global_groups = db.execute(
            "SELECT COUNT(*),COUNT(DISTINCT work_id) FROM books WHERE status='indexed'"
        ).fetchone()
        db.rollback()
    batches: list[dict[str, Any]] = []
    for received_at, group in sorted(groups.items()):
        paths = frozenset(entry["bulk_acquisition"]["remote_path"] for entry in group)
        source_list = input_sets.get(paths)
        if source_list is None:
            continue
        observed = [rows[entry["id"]] for entry in group if entry["id"] in rows]
        if len(observed) != len(group) or any(
            row["status"] not in {"indexed", "failed", "duplicate"} for row in observed
        ):
            continue
        start = datetime.fromtimestamp(source_list.stat().st_mtime, UTC)
        transferred = datetime.fromisoformat(received_at)
        timestamp = max(datetime.fromisoformat(row["stage_updated_at"]).replace(tzinfo=UTC) for row in observed)
        upper = timestamp + timedelta(seconds=1)
        if upper <= start or transferred < start or upper < transferred:
            continue  # A rewritten input/resume cannot establish the original stopwatch interval.
        lower = max(timestamp, transferred, start)
        indexed = sum(row["status"] == "indexed" for row in observed)
        batches.append(
            {
                "files_from": str(source_list),
                "requested_file_count": len(paths),
                "received_source_bytes": sum(entry["bulk_acquisition"]["bytes"] for entry in group),
                "indexed_editions": indexed,
                "duplicate_editions": sum(row["status"] == "duplicate" for row in observed),
                "failed_editions": sum(row["status"] == "failed" for row in observed),
                "filelist_created_at": start.isoformat(),
                "receiver_completed_at": transferred.isoformat(),
                "processing_finished_lower_at": lower.isoformat(),
                "processing_finished_upper_at": upper.isoformat(),
                "transfer_wall_interval_seconds": round((transferred - start).total_seconds(), 3),
                "end_to_search_seconds_lower": round((lower - start).total_seconds(), 3),
                "end_to_search_seconds_upper": round((upper - start).total_seconds(), 3),
            }
        )
    summary: dict[str, Any] = {
        "complete_batches": len(batches),
        "indexed_editions": sum(b["indexed_editions"] for b in batches),
    }
    if len(batches) >= 2:
        start = datetime.fromisoformat(batches[0]["filelist_created_at"])
        lower = datetime.fromisoformat(batches[-1]["processing_finished_lower_at"])
        upper = datetime.fromisoformat(batches[-1]["processing_finished_upper_at"])
        summary.update(
            received_source_bytes=sum(b["received_source_bytes"] for b in batches),
            wall_elapsed_seconds_lower=round((lower - start).total_seconds(), 3),
            wall_elapsed_seconds_upper=round((upper - start).total_seconds(), 3),
            indexed_editions_per_minute_lower=round(
                summary["indexed_editions"] * 60 / (upper - start).total_seconds(), 2
            ),
            indexed_editions_per_minute_upper=round(
                summary["indexed_editions"] * 60 / (lower - start).total_seconds(), 2
            ),
        )
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "plan_sha256": plan_sha,
        "scope": "Completed receiver groups from local manifest only; current transfer/index phase is excluded",
        "timestamp_precision": (
            "Source pipeline SQLite processing timestamps have one-second resolution; "
            "intervals are shown as bounds, not an exact commit stopwatch"
        ),
        "summary": summary,
        "batches": batches,
        "current_searchable_editions": global_books,
        "current_candidate_workgroups": global_groups,
        "unique_work_authority_promotion": False,
        "database_writes": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=Path("data/catalog/bulk-ibiblio-v1/plan.json"))
    parser.add_argument("--database", type=Path, default=Path("data/full/library.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("data/catalog/bulk-ibiblio-v1/batch-measurements.json"))
    args = parser.parse_args()
    report = summarize(args.plan, args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".part")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(args.output)
    print(json.dumps({k: v for k, v in report.items() if k != "batches"}, indent=2))


if __name__ == "__main__":
    main()
