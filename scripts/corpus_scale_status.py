#!/usr/bin/env python3
"""Read-only queue/download/extract/index receipt with a measured-rate ETA."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def snapshot(manifest: Path, data_dir: Path) -> dict[str, Any]:
    entries = json.loads(manifest.read_text())
    identities = json.dumps([e["id"] for e in entries])
    state_path = manifest.with_suffix(".run-state.json")
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    pid = state.get("pid")
    process = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True, check=False)
    alive = "scripts/scale_corpus.py" in process.stdout and str(manifest) in process.stdout
    now = datetime.now(UTC)
    with sqlite3.connect(
        "file:" + str((data_dir / "library.sqlite3").resolve()) + "?mode=ro", uri=True, timeout=30
    ) as db:
        indexed, groups = db.execute(
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
        first = db.execute(
            "SELECT MIN(indexed_at) FROM books WHERE status='indexed' AND id IN(SELECT value FROM json_each(?))",
            (identities,),
        ).fetchone()[0]
        start = max(datetime.fromisoformat(first).replace(tzinfo=UTC), now - timedelta(minutes=5)) if first else now
        recent = db.execute(
            "SELECT COUNT(*) FROM books WHERE status='indexed' AND indexed_at>=? "
            "AND id IN(SELECT value FROM json_each(?))",
            (start.strftime("%Y-%m-%d %H:%M:%S"), identities),
        ).fetchone()[0]
    seconds = max(1, (now - start).total_seconds())
    rate = recent / seconds
    running = alive and state.get("stage") == "running"
    return {
        "observed_at": now.isoformat(),
        "pid": pid,
        "process_alive": alive,
        "stage": state.get("stage", "unknown"),
        "run_state_updated_at": state.get("updated_at"),
        "last_completed_ordinal": state.get("ordinal"),
        "last_completed_book_id": state.get("last_result", {}).get("id"),
        "queue": str(manifest),
        "queued_candidates": len(entries),
        "downloaded_source_available": stages.get("downloaded", 0),
        "extracted": stages.get("processed", 0),
        "indexed_from_queue": statuses.get("indexed", 0),
        "canonical_duplicates": statuses.get("duplicate", 0),
        "failed_current": statuses.get("failed", 0),
        "deferred_current": statuses.get("deferred", 0),
        "not_yet_registered": len(entries) - sum(statuses.values()),
        "registered_statuses": statuses,
        "searchable_editions": indexed,
        "searchable_candidate_workgroups": groups,
        "confirmed_unique_work_claim": False,
        "free_bytes": shutil.disk_usage(data_dir).free,
        "measured_rate": {
            "window_seconds": round(seconds, 2),
            "indexed_in_window": recent,
            "books_per_minute": round(rate * 60, 2),
        },
        "eta_to_10000_candidate_groups_seconds": round(max(0, 10000 - groups) / rate) if running and rate > 0 else None,
        "eta_scope": "Extrapolated only from observed recent indexing rate; no estimate while runner stopped/deferred",
        "resume_skipped_previously_indexed": state.get("completed", {}).get("already_indexed_untouched", 0),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, default=Path("data/catalog/scale10000-v2/queue.json"))
    p.add_argument("--data-dir", type=Path, default=Path("data/full"))
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    result = snapshot(a.manifest, a.data_dir)
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = a.output.with_suffix(a.output.suffix + ".part")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(a.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
