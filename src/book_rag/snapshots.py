"""Consistent, atomic SQLite snapshots for backups and frozen experiments."""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_rag.library import Library


def create_snapshot(
    library: Library, destination: Path, book_ids: list[str] | None = None, include_saved: bool = True
) -> dict[str, Any]:
    """Back up a live WAL library and optionally retain only an indexed subset.

    Uses SQLite's online backup API, not a file copy. Never overwrites an
    existing destination, touches original artifacts, or copies model weights.
    """
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Snapshot destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".staging-" + uuid.uuid4().hex)
    staging.mkdir()
    try:
        with sqlite3.connect(staging / "library.sqlite3") as target:
            with library.connect() as source:
                source.backup(target, pages=1024, sleep=0.01)
        snapshot = Library(staging)
        with snapshot.connect() as conn:
            if book_ids is not None:
                keep = set(book_ids)
                indexed = {r[0] for r in conn.execute("SELECT id FROM books WHERE status='indexed'")}
                missing = keep - indexed
                if missing:
                    raise ValueError(f"Requested books are not indexed: {sorted(missing)}")
                conn.execute("CREATE TEMP TABLE keep_books(id TEXT PRIMARY KEY)")
                conn.executemany("INSERT INTO keep_books VALUES (?)", [(identity,) for identity in sorted(keep)])
                remove = [
                    r[0] for r in conn.execute("SELECT id FROM books WHERE id NOT IN (SELECT id FROM keep_books)")
                ]
                for book_id in remove:
                    snapshot._remove_index(conn, book_id)
                conn.execute("DELETE FROM books WHERE id NOT IN (SELECT id FROM keep_books)")
                conn.execute("DELETE FROM events WHERE book_id NOT IN (SELECT id FROM books)")
            if not include_saved:
                conn.execute("DELETE FROM saved")
            conn.execute("INSERT INTO passage_fts(passage_fts) VALUES ('integrity-check')")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("Snapshot foreign-key validation failed")
        if book_ids is not None:
            # Reclaim removed book/FTS pages so a small evaluation subset is compact.
            with snapshot.connect() as conn:
                conn.execute("INSERT INTO passage_fts(passage_fts) VALUES ('optimize')")
            with snapshot.connect() as conn:
                conn.execute("VACUUM")
        report = {
            "created_at": datetime.now(UTC).isoformat(),
            "source": str(library.data_dir.resolve()),
            "destination": str(destination),
            "stats": snapshot.stats(),
            "book_ids": sorted(book_ids) if book_ids is not None else None,
            "include_saved": include_saved,
            "format": "SQLite online backup; originals/models/vectors remain outside snapshot",
        }
        (staging / "snapshot.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        staging.rename(destination)
        return report
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
