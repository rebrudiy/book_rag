"""Operational receipts never claim a stopped/deferred acquisition is active."""

from __future__ import annotations

import hashlib
import json
import runpy
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

snapshot = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/corpus_scale_status.py"))["snapshot"]


@pytest.mark.parametrize("alive,stage", [(False, "running"), (True, "deferred"), (True, "running")])
def test_receipt_tracks_distinct_stages_without_changing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alive: bool, stage: str
) -> None:
    manifest = tmp_path / "queue.json"
    manifest.write_text(json.dumps([{"id": "pg1-full"}, {"id": "pg2-full"}, {"id": "pg3-full"}]))
    manifest.with_suffix(".run-state.json").write_text(
        json.dumps({"pid": 123, "stage": stage, "completed": {"already_indexed_untouched": 1}})
    )
    database = tmp_path / "library.sqlite3"
    when = (datetime.now(UTC) - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE books(id,work_id,status,indexed_at)")
        db.execute("CREATE TABLE events(book_id,stage)")
        db.executemany(
            "INSERT INTO books VALUES(?,?,?,?)",
            [("pg1-full", "candidate1", "indexed", when), ("pg2-full", "candidate2", "failed", None)],
        )
        db.executemany(
            "INSERT INTO events VALUES(?,?)",
            [("pg1-full", "downloaded"), ("pg1-full", "downloaded"), ("pg1-full", "processed")],
        )
    original = hashlib.sha256(database.read_bytes()).hexdigest()
    command = f"python scripts/scale_corpus.py --manifest {manifest}" if alive else ""
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=command))
    result = snapshot(manifest, tmp_path)
    assert result["process_alive"] is alive
    assert result["downloaded_source_available"] == result["extracted"] == result["indexed_from_queue"] == 1
    assert result["failed_current"] == result["not_yet_registered"] == 1
    assert result["resume_skipped_previously_indexed"] == 1
    assert result["confirmed_unique_work_claim"] is False
    if alive and stage == "running":
        assert result["eta_to_10000_candidate_groups_seconds"] > 0
    else:
        assert result["eta_to_10000_candidate_groups_seconds"] is None
    assert hashlib.sha256(database.read_bytes()).hexdigest() == original


def test_missing_database_is_not_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "queue.json"
    manifest.write_text("[]")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=""))
    with pytest.raises(sqlite3.OperationalError):
        snapshot(manifest, tmp_path)
    assert not (tmp_path / "library.sqlite3").exists()
