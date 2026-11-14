"""Measure epoch-trigger write cost and guard latency in owned temporary libraries.

This opt-in benchmark uses original synthetic CC0 texts, no network or models,
and never opens a live/frozen library. It is outside pytest test discovery.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_rag import index_coverage
from book_rag.index_coverage import bind_coverage, capture_coverage, ensure_epoch_schema, validate_coverage
from book_rag.library import Library


def disable_owned_tracking(library: Library) -> None:
    with library.connect() as conn:
        for name in index_coverage._TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute("DROP TABLE IF EXISTS book_index_epochs")
        conn.execute("DROP TABLE IF EXISTS book_index_epoch_meta")


def write_sources(folder: Path, version: int, books: int, paragraphs: int) -> None:
    folder.mkdir(exist_ok=True)
    stem = "The reader follows the exact argument and keeps its source context for later comparison. "
    for identity in range(books):
        text = [
            (f"Original CC0 test edition {identity}, paragraph {ordinal}, revision {version}. " + stem * 16)[:1300]
            for ordinal in range(paragraphs)
        ]
        (folder / f"book-{identity}.txt").write_text("\n\n".join(text))


def import_sources(library: Library, sources: Path, books: int) -> float:
    started = time.perf_counter()
    for identity in range(books):
        library.import_book(
            sources / f"book-{identity}.txt",
            {"id": f"book-{identity}", "title": f"Owned book {identity}", "author": "CC0 benchmark", "language": "en"},
        )
    return time.perf_counter() - started


def guard_measurement(library: Library, samples: int = 50) -> dict[str, Any]:
    with library.connect() as conn:
        proof = capture_coverage(conn, kind="full")
    started = time.perf_counter()
    binding = bind_coverage(library, proof, receipt_sha256="a" * 64)
    bind_ms = (time.perf_counter() - started) * 1000
    timings = []
    for _ in range(samples):
        started = time.perf_counter()
        result = validate_coverage(library, proof, binding)
        timings.append((time.perf_counter() - started) * 1000)
        assert result["valid"]
    return {
        "books": proof["books"],
        "passages": proof["passages"],
        "samples": samples,
        "strict_bind_ms": round(bind_ms, 3),
        "guard_p50_ms": round(statistics.median(timings), 3),
        "guard_p95_ms": round(sorted(timings)[int((samples - 1) * 0.95)], 3),
    }


def add_owned_metadata_editions(library: Library, first: int, total: int) -> None:
    """Extend only the guard fixture; these rows do not simulate a ready index."""
    with library.connect() as conn:
        for identity in range(first, total):
            book_id = f"book-{identity}"
            text = f"Original CC0 guard metadata fixture edition {identity}."
            conn.execute(
                "INSERT INTO books(id,title,author,language,work_id,edition,source_url,rights,status) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (book_id, f"Owned book {identity}", "CC0 fixture", "en", book_id, "Test", "", "CC0", "indexed"),
            )
            cursor = conn.execute(
                "INSERT INTO sections(book_id,ordinal,title,text) VALUES(?,?,?,?)", (book_id, 0, "Test", text)
            )
            conn.execute(
                "INSERT INTO passages(book_id,section_id,start,end,text,sha256) VALUES(?,?,?,?,?,?)",
                (book_id, cursor.lastrowid, 0, len(text), text, hashlib.sha256(text.encode()).hexdigest()),
            )


def benchmark(*, books: int = 20, paragraphs: int = 1000, repeats: int = 3) -> dict[str, Any]:
    created_at = datetime.now(UTC).isoformat()
    implementation_hashes = {
        "index_coverage.py": hashlib.sha256(Path(index_coverage.__file__).read_bytes()).hexdigest(),
        "library.py": hashlib.sha256(Path("src/book_rag/library.py").read_bytes()).hexdigest(),
    }
    with tempfile.TemporaryDirectory(prefix="bookrag-epoch-benchmark-") as temporary:
        root = Path(temporary)
        sources = root / "sources"
        results = []
        guards = []
        # Alternate ordering across pairs, so file-cache/background load is not
        # systematically assigned to one condition. Each run gets a fresh DB.
        conditions = [
            condition for pair in range(repeats) for condition in ((False, True) if pair % 2 == 0 else (True, False))
        ]
        for run, tracked in enumerate(conditions):
            write_sources(sources, 1, books, paragraphs)
            library = Library(root / f"run-{run}")
            disable_owned_tracking(library)
            if tracked:
                with library.connect() as conn:
                    ensure_epoch_schema(conn)
            insert = import_sources(library, sources, books)
            write_sources(sources, 2, books, paragraphs)
            refresh = import_sources(library, sources, books)
            record = {
                "tracked": tracked,
                "insert_seconds": round(insert, 4),
                "refresh_seconds": round(refresh, 4),
                "passages": library.stats()["passages"],
            }
            results.append(record)
            print(json.dumps(record), flush=True)
            if tracked and run == max(index for index, value in enumerate(conditions) if value):
                guards.append(guard_measurement(library))
                if books < 1308:
                    add_owned_metadata_editions(library, books, 1308)
                    guards.append(guard_measurement(library))
        medians = {}
        for key in ("insert_seconds", "refresh_seconds"):
            baseline = statistics.median(row[key] for row in results if not row["tracked"])
            tracked_median = statistics.median(row[key] for row in results if row["tracked"])
            medians[key] = {
                "baseline": baseline,
                "tracked": tracked_median,
                "ratio": round(tracked_median / baseline, 4),
            }
        return {
            "schema_version": 1,
            "created_at": created_at,
            "method": "actual Library.import_book; original synthetic CC0 TXT; fresh owned DB per run",
            "environment": {"python": platform.python_version(), "platform": platform.platform()},
            "implementation_sha256": implementation_hashes,
            "books": books,
            "paragraphs_per_book": paragraphs,
            "paragraph_characters": 1300,
            "repeats_each": repeats,
            "trigger_count": len(index_coverage._TRIGGERS),
            "runs": results,
            "medians": medians,
            "guards": guards,
            "limitations": [
                "Synthetic repetitive vocabulary differs from a broad real corpus.",
                "Concurrent background work and warmed filesystem cache affect timings.",
                "Guard-only extra editions are minimal SQL fixtures, not an embedding-quality experiment.",
                "Receipt digest is an explicit test placeholder; this benchmark does not install an index.",
            ],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--books", type=int, default=20, choices=range(1, 51))
    parser.add_argument("--paragraphs", type=int, default=1000, choices=range(1, 5001))
    parser.add_argument("--repeats", type=int, default=3, choices=range(1, 6))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is not None and args.output.exists():
        parser.error("Choose a new output filename; existing reports are never overwritten")
    result = benchmark(books=args.books, paragraphs=args.paragraphs, repeats=args.repeats)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
