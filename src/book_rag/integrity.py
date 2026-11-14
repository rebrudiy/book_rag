"""Read-only, snapshot-consistent evidence and artifact integrity audits."""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from pathlib import Path
from typing import Any

from book_rag.library import Library, digest


def audit_library(library: Library, artifacts: bool = False) -> dict[str, Any]:
    """Check every indexed section/passage, optionally rehash original files.

    Does not download, repair, rewrite metadata or mark unsupported source
    transcription as verified. A read transaction keeps canonical checks coherent
    while a live importer writes newer data in WAL mode.
    """
    started = time.perf_counter()
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    books_checked = passages_checked = artifacts_checked = 0
    error_count = 0
    error_types: Counter[str] = Counter()

    def record(error: dict[str, Any]) -> None:
        nonlocal error_count
        error_count += 1
        error_types[error["error"]] += 1
        if len(errors) < 100:
            errors.append(error)

    with library.connect() as conn:
        conn.execute("BEGIN")
        for book in conn.execute("SELECT * FROM books WHERE status='indexed' ORDER BY id"):
            books_checked += 1
            content = hashlib.sha256()
            section_count = 0
            for section in conn.execute("SELECT * FROM sections WHERE book_id=? ORDER BY ordinal", (book["id"],)):
                if section_count:
                    content.update(b"\n\n")
                content.update(section["text"].encode("utf-8"))
                section_count += 1
                for passage in conn.execute(
                    "SELECT * FROM passages WHERE section_id=? ORDER BY start", (section["id"],)
                ):
                    passages_checked += 1
                    start, end, text = passage["start"], passage["end"], passage["text"]
                    if not (
                        passage["book_id"] == book["id"]
                        and 0 <= start < end <= len(section["text"])
                        and end - start == len(text)
                        and section["text"][start:end] == text
                        and digest(text) == passage["sha256"]
                    ):
                        record({"book_id": book["id"], "passage_id": passage["id"], "error": "citation_integrity"})
            if not section_count or content.hexdigest() != book["content_sha256"]:
                record({"book_id": book["id"], "error": "canonical_content_hash"})
            if artifacts:
                path = Path(book["local_path"]) if book["local_path"] else None
                if path is None or not path.is_file():
                    warnings.append({"book_id": book["id"], "warning": "original_artifact_unavailable"})
                else:
                    with path.open("rb") as stream:
                        actual = hashlib.file_digest(stream, "sha256").hexdigest()
                    artifacts_checked += 1
                    if actual != book["artifact_sha256"]:
                        record({"book_id": book["id"], "error": "original_artifact_hash"})
        for row in conn.execute("PRAGMA foreign_key_check"):
            record({"error": "foreign_key", "table": row[0], "rowid": row[1]})
    return {
        "valid": error_count == 0 and (not artifacts or artifacts_checked == books_checked),
        "canonical_valid": not any(
            error_types[k] for k in ("citation_integrity", "canonical_content_hash", "foreign_key")
        ),
        "status": "failed"
        if error_count
        else "incomplete"
        if artifacts and artifacts_checked != books_checked
        else "passed",
        "books_checked": books_checked,
        "passages_checked": passages_checked,
        "artifacts_requested": artifacts,
        "artifacts_checked": artifacts_checked,
        "originals_complete": artifacts_checked == books_checked if artifacts else None,
        "error_count": error_count,
        "errors": errors,
        "errors_truncated": error_count > len(errors),
        "warnings": warnings,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "scope": "Canonical text/digests and optional original file digests, not source transcription accuracy",
    }
