#!/usr/bin/env python3
"""Rebuild an offline manifest in an isolated scratch index and measure costs.

Never downloads files, alters the live library, or needs optional models. The
reported RSS is process lifetime peak; run in a fresh process for each report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, TypedDict

from book_rag.library import Library
from book_rag.sources import load_manifest


class Artifact(TypedDict):
    entry: dict[str, Any]
    path: Path
    bytes: int
    sha256: str


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("corpus/manifest.json"))
    parser.add_argument("--downloads", type=Path, default=Path("data/full/downloads"))
    parser.add_argument("--output", type=Path, default=Path("experiments/index20-v1.json"))
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("limit must be positive")
    entries = load_manifest(args.manifest)[: args.limit]
    artifacts: list[Artifact] = []
    for entry in entries:
        path = (
            Path(entry["local_path"])
            if entry.get("local_path")
            else args.downloads / f"{entry['id']}.{entry['format']}"
        )
        if not path.is_file():
            parser.error(f"Download corpus first; missing {path}")
        with path.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        artifacts.append({"entry": entry, "path": path, "bytes": path.stat().st_size, "sha256": sha})
    with tempfile.TemporaryDirectory(prefix="bookrag-benchmark-") as scratch:
        library = Library(Path(scratch))
        started = time.perf_counter()
        timings = []
        for artifact in artifacts:
            one_started = time.perf_counter()
            library.import_book(artifact["path"], artifact["entry"])
            timings.append(
                {
                    "id": artifact["entry"]["id"],
                    "bytes": artifact["bytes"],
                    "sha256": artifact["sha256"],
                    "seconds": time.perf_counter() - one_started,
                }
            )
        indexing_seconds = time.perf_counter() - started
        stats = library.stats()
        queries = ["nature", "division of labour", '"White Rabbit"', "свобода", "philosophy", "zzxxyynotpresent"]
        latency = []
        verified = 0
        for _ in range(3):
            for query in queries:
                begin = time.perf_counter()
                result = library.search(query, mode="lexical")
                latency.append((time.perf_counter() - begin) * 1000)
                for hit in result["results"]:
                    book = library.get_book(hit["book_id"])
                    assert book is not None
                    section = next(s for s in book["sections"] if s["id"] == hit["section_id"])
                    assert section["text"][hit["start"] : hit["end"]] == hit["text"]
                    verified += 1
        latency.sort()
        disk_bytes = sum(p.stat().st_size for p in Path(scratch).glob("library.sqlite3*"))
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report = {
            "schema_version": 1,
            "purpose": "offline index cost; no relevance judgments",
            "python": platform.python_version(),
            "platform": platform.platform(),
            "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
            "stats": stats,
            "source_bytes": sum(a["bytes"] for a in artifacts),
            "index_disk_bytes_including_wal": disk_bytes,
            "indexing_seconds": indexing_seconds,
            "passages_per_second": stats["passages"] / indexing_seconds,
            "process_peak_rss_mib": rss / (1024**2 if sys.platform == "darwin" else 1024),
            "query_p50_ms": statistics.median(latency),
            "query_p95_ms": latency[int(0.95 * (len(latency) - 1))],
            "query_samples": len(latency),
            "independently_verified_results": verified,
            "books": timings,
            "limitations": [
                "No network, OCR, embedding build or model memory measured.",
                "The host may have other workloads; not a dedicated performance environment.",
                "RSS is process lifetime peak, not incremental allocator memory.",
            ],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "books"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
