#!/usr/bin/env python3
"""Remove exactly two explicitly authorized, verified disposable benchmark DBs.

Frozen controls, public experiment receipts and runtime query proofs are retained.
No recursive directory deletion and no arbitrary path arguments are supported.
"""

from __future__ import annotations

import hashlib
import json
import runpy
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/snapshots/library1308-v1/library.sqlite3"
SPAN = ROOT / "data/benchmarks/span-storage1308-v1/library.sqlite3"
FTS = ROOT / "data/benchmarks/fts-maintenance1308-v1/library.sqlite3"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stamp(path: Path) -> tuple[int, int, int, int, int]:
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    part.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    part.replace(path)


def main() -> None:
    span_receipt = json.loads((ROOT / "experiments/span-storage-v1.json").read_text())
    fts_receipt = json.loads((ROOT / "experiments/fts-maintenance1308-v1.json").read_text())
    assert ROOT / span_receipt["source"] == SOURCE
    assert ROOT / span_receipt["owned_copy"] == SPAN
    expected_source = span_receipt["source_sha256"]
    assert expected_source == fts_receipt["source_sha256"]
    for p in (SOURCE, SPAN, FTS):
        assert p.is_file() and not p.is_symlink()
    assert stamp(SOURCE)[:2] != stamp(SPAN)[:2] and stamp(SOURCE)[:2] != stamp(FTS)[:2]
    assert SPAN.stat().st_size == span_receipt["storage"]["after_bytes"]
    assert FTS.stat().st_size == fts_receipt["explicit_rebuild_vacuum"]["database_bytes"]
    assert {p.name for p in FTS.parent.iterdir()} == {"library.sqlite3"}
    source_stamp = stamp(SOURCE)
    assert digest(SOURCE) == expected_source
    print(json.dumps({"stage": "original_control_sha_verified", "sha256": expected_source}), flush=True)
    records: list[dict[str, Any]] = []
    spans = runpy.run_path(str(ROOT / "scripts/benchmark_span_storage.py"))
    with sqlite3.connect("file:" + str(SPAN) + "?mode=ro&immutable=1", uri=True) as db:
        verified = spans["verify_spans"](db, sentinel=True)
    for key in ("passage_metadata_sha256", "passage_text_sha256", "passages", "books"):
        assert verified[key] == span_receipt["after"][key]
    records.append(
        {
            "path": str(SPAN.relative_to(ROOT)),
            "logical_bytes": SPAN.stat().st_size,
            "allocated_bytes": SPAN.stat().st_blocks * 512,
            "sha256": digest(SPAN),
            "identity_before": stamp(SPAN),
            "logical_receipt_verified": True,
            "span_proof": verified,
        }
    )
    print(json.dumps({"stage": "span_copy_verified", "bytes": SPAN.stat().st_size}), flush=True)
    h = hashlib.sha256()
    with sqlite3.connect("file:" + str(FTS) + "?mode=ro&immutable=1", uri=True) as db:
        for identity, value in db.execute("SELECT id,sha256 FROM passages ORDER BY id"):
            h.update(f"{identity}:{value}\n".encode())
        assert db.execute("SELECT COUNT(*) FROM books WHERE status='indexed'").fetchone()[0] == 1308
        assert db.execute("SELECT COUNT(*) FROM passages").fetchone()[0] == 445466
    assert h.hexdigest() == fts_receipt["explicit_rebuild_vacuum"]["passage_fingerprint"]
    records.append(
        {
            "path": str(FTS.relative_to(ROOT)),
            "logical_bytes": FTS.stat().st_size,
            "allocated_bytes": FTS.stat().st_blocks * 512,
            "sha256": digest(FTS),
            "identity_before": stamp(FTS),
            "logical_receipt_verified": True,
            "passage_fingerprint": h.hexdigest(),
        }
    )
    assert stamp(SOURCE) == source_stamp
    report = {
        "schema_version": 1,
        "authorized_by": "Root explicit authorization: only named owned span/FTS benchmark DB copies",
        "created_at": datetime.now(UTC).isoformat(),
        "source": str(SOURCE.relative_to(ROOT)),
        "source_sha256": expected_source,
        "source_unchanged": True,
        "free_bytes_before": shutil.disk_usage(ROOT).free,
        "records": records,
        "receipts_preserved": ["experiments/span-storage-v1.json", "experiments/fts-maintenance1308-v1.json"],
        "status": "preflight_verified",
    }
    target = ROOT / "data/runtime/corpus-benchmark-cleanup-v1.json"
    if target.exists():
        raise FileExistsError("Preserve previous cleanup receipt")
    save(target, report)
    for record in records:
        path = ROOT / record["path"]
        assert stamp(path) == tuple(record["identity_before"]) and not path.is_symlink()
        path.unlink()
        save(
            path.with_name("database.removed.json"),
            {"removed_at": datetime.now(UTC).isoformat(), "receipt": str(target.relative_to(ROOT)), **record},
        )
    assert stamp(SOURCE) == source_stamp and digest(SOURCE) == expected_source
    report.update(
        status="removed_named_owned_copies",
        free_bytes_after=shutil.disk_usage(ROOT).free,
        removed_logical_bytes=sum(r["logical_bytes"] for r in records),
    )
    report["observed_free_delta_bytes"] = report["free_bytes_after"] - report["free_bytes_before"]
    report["concurrency_note"] = "Live corpus ingest and authorized GGUF download may change free space concurrently."
    save(target, report)
    public = {k: v for k, v in report.items() if k != "records"}
    public["removed"] = [{k: v for k, v in r.items() if k not in {"span_proof", "identity_before"}} for r in records]
    save(ROOT / "experiments/corpus-benchmark-cleanup-v1.json", public)
    print(json.dumps(public, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
