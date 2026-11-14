"""Resume embeddings without migrating a frozen corpus database."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import resource
import sqlite3
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from book_rag.library import Library
from book_rag.semantic import build_index


class FrozenEmbeddingSource(Library):
    """Embedding-only source; SQL connections cannot migrate or write data."""

    def __init__(self, directory: Path) -> None:
        self.data_dir = directory.resolve()
        self.db_path = self.data_dir / "library.sqlite3"
        self.identity = self._identity()
        self._check()

    def _identity(self) -> tuple[int, int, int, int, int]:
        info = self.db_path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _check(self) -> None:
        wal = self.db_path.with_name(self.db_path.name + "-wal")
        if self.db_path.is_symlink() or (wal.exists() and wal.stat().st_size) or self._identity() != self.identity:
            raise ValueError("Use an unchanged frozen database with an empty or absent WAL")

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        self._check()
        connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            connection.close()
            self._check()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--resources", type=Path, required=True)
    args = parser.parse_args()
    torch = importlib.import_module("torch")

    source = FrozenEmbeddingSource(args.source)
    with source.db_path.open("rb") as stream:
        before = hashlib.file_digest(stream, "sha256").hexdigest()
    stopped = threading.Event()
    args.resources.parent.mkdir(parents=True, exist_ok=True)

    def sample() -> None:
        while not stopped.is_set():
            record = {
                "observed_at": datetime.now(UTC).isoformat(),
                "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1 if sys.platform == "darwin" else 1024),
            }
            if torch.backends.mps.is_available():
                record.update(
                    mps_current_bytes=torch.mps.current_allocated_memory(),
                    mps_driver_bytes=torch.mps.driver_allocated_memory(),
                    mps_recommended_bytes=torch.mps.recommended_max_memory(),
                )
            with args.resources.open("a") as stream:
                stream.write(json.dumps(record) + "\n")
            stopped.wait(30)

    worker = threading.Thread(target=sample, daemon=True)
    worker.start()
    try:
        result = build_index(source, batch_size=args.batch_size)
        with source.db_path.open("rb") as stream:
            after = hashlib.file_digest(stream, "sha256").hexdigest()
        if before != after:
            raise RuntimeError("Frozen corpus database changed during embedding")
        result["source_database_sha256"] = after
        result["source_database_unchanged"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        stopped.set()
        worker.join(timeout=2)


if __name__ == "__main__":
    main()
