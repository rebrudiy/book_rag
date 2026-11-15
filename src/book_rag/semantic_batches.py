"""Exact, bounded window manifests for explicit live-library edition batches.

Original SQLite is read-only. The manifest stores production encoder inputs, not
vectors; compact indexing can resume without keeping a full float32 matrix.
"""

from __future__ import annotations

import fcntl
import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from book_rag.experimental_ivfpq import canonical_bytes, file_sha256
from book_rag.incremental_ivfpq import Window
from book_rag.index_coverage import capture_coverage, validate_coverage
from book_rag.library import Library
from book_rag.semantic_pilot import ReadOnlyLibrary, _publish_json, selected_signature

KIND = "bookrag-explicit-edition-window-batch-v1"
RESERVE_BYTES = int(4.5 * 1024**3)


def _selection(book_ids: list[str]) -> list[str]:
    if (
        not isinstance(book_ids, list)
        or not 1 <= len(book_ids) <= 2048
        or any(not isinstance(identity, str) or not identity.strip() or len(identity) > 512 for identity in book_ids)
        or len(set(book_ids)) != len(book_ids)
    ):
        raise ValueError("Select one to 2048 explicit unique edition IDs")
    return sorted(book_ids)


def batch_source_state(source: Library, book_ids: list[str]) -> dict[str, Any]:
    """Bind canonical, attribution and origin data to the original SQL namespace."""
    books = _selection(book_ids)
    before = source.db_path.stat()
    with source.connect() as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN")
        found = conn.execute(
            "SELECT id,status FROM books WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id",
            (json.dumps(books),),
        ).fetchall()
        if [row["id"] for row in found] != books or any(row["status"] != "indexed" for row in found):
            raise ValueError("Every explicitly selected edition must be indexed")
        coverage = capture_coverage(conn, books)

        class SnapshotSource:
            db_path = source.db_path

            def connect(self) -> Any:
                from contextlib import nullcontext

                return nullcontext(conn)

        tracked = validate_coverage(SnapshotSource(), coverage)
        if not tracked["tracked"]:
            raise ValueError("Existing source epoch tracking is required; no migrations are performed")
        state = {
            "coverage": coverage,
            "state_token": tracked["state_token"],
            "signature": selected_signature(conn, books),
            "source_directory": str(source.data_dir),
            "database_device": before.st_dev,
            "database_inode": before.st_ino,
        }
    after = source.db_path.stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError("Original database replaced during batch proof")
    return state


def verify_batch_source(source: Library, receipt: dict[str, Any]) -> None:
    descriptor = receipt["descriptor"]
    if batch_source_state(source, descriptor["book_ids"]) != descriptor["source_state"]:
        raise ValueError("Selected canonical text, attribution, provenance or namespace changed")


def _internal_receipt(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key='receipt'").fetchone()
        if row is None:
            raise ValueError("Incomplete window manifest")
        result = json.loads(row[0])
        if not isinstance(result, dict):
            raise ValueError("Invalid window receipt")
        return result


def _verify_rows(path: Path, receipt: dict[str, Any]) -> None:
    chain = "0" * 64
    counts = dict.fromkeys(receipt["rows_by_book"], 0)
    rows = 0
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        for row in conn.execute("SELECT id,passage_id,book_id,text,input_sha256 FROM windows ORDER BY id"):
            if row[0] != rows or row[2] not in counts or hashlib.sha256(row[3].encode()).hexdigest() != row[4]:
                raise ValueError("Window manifest row checksum, identity or ownership changed")
            window = Window(*row[:4])
            chain = hashlib.sha256(bytes.fromhex(chain) + window.key()).hexdigest()
            counts[window.book_id] += 1
            rows += 1
    if rows != receipt["rows"] or chain != receipt["input_chain"] or counts != receipt["rows_by_book"]:
        raise ValueError("Window manifest does not match its complete stream receipt")


def prepare_batch_windows(
    source_directory: Path,
    output: Path,
    book_ids: list[str],
    model: Any,
    model_space: dict[str, Any],
    *,
    max_windows: int = 200_000,
    max_manifest_bytes: int = 512 * 1024**2,
    reserve_bytes: int = RESERVE_BYTES,
) -> dict[str, Any]:
    """Seal all production windows for selected editions, with bounded disk use.

    The source is never initialized or migrated. Covered changes invalidate reuse;
    unrelated additions and failed refresh attempts do not change canonical data.
    """
    from book_rag.semantic import _windows, token_windows

    books = _selection(book_ids)
    if type(max_windows) is not int or not 1 <= max_windows <= 2_000_000:
        raise ValueError("Window limit must be between one and two million")
    if type(max_manifest_bytes) is not int or not 4096 <= max_manifest_bytes <= 1024**3:
        raise ValueError("Window manifest byte limit must be within 4KiB and 1GiB")
    if type(reserve_bytes) is not int or not 0 <= reserve_bytes <= 1024**4:
        raise ValueError("Invalid free-space reserve")
    source = ReadOnlyLibrary(source_directory)
    output = output.absolute()
    if output.is_symlink() or output.resolve().is_relative_to(source.data_dir):
        raise ValueError("Write a separate owned manifest outside the original library")
    output.parent.mkdir(parents=True, exist_ok=True)
    state = batch_source_state(source, books)
    descriptor = {
        "kind": KIND,
        "book_ids": books,
        "source_state": state,
        "model_space": json.loads(canonical_bytes(model_space)),
        "window_recipe_sha256": hashlib.sha256(
            (inspect.getsource(_windows) + inspect.getsource(token_windows)).encode()
        ).hexdigest(),
        "max_windows": max_windows,
        "max_manifest_bytes": max_manifest_bytes,
    }
    receipt_path = output.with_suffix(output.suffix + ".receipt.json")
    lock_path = output.with_suffix(output.suffix + ".lock")
    if lock_path.is_symlink() or receipt_path.is_symlink():
        raise ValueError("Manifest metadata must be owned nonsymlink files")
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another process owns this manifest") from error
        if output.exists():
            internal = _internal_receipt(output)
            if internal.get("descriptor") != descriptor:
                raise ValueError("Existing window manifest belongs to another source or model recipe")
            _verify_rows(output, internal)
            verify_batch_source(source, internal)
            restored = internal | {"database_sha256": file_sha256(output)}
            if receipt_path.exists():
                prior = json.loads(receipt_path.read_text())
                if prior != restored:
                    raise ValueError("Sealed window manifest checksum or receipt changed")
                return prior
            # Recovery after the immutable SQLite link, before its sidecar commit.
            _publish_json(receipt_path, restored)
            return restored
        if receipt_path.exists():
            raise ValueError("Sealed window manifest payload is missing")
        temporary = output.with_name(output.name + ".owned-staging-" + uuid.uuid4().hex)
        chain = "0" * 64
        counts = dict.fromkeys(books, 0)
        rows = 0
        try:
            with sqlite3.connect(temporary) as target, source.connect() as conn:
                target.executescript(
                    "CREATE TABLE windows(id INTEGER PRIMARY KEY,passage_id INTEGER,"
                    "book_id TEXT,text TEXT,input_sha256 TEXT);"
                    "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)"
                )
                conn.execute("BEGIN")
                # Book-first seeks avoid rescanning unrelated millions of passages.
                sql = (
                    "SELECT p.id,p.text,b.id book_id,b.title,b.author,s.title section_title "
                    "FROM books b CROSS JOIN passages p ON p.book_id=b.id "
                    "JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
                    "WHERE b.id IN (SELECT value FROM json_each(?)) AND b.status='indexed' ORDER BY p.id"
                )
                for passage in conn.execute(sql, (json.dumps(books),)):
                    for text in _windows(model, passage):
                        if rows >= max_windows:
                            raise ValueError("Selected complete editions exceed the bounded window limit")
                        if rows % 128 == 0:
                            if temporary.stat().st_size > max_manifest_bytes:
                                raise ValueError("Window manifest exceeded its disk budget")
                            if shutil.disk_usage(output.parent).free < reserve_bytes:
                                raise RuntimeError("Free disk space reached the configured reserve")
                        window = Window(rows, passage["id"], passage["book_id"], text)
                        chain = hashlib.sha256(bytes.fromhex(chain) + window.key()).hexdigest()
                        target.execute(
                            "INSERT INTO windows VALUES (?,?,?,?,?)",
                            (rows, window.passage_id, window.book_id, text, hashlib.sha256(text.encode()).hexdigest()),
                        )
                        counts[window.book_id] += 1
                        rows += 1
                if not rows or not all(counts.values()):
                    raise ValueError("Every selected edition must contribute its complete text windows")
                receipt = {
                    "schema_version": 1,
                    "descriptor": descriptor,
                    "rows": rows,
                    "rows_by_book": counts,
                    "input_chain": chain,
                    "embedding_vectors_stored": False,
                    "source_database_copied": False,
                }
                target.execute("INSERT INTO metadata VALUES ('receipt',?)", (json.dumps(receipt, sort_keys=True),))
            if temporary.stat().st_size > max_manifest_bytes:
                raise ValueError("Window manifest exceeded its disk budget at commit")
            verify_batch_source(source, receipt)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.link(temporary, output)
            receipt["database_sha256"] = file_sha256(output)
            _publish_json(receipt_path, receipt)
            return receipt
        finally:
            temporary.unlink(missing_ok=True)
