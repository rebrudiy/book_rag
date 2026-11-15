"""Completed receiver groups and timestamp precision stay distinct from active indexing."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUNNER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/summarize_bulk_batches.py"))


def fixture(tmp_path: Path, *, fast: bool = False) -> tuple[Path, Path]:
    plan = tmp_path / "plan.json"
    plan.write_text('{"entries":[]}')
    plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()
    times = [(0.8, 0.9, 0)] if fast else [(0, 5, 7), (10, 15, 17)]
    epoch = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    entries: list[dict[str, Any]] = []
    dbpath = tmp_path / "library.sqlite3"
    with sqlite3.connect(dbpath) as db:
        db.execute("CREATE TABLE books(id,status,indexed_at,work_id)")
        db.execute("CREATE TABLE source_pipeline(book_id,updated_at)")
        for ordinal, (started, received, finished) in enumerate(times):
            bid = f"pg{101 + ordinal}-full"
            remote = f"{101 + ordinal}/pg{101 + ordinal}.epub"
            source_list = tmp_path / f"plan.batch-{ordinal:05d}.files-from.txt"
            source_list.write_text(remote + "\n")
            os.utime(source_list, (epoch + started, epoch + started))
            stamp = datetime.fromtimestamp(epoch + finished, UTC).strftime("%Y-%m-%d %H:%M:%S")
            db.execute("INSERT INTO books VALUES(?,?,?,?)", (bid, "indexed", stamp, bid))
            db.execute("INSERT INTO source_pipeline VALUES(?,?)", (bid, stamp))
            entries.append(
                {
                    "id": bid,
                    "bulk_acquisition": {
                        "plan_sha256": plan_sha,
                        "remote_path": remote,
                        "bytes": 120,
                        "retrieved_at": datetime.fromtimestamp(epoch + received, UTC).isoformat(),
                    },
                }
            )
        # A currently indexing third source is visible globally, but not yet in
        # the completed local manifest. It must not inflate batch throughput.
        db.execute("INSERT INTO books VALUES('active','indexed','2026-01-01 00:00:22','active')")
    plan.with_suffix(".local-manifest.json").write_text(json.dumps(entries))
    return plan, dbpath


def test_complete_batches_exclude_current_phase_and_preserve_database(tmp_path: Path) -> None:
    plan, db = fixture(tmp_path)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    report = RUNNER["summarize"](plan, db)
    assert report["current_searchable_editions"] == 3
    assert report["summary"]["complete_batches"] == 2
    assert report["summary"]["indexed_editions"] == 2
    assert report["summary"]["received_source_bytes"] == 240
    assert report["summary"]["wall_elapsed_seconds_lower"] == 17
    assert report["summary"]["wall_elapsed_seconds_upper"] == 18
    assert report["summary"]["indexed_editions_per_minute_lower"] == 6.67
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    path = tmp_path / "plan.batch-00001.files-from.txt"
    future = datetime(2026, 1, 1, 0, 0, 25, tzinfo=UTC).timestamp()
    os.utime(path, (future, future))
    assert RUNNER["summarize"](plan, db)["summary"]["complete_batches"] == 1


def test_subsecond_completion_is_bounded_instead_of_discarded(tmp_path: Path) -> None:
    plan, db = fixture(tmp_path, fast=True)
    report = RUNNER["summarize"](plan, db)
    assert report["summary"]["complete_batches"] == 1
    batch = report["batches"][0]
    assert batch["end_to_search_seconds_lower"] == 0.1
    assert batch["end_to_search_seconds_upper"] == 0.2
