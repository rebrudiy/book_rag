"""Compare title-prefix and balanced thematic-map selection using metadata only.

Reads one checkpointed SQLite source with immutable=1; never constructs Library,
loads book text/models, changes production schema, or contacts an API.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

ROUND_ROBIN_SQL = """
WITH ranked AS (
 SELECT id,title,author,language,edition,
        ROW_NUMBER() OVER (PARTITION BY language ORDER BY title,id) AS language_rank
 FROM books WHERE status='indexed'
)
SELECT id,title,author,language,edition FROM ranked
ORDER BY language_rank,language,title,id LIMIT ?
"""


def identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def inspect(directory: Path, limit: int = 500) -> dict[str, Any]:
    if not 1 <= limit <= 500:
        raise ValueError("Map inventory limit must be in 1..500")
    database = directory.expanduser().resolve() / "library.sqlite3"
    wal = database.with_name(database.name + "-wal")
    if wal.is_symlink() or (wal.exists() and wal.stat().st_size):
        raise ValueError("Use a checkpointed source; this metadata probe never checkpoints or writes it")
    before = identity(database)
    conn = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        available = {
            row["language"]: row["count"]
            for row in conn.execute(
                "SELECT language,COUNT(*) count FROM books WHERE status='indexed' GROUP BY language ORDER BY language"
            )
        }
        current = list(
            conn.execute("SELECT id,language FROM books WHERE status='indexed' ORDER BY title,id LIMIT ?", (limit,))
        )
        balanced = list(conn.execute(ROUND_ROBIN_SQL, (limit,)))
        current_counts = Counter(row["language"] for row in current)
        balanced_counts = Counter(row["language"] for row in balanced)
    finally:
        conn.close()
    if identity(database) != before or wal.is_symlink() or (wal.exists() and wal.stat().st_size):
        raise RuntimeError("Source changed during metadata inventory; discard this report")
    return {
        "method": "metadata-only immutable SQLite; title/id prefix versus language round-robin",
        "limit": limit,
        "indexed_editions": sum(available.values()),
        "available_by_language": available,
        "current_selected_by_language": dict(sorted(current_counts.items())),
        "balanced_selected_by_language": dict(sorted(balanced_counts.items())),
        "newly_represented_languages": sorted(set(balanced_counts) - set(current_counts)),
        "current_russian_ids": [row["id"] for row in current if row["language"] == "ru"],
        "balanced_russian_ids": [row["id"] for row in balanced if row["language"] == "ru"],
        "limitations": [
            "Balanced selection is not a proportional sample or an estimate of corpus topic prevalence.",
            "Within each language, title/id ordering still favors the alphabet's beginning.",
            "No book text, retrieval quality or theme weights are measured here.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/full"))
    parser.add_argument("--limit", type=int, default=500, choices=range(1, 501))
    parser.add_argument("--output", type=Path, default=Path("artifacts/explore-balanced-inventory.json"))
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.data_dir.expanduser().resolve()):
        parser.error("Write the report outside the production/source directory")
    if args.output.exists():
        parser.error("Choose a new output path; existing reports are retained")
    report = inspect(args.data_dir, args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
