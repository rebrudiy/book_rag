#!/usr/bin/env python3
"""Plan and run a serial, resumable, disk-guarded public-source corpus expansion."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from book_rag.corpus_scale import _save, plan_scale_corpus
from book_rag.library import Library
from book_rag.sources import DownloadDeferred, cached_source, download_source, load_manifest


def counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect("file:" + str(db_path.resolve()) + "?mode=ro", uri=True, timeout=30) as db:
        books, groups = db.execute(
            "SELECT COUNT(*),COUNT(DISTINCT work_id) FROM books WHERE status='indexed'"
        ).fetchone()
    return {"searchable_editions": books, "searchable_candidate_workgroups": groups}


def run_queue(manifest: Path, data_dir: Path) -> dict[str, Any]:
    """Use public Library ingestion; never update an already indexed source ID."""
    library = Library(data_dir)
    entries = load_manifest(manifest)
    with sqlite3.connect("file:" + str(library.db_path.resolve()) + "?mode=ro", uri=True) as db:
        already = {r[0] for r in db.execute("SELECT id FROM books WHERE status='indexed'")}
    state_path = manifest.with_suffix(".run-state.json")
    start = time.perf_counter()
    last_network = 0.0
    completed: Counter[str] = Counter()
    state: dict[str, Any] = {
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
        "stage": "running",
        "queue": str(manifest),
        "queued": len(entries),
        "baseline": counts(library.db_path),
        "protected_existing_ids": len(already),
        "download_delay_seconds": 2.0,
        "minimum_free_gib": 4.0,
    }
    _save(state_path, state)
    print(json.dumps(state), flush=True)
    with (data_dir / ".gutenberg-acquisition.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            for ordinal, entry in enumerate(entries, 1):
                identity = entry["id"]
                if identity in already:
                    completed["already_indexed_untouched"] += 1
                    continue
                free = shutil.disk_usage(data_dir).free
                next_cost = int(entry.get("resource_forecast", {}).get("estimated_added_bytes", 0))
                if free < 4 * 1024**3 + max(512 * 1024**2, 2 * next_cost):
                    state.update(stage="stopped_disk_reserve", free_bytes=free)
                    break
                pending = max(
                    (
                        library.source_retry_at(url) or 0
                        for url in (entry["download_url"], "https://www.gutenberg.org/", "https://gutenberg.org/")
                    ),
                    default=0,
                )
                if pending > time.time():
                    state.update(stage="deferred", retry_at_epoch=pending)
                    break
                library.register(entry)
                try:
                    path = cached_source(entry, data_dir / "downloads")
                    if path is None:
                        time.sleep(max(0, 2.0 - (time.monotonic() - last_network)))
                        try:
                            path = download_source(entry, data_dir / "downloads")
                        finally:
                            last_network = time.monotonic()
                    result = library.import_book(path, entry)
                    completed[result["status"]] += 1
                except DownloadDeferred as error:
                    retry_at = (
                        error.retry_at.timestamp() if error.retry_at is not None else time.time() + error.retry_after
                    )
                    library.defer_source(entry["download_url"], retry_at)
                    library.defer_source(str(error.request.url), retry_at)
                    library.stage(identity, "deferred", str(error))
                    state.update(stage="deferred", retry_at_epoch=retry_at, error=str(error))
                    break
                except (httpx.TimeoutException, httpx.TransportError) as error:
                    retry_at = time.time() + 3600
                    library.defer_source(entry["download_url"], retry_at)
                    library.stage(identity, "deferred", str(error))
                    state.update(stage="deferred", retry_at_epoch=retry_at, error=str(error))
                    break
                except httpx.HTTPStatusError as error:
                    if error.response.status_code in {403, 429} or error.response.status_code >= 500:
                        retry_at = time.time() + 3600
                        library.defer_source(entry["download_url"], retry_at)
                        library.stage(identity, "deferred", str(error))
                        state.update(stage="deferred", retry_at_epoch=retry_at, error=str(error))
                        break
                    library.stage(identity, "failed", str(error))
                    completed["failed"] += 1
                    result = {"id": identity, "status": "failed", "error": str(error)}
                except Exception as error:
                    library.stage(identity, "failed", str(error))
                    completed["failed"] += 1
                    result = {"id": identity, "status": "failed", "error": str(error)}
                state.update(
                    ordinal=ordinal,
                    last_result=result,
                    completed=dict(completed),
                    free_bytes=shutil.disk_usage(data_dir).free,
                    elapsed_seconds=round(time.perf_counter() - start, 2),
                    updated_at=datetime.now(UTC).isoformat(),
                )
                if ordinal % 25 == 0 or completed["indexed"] <= 3:
                    state.update(counts(library.db_path))
                _save(state_path, state)
                print(
                    json.dumps(
                        {**result, "ordinal": ordinal, "completed": dict(completed), "free_bytes": state["free_bytes"]},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            else:
                state["stage"] = "queue_complete"
        except KeyboardInterrupt:
            state["stage"] = "interrupted_resumable"
        finally:
            state.update(
                completed=dict(completed),
                elapsed_seconds=round(time.perf_counter() - start, 2),
                updated_at=datetime.now(UTC).isoformat(),
                **counts(library.db_path),
            )
            _save(state_path, state)
            print(json.dumps(state, ensure_ascii=False), flush=True)
    return state


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--catalog", type=Path, default=Path("data/catalog/rdf-files.tar.bz2"))
    p.add_argument("--data-dir", type=Path, default=Path("data/full"))
    p.add_argument("--manifest", type=Path, default=Path("data/catalog/scale10000-v1/queue.json"))
    p.add_argument("--limit", type=int, default=7000)
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    print(json.dumps({"pid": os.getpid(), "stage": "planning", "manifest": str(args.manifest)}), flush=True)
    report = plan_scale_corpus(args.catalog, args.data_dir / "library.sqlite3", args.manifest, limit=args.limit)
    print(json.dumps({"stage": "planned", **report}, ensure_ascii=False), flush=True)
    if args.run:
        run_queue(args.manifest, args.data_dir.resolve())


if __name__ == "__main__":
    main()
