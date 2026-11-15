#!/usr/bin/env python3
"""Plan/probe selected official rsync sources; payload transfer needs --run and an idle shared lock."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import time
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_rag.bulk_sources import (
    OFFICIAL_ORIGINS,
    _save,
    _sha,
    cache_root,
    manifest_entry,
    prepare_bulk_plan,
    publish_received,
    registered_ids,
    transfer_command,
    verified_bulk_source,
)

PROVIDER_HOSTS = {"www.gutenberg.org", "gutenberg.org", "gutenberg.pglaf.org", "rsync.ibiblio.org"}


def provider_retry_at(database: Path) -> float:
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='source_deferrals'").fetchone():
            return 0
        return max(
            (float(t) for h, t in db.execute("SELECT host,retry_at FROM source_deferrals") if h in PROVIDER_HOSTS),
            default=0,
        )


def probe(plan_path: Path, database: Path, *, binary: str = "/usr/bin/rsync") -> dict[str, Any]:
    """A single exact-file metadata listing, never a payload download."""
    plan = json.loads(plan_path.read_text())
    if plan["origin"] not in OFFICIAL_ORIGINS:
        raise ValueError("Unapproved origin")
    retry_at = provider_retry_at(database)
    if retry_at > time.time():
        raise ValueError(f"Provider deferred until epoch {retry_at}; no probe was sent")
    if not plan["entries"]:
        raise ValueError("No selected source is available for a metadata probe")
    item = plan["entries"][0]
    command = [
        binary,
        "--list-only",
        "--no-motd",
        "--timeout=15",
        "--contimeout=10",
        plan["origin"] + item["remote"]["path"],
    ]
    started = datetime.now(UTC).isoformat()
    result = subprocess.run(command, capture_output=True, text=True, timeout=25, check=False)
    expected_name = Path(item["remote"]["path"]).name
    matched = False
    if len(result.stdout) <= 65536 and len(result.stderr) <= 65536:
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 5 and fields[-1] in {expected_name, item["remote"]["path"]}:
                matched |= (
                    fields[1].replace(",", "").isdigit() and int(fields[1].replace(",", "")) == item["remote"]["bytes"]
                )
    receipt = {
        "plan_sha256": _sha(plan_path),
        "origin": plan["origin"],
        "remote_path": item["remote"]["path"],
        "expected_bytes": item["remote"]["bytes"],
        "command": command,
        "started_at": started,
        "completed_at": datetime.now(UTC).isoformat(),
        "returncode": result.returncode,
        "stdout": result.stdout[:65536],
        "stderr": result.stderr[:65536],
        "reachable_exact_file": result.returncode == 0 and matched,
        "payload_downloaded": False,
    }
    _save(plan_path.with_suffix(".reachability.json"), receipt)
    return receipt


def run(
    plan_path: Path,
    data_dir: Path,
    *,
    allow_transfer: bool = False,
    batch_size: int = 128,
    max_books: int | None = None,
    binary: str = "/usr/bin/rsync",
) -> dict[str, Any]:
    if not allow_transfer:
        raise ValueError("Payload transfer requires explicit --run after coordinated handoff")
    if not 1 <= batch_size <= 256 or (max_books is not None and max_books < 1):
        raise ValueError("Invalid bounded batch/pilot size")
    data_dir = data_dir.absolute()
    database = data_dir / "library.sqlite3"
    plan_sha = _sha(plan_path)
    plan = json.loads(plan_path.read_text())
    reachability = json.loads(plan_path.with_suffix(".reachability.json").read_text())
    if (
        not reachability.get("reachable_exact_file")
        or reachability.get("plan_sha256") != plan_sha
        or reachability.get("origin") != plan["origin"]
    ):
        raise ValueError("A successful matching metadata-only reachability receipt is required before transfer")
    state_path = plan_path.with_suffix(".run-state.json")
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    if previous and previous.get("plan_sha256") != plan_sha:
        raise ValueError("Existing acquisition state belongs to another immutable bulk plan")
    if max(provider_retry_at(database), float(previous.get("retry_at_epoch", 0))) > time.time():
        raise ValueError("Provider remains deferred; no transfer was sent")
    with (data_dir / ".gutenberg-acquisition.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                "Another Gutenberg acquirer holds the shared lock; current pipeline was left running"
            ) from error
        # Import/construct only after the real existing acquirer lock is acquired.
        from book_rag.library import Library

        library = Library(data_dir)
        root = cache_root(data_dir, plan["origin"])
        staging = root / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        completed: Counter[str] = Counter()
        transfer_seconds = 0.0
        import_seconds = 0.0
        state: dict[str, Any] = {
            "pid": os.getpid(),
            "plan_sha256": plan_sha,
            "stage": "running",
            "origin": plan["origin"],
            "started_at": datetime.now(UTC).isoformat(),
            "retrieval_window_started_at": previous.get("retrieval_window_started_at", datetime.now(UTC).isoformat()),
            "selected": len(plan["entries"]),
            "max_books_this_run": max_books,
        }
        _save(state_path, state)
        local_manifest = plan_path.with_suffix(".local-manifest.json")
        previous_entries = json.loads(local_manifest.read_text()) if local_manifest.exists() else []
        allowed_ids = {item["entry"]["id"] for item in plan["entries"]}
        if any(
            e.get("id") not in allowed_ids or e.get("bulk_acquisition", {}).get("plan_sha256") != plan_sha
            for e in previous_entries
        ):
            raise ValueError("Existing local manifest contains source identities from another bulk plan")
        derived: dict[str, dict[str, Any]] = {entry["id"]: entry for entry in previous_entries}
        processed = 0
        try:
            for offset in range(0, len(plan["entries"]), batch_size):
                if max_books is not None and processed >= max_books:
                    state["stage"] = "pilot_limit_complete"
                    break
                retry_at = provider_retry_at(database)
                if retry_at > time.time():
                    state.update(stage="deferred", retry_at_epoch=retry_at)
                    break
                current = registered_ids(database)
                indexed = registered_ids(database, indexed_only=True)
                items = []
                for item in plan["entries"][offset : offset + batch_size]:
                    identity = item["entry"]["id"]
                    if identity in indexed:
                        completed["indexed_untouched"] += 1
                        continue
                    path = verified_bulk_source(item, root, plan["origin"], plan_sha)
                    if identity in current and path is None:
                        completed["existing_registered_untouched"] += 1
                        continue
                    items.append(item)
                if max_books is not None:
                    items = items[: max_books - processed]
                if not items:
                    continue
                missing = [i for i in items if verified_bulk_source(i, root, plan["origin"], plan_sha) is None]
                free = shutil.disk_usage(data_dir).free
                batch_forecast = sum(i["estimated_added_bytes"] for i in items)
                batch_payload = sum(i["remote"]["bytes"] for i in missing)
                if free < int(4.5 * 1024**3) + batch_forecast + 2 * batch_payload:
                    state.update(stage="stopped_disk_reserve", free_bytes=free)
                    break
                if missing:
                    files_from = plan_path.parent / f"{plan_path.stem}.batch-{offset:05d}.files-from.txt"
                    files_from.write_text("".join(i["remote"]["path"] + "\n" for i in missing))
                    command = transfer_command(plan["origin"], files_from, staging, binary=binary)
                    state.update(
                        stage="transferring",
                        batch_offset=offset,
                        batch_files=len(missing),
                        command=command,
                        batch_payload_bytes=batch_payload,
                        phase_started_at=datetime.now(UTC).isoformat(),
                    )
                    _save(state_path, state)
                    print(
                        json.dumps({"stage": "transferring", "files": len(missing), "bytes": batch_payload}), flush=True
                    )
                    transfer_started = time.perf_counter()
                    try:
                        with (plan_path.parent / "rsync-transfer.log").open("ab") as log:
                            try:
                                transfer = subprocess.run(
                                    command, stdout=log, stderr=subprocess.STDOUT, timeout=1800, check=False
                                )
                                code = transfer.returncode
                            except subprocess.TimeoutExpired:
                                code = -1
                    finally:
                        transfer_seconds += time.perf_counter() - transfer_started
                    if code:
                        retry_at = time.time() + 3600
                        for host in PROVIDER_HOSTS:
                            library.defer_source("https://" + host + "/", retry_at)
                        state.update(stage="transfer_failed_deferred", returncode=code, retry_at_epoch=retry_at)
                        break
                    ended = datetime.now(UTC).isoformat()
                    for item in missing:
                        publish_received(
                            item,
                            staging,
                            root,
                            plan,
                            plan_sha,
                            retrieval_window_started_at=state["retrieval_window_started_at"],
                            transfer_completed_at=ended,
                        )
                        completed["downloaded"] += 1
                state["stage"] = "indexing_cached"
                for item in items:
                    identity = item["entry"]["id"]
                    if identity in registered_ids(database, indexed_only=True):
                        completed["indexed_untouched"] += 1
                        continue
                    entry = manifest_entry(item, root, plan, plan_sha)
                    marker = Path(entry["local_path"]).with_suffix(".epub.ingest-attempt.json")
                    owned_attempt = json.loads(marker.read_text()) if marker.is_file() else {}
                    if identity in registered_ids(database) and (
                        owned_attempt.get("plan_sha256") != plan_sha or owned_attempt.get("book_id") != identity
                    ):
                        completed["existing_registered_untouched"] += 1
                        continue
                    if shutil.disk_usage(data_dir).free < 4 * 1024**3 + max(
                        512 * 1024**2, 2 * item["estimated_added_bytes"]
                    ):
                        state["stage"] = "stopped_disk_reserve"
                        break
                    _save(
                        marker,
                        {"plan_sha256": plan_sha, "book_id": identity, "attempted_at": datetime.now(UTC).isoformat()},
                    )
                    import_started = time.perf_counter()
                    try:
                        result = library.import_book(Path(entry["local_path"]), entry)
                    except Exception as error:
                        result = {"id": identity, "status": "failed", "error_type": type(error).__name__}
                    finally:
                        import_seconds += time.perf_counter() - import_started
                    completed[result["status"]] += 1
                    processed += 1
                    derived[identity] = entry
                    state.update(
                        last_result=result,
                        completed=dict(completed),
                        processed_this_run=processed,
                        free_bytes=shutil.disk_usage(data_dir).free,
                        updated_at=datetime.now(UTC).isoformat(),
                    )
                    _save(state_path, state)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                _save(local_manifest, list(derived.values()))
                if state["stage"] == "stopped_disk_reserve":
                    break
                state["stage"] = "running"
            else:
                state["stage"] = "queue_complete"
        except KeyboardInterrupt:
            state["stage"] = "interrupted_resumable"
        except Exception as error:
            state.update(stage="stopped_validation_error", error_type=type(error).__name__, error=str(error))
        finally:
            _save(local_manifest, list(derived.values()))
            state.update(
                completed=dict(completed),
                processed_this_run=processed,
                elapsed_seconds=round(time.perf_counter() - started, 3),
                transfer_seconds=round(transfer_seconds, 3),
                import_seconds=round(import_seconds, 3),
                free_bytes=shutil.disk_usage(data_dir).free,
                updated_at=datetime.now(UTC).isoformat(),
            )
            _save(state_path, state)
            print(json.dumps(state, ensure_ascii=False), flush=True)
        return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=Path("data/catalog/scale10000-v2/queue.json"))
    parser.add_argument("--metadata", type=Path, default=Path("data/transport-research/pg-generated-metadata-v2.json"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/full"))
    parser.add_argument("--plan", type=Path, default=Path("data/catalog/bulk-generated-v1/plan.json"))
    parser.add_argument("--limit", type=int, default=6500)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--max-books", type=int)
    parser.add_argument("--probe", action="store_true", help="Exact-file rsync metadata listing only")
    parser.add_argument("--run", action="store_true", help="Payload+index; requires root-coordinated idle shared lock")
    args = parser.parse_args()
    plan = prepare_bulk_plan(args.queue, args.metadata, args.data_dir / "library.sqlite3", args.plan, limit=args.limit)
    print(json.dumps({k: v for k, v in plan.items() if k != "entries"}, ensure_ascii=False), flush=True)
    if args.probe:
        print(json.dumps(probe(args.plan, args.data_dir / "library.sqlite3")), flush=True)
    if args.run:
        result = run(
            args.plan, args.data_dir, allow_transfer=True, batch_size=args.batch_size, max_books=args.max_books
        )
        if result["stage"] not in {"queue_complete", "pilot_limit_complete"}:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
