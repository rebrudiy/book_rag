#!/usr/bin/env python3
"""Read-only actual bulk progress, including staged bytes before batch indexing."""

from __future__ import annotations

import argparse
import fcntl
import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from book_rag.bulk_sources import cache_root


def snapshot(plan_path: Path, data_dir: Path) -> dict:
    plan = json.loads(plan_path.read_text())
    state_path = plan_path.with_suffix(".run-state.json")
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    identities = json.dumps([item["entry"]["id"] for item in plan["entries"]])
    process = subprocess.run(
        ["ps", "-p", str(state.get("pid")), "-o", "args="], capture_output=True, text=True, check=False
    )
    root = cache_root(data_dir, plan["origin"])
    staging = root / ".staging"
    staged_files = []
    if staging.exists():
        for path in staging.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    staged_files.append(path.stat().st_size)
            except FileNotFoundError:
                # Atomic promotion may move a file between this listing/stat.
                continue
    with closing(sqlite3.connect((data_dir / "library.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        books, groups = db.execute(
            "SELECT COUNT(*),COUNT(DISTINCT work_id) FROM books WHERE status='indexed'"
        ).fetchone()
        statuses = dict(
            db.execute(
                "SELECT status,COUNT(*) FROM books WHERE id IN(SELECT value FROM json_each(?)) GROUP BY status",
                (identities,),
            )
        )
        stages = dict(
            db.execute(
                "SELECT stage,COUNT(DISTINCT book_id) FROM events "
                "WHERE book_id IN(SELECT value FROM json_each(?)) GROUP BY stage",
                (identities,),
            )
        )
        languages = dict(db.execute("SELECT language,COUNT(*) FROM books WHERE status='indexed' GROUP BY language"))
        db.rollback()
    locked = False
    with (data_dir / ".gutenberg-acquisition.lock").open("r") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
        except BlockingIOError:
            locked = True
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "pid": state.get("pid"),
        "process_alive": "scripts/run_bulk_sources.py" in process.stdout and "--run" in process.stdout,
        "provider_lock_held": locked,
        "stage": state.get("stage", "unknown"),
        "last_state_update": state.get("updated_at", state.get("started_at")),
        "phase_started_at": state.get("phase_started_at"),
        "batch_offset": state.get("batch_offset"),
        "batch_files": state.get("batch_files"),
        "batch_payload_bytes": state.get("batch_payload_bytes"),
        "staged_files_present": len(staged_files),
        "staged_logical_bytes_present": sum(staged_files),
        "staging_caveat": (
            "Observed local file sizes, including partial files; "
            "not promoted source receipts or measured network byte counts"
        ),
        "selected_plan_editions": len(plan["entries"]),
        "registered_statuses_from_plan": statuses,
        "downloaded_from_plan": stages.get("downloaded", 0),
        "extracted_from_plan": stages.get("processed", 0),
        "indexed_from_plan": statuses.get("indexed", 0),
        "failed_from_plan": statuses.get("failed", 0),
        "canonical_duplicates_from_plan": statuses.get("duplicate", 0),
        "searchable_editions": books,
        "searchable_candidate_workgroups": groups,
        "confirmed_unique_work_claim": False,
        "searchable_by_language": languages,
        "free_bytes": shutil.disk_usage(data_dir).free,
        "completed_this_run": state.get("completed", {}),
        "eta_seconds": None,
        "eta_note": (
            "This lightweight snapshot does not estimate completion; "
            "summarize_bulk_batches.py reports actual completed-batch throughput"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=Path("data/catalog/bulk-generated-v1/plan.json"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/full"))
    parser.add_argument("--output", type=Path, default=Path("data/catalog/bulk-generated-v1/status-latest.json"))
    args = parser.parse_args()
    result = snapshot(args.plan, args.data_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".part")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
