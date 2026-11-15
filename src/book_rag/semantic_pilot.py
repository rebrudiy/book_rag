"""Owned canonical subset and exact production-window manifests for pilots.

Original databases use read-only connections without Library initialization.
Only the new owned subset initializes normal schema/FTS/epoch tracking.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from book_rag.experimental_ivfpq import canonical_bytes, file_sha256
from book_rag.incremental_ivfpq import Window
from book_rag.index_coverage import capture_coverage
from book_rag.library import Library

TABLES = ("books", "book_metadata", "index_versions", "sections", "section_roles", "passages")


def _publish_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".owned-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)  # An existing sealed receipt is never overwritten.
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class ReadOnlyLibrary(Library):
    """A read-only normal-library interface; no constructor migration or DDL."""

    def __init__(self, directory: Path) -> None:
        self.data_dir = directory.resolve(strict=True)
        self.db_path = self.data_dir / "library.sqlite3"
        if not self.db_path.is_file() or self.db_path.is_symlink():
            raise ValueError("Expected a regular existing canonical database")

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.create_function("casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
        conn.execute("PRAGMA query_only=ON")
        try:
            yield conn
        finally:
            conn.close()


def _selected_rows(conn: sqlite3.Connection, table: str, book_ids: list[str]) -> Generator[sqlite3.Row]:
    if table not in TABLES and table != "source_pipeline":
        raise ValueError("Unsupported canonical table")
    parameter = json.dumps(book_ids)
    if table == "section_roles":
        sql = (
            "SELECT a.* FROM section_roles a JOIN sections s ON s.id=a.section_id "
            "WHERE s.book_id IN (SELECT value FROM json_each(?)) ORDER BY a.section_id"
        )
    else:
        key = "id" if table == "books" else "book_id"
        order = "id" if table in ("books", "sections", "passages") else "book_id"
        sql = f"SELECT * FROM {table} WHERE {key} IN (SELECT value FROM json_each(?)) ORDER BY {order}"
    yield from conn.execute(sql, (parameter,))


def selected_signature(conn: sqlite3.Connection, book_ids: list[str]) -> dict[str, Any]:
    """Include canonical bytes, original IDs, provenance and attribution metadata."""
    hashes = {}
    counts = {}
    for table in TABLES:
        checksum = hashlib.sha256()
        count = 0
        for row in _selected_rows(conn, table, book_ids):
            value = dict(row)
            if table == "books":
                value = {
                    key: item
                    for key, item in value.items()
                    if key not in {"created_at", "indexed_at", "local_path", "error"}
                }
            checksum.update(canonical_bytes(value) + b"\n")
            count += 1
        hashes[table] = checksum.hexdigest()
        counts[table] = count
    return {"book_ids": book_ids, "table_sha256": hashes, "table_counts": counts}


def signature(library: ReadOnlyLibrary, book_ids: list[str]) -> dict[str, Any]:
    with library.connect() as conn:
        conn.execute("BEGIN")
        return selected_signature(conn, book_ids)


def create_owned_subset(source_directory: Path, destination: Path, book_ids: list[str] | None = None) -> dict[str, Any]:
    source = ReadOnlyLibrary(source_directory)
    if destination.exists():
        raise FileExistsError("Owned subset destination must be new")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".owned-staging-" + uuid.uuid4().hex)
    try:
        target = Library(staging)  # Only the newly owned database is initialized.
        with source.connect() as original, target.connect() as owned:
            original.execute("BEGIN")
            selected = (
                sorted(book_ids)
                if book_ids is not None
                else [
                    row[0]
                    for row in original.execute(
                        "SELECT id FROM books WHERE language='ru' AND status='indexed' ORDER BY id LIMIT 26"
                    )
                ]
            )
            if not selected or len(selected) > 25 or len(set(selected)) != len(selected):
                raise ValueError("Pilot requires a bounded unique subset of at most25 editions")
            available = original.execute(
                "SELECT id,language,status FROM books WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(selected),),
            ).fetchall()
            if len(available) != len(selected) or any(
                row["language"] != "ru" or row["status"] != "indexed" for row in available
            ):
                raise ValueError("Pilot selection must contain existing indexed Russian editions")
            proof = capture_coverage(original, selected)
            before = selected_signature(original, selected)
            owned.execute("BEGIN IMMEDIATE")
            for table in (*TABLES, "source_pipeline"):
                for row in _selected_rows(original, table, selected):
                    columns = list(row.keys())
                    owned.execute(
                        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        tuple(row),
                    )
            for row in owned.execute(
                "SELECT p.*,b.title,b.author,s.title section_title,a.section_author FROM passages p "
                "JOIN books b ON b.id=p.book_id JOIN sections s ON s.id=p.section_id "
                "LEFT JOIN section_roles a ON a.section_id=s.id ORDER BY p.id"
            ):
                author = row["author"]
                if row["section_author"] and row["section_author"] != author:
                    author += "; " + row["section_author"]
                owned.execute(
                    "INSERT INTO passage_fts(rowid,text,title,author,section_title) VALUES (?,?,?,?,?)",
                    (row["id"], row["text"], row["title"], author, row["section_title"]),
                )
            if capture_coverage(owned, selected) != proof or selected_signature(owned, selected) != before:
                raise ValueError("Owned canonical subset differs from coherent source snapshot")
            if owned.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Owned subset foreign-key failure")
            owned.execute("INSERT INTO passage_fts(passage_fts) VALUES ('integrity-check')")
        if signature(source, selected) != before:
            raise ValueError("Selected live editions changed during subset capture")
        with sqlite3.connect(target.db_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("VACUUM")
        receipt = {
            "schema_version": 1,
            "kind": "owned-selective-canonical-copy",
            "source_directory": str(source.data_dir),
            "source_signature": before,
            "source_coverage": proof,
            "book_ids": selected,
            "snapshot_db_sha256": file_sha256(target.db_path),
            "snapshot_bytes": target.db_path.stat().st_size,
            "saved_notes_copied": False,
            "original_library_constructor_called": False,
            "original_models_vectors_artifacts_copied": False,
        }
        _publish_json(staging / "snapshot.json", receipt)
        staging.rename(destination)
        return receipt
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_owned_subset(directory: Path, *, verify_live: bool = True) -> dict[str, Any]:
    receipt = json.loads((directory / "snapshot.json").read_text())
    owned = ReadOnlyLibrary(directory)
    if file_sha256(owned.db_path) != receipt["snapshot_db_sha256"]:
        raise ValueError("Owned canonical database bytes changed")
    if signature(owned, receipt["book_ids"]) != receipt["source_signature"]:
        raise ValueError("Owned canonical/provenance signature changed")
    if (
        verify_live
        and signature(ReadOnlyLibrary(Path(receipt["source_directory"])), receipt["book_ids"])
        != receipt["source_signature"]
    ):
        raise ValueError("Selected original source/attribution changed since snapshot")
    return receipt


def prepare_windows(directory: Path, output: Path, model: Any, model_space: dict[str, Any]) -> dict[str, Any]:
    """Persist exact production MiniLM window inputs, not embedding vectors."""
    from book_rag.semantic import _windows, token_windows

    source = ReadOnlyLibrary(directory)
    with source.connect() as conn:
        conn.execute("BEGIN")
        books = [row[0] for row in conn.execute("SELECT id FROM books WHERE status='indexed' ORDER BY id")]
        proof = capture_coverage(conn, books)
    recipe = hashlib.sha256((inspect.getsource(_windows) + inspect.getsource(token_windows)).encode()).hexdigest()
    descriptor = {"model_space": model_space, "source_coverage": proof, "window_recipe_sha256": recipe}
    receipt_path = output.with_suffix(output.suffix + ".receipt.json")
    if output.exists():
        if not receipt_path.is_file():
            if output.is_symlink():
                raise ValueError("Unsealed owned manifest cannot be a symlink")
            output.unlink()
        else:
            prior = json.loads(receipt_path.read_text())
            if prior["descriptor"] != descriptor or file_sha256(output) != prior["database_sha256"]:
                raise ValueError("Existing window manifest belongs to another canonical/model recipe")
            return prior
    temporary = output.with_name(output.name + ".owned-staging")
    if temporary.exists():
        temporary.unlink()
    counts = dict.fromkeys(books, 0)
    chain = "0" * 64
    rows = 0
    try:
        with sqlite3.connect(temporary) as target, source.connect() as conn:
            target.executescript(
                "CREATE TABLE windows(id INTEGER PRIMARY KEY,passage_id INTEGER,"
                "book_id TEXT,text TEXT,input_sha256 TEXT);"
                "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)"
            )
            conn.execute("BEGIN")
            for passage in conn.execute(
                "SELECT p.id,p.text,b.id book_id,b.title,b.author,s.title section_title FROM passages p "
                "JOIN books b ON b.id=p.book_id JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
                "WHERE b.status='indexed' ORDER BY p.id"
            ):
                for text in _windows(model, passage):
                    if rows >= 50000:
                        raise ValueError("Bounded pilot window limit50000 exceeded")
                    window = Window(rows, passage["id"], passage["book_id"], text)
                    chain = hashlib.sha256(bytes.fromhex(chain) + window.key()).hexdigest()
                    target.execute(
                        "INSERT INTO windows VALUES (?,?,?,?,?)",
                        (rows, window.passage_id, window.book_id, text, hashlib.sha256(text.encode()).hexdigest()),
                    )
                    counts[window.book_id] += 1
                    rows += 1
            if capture_coverage(conn, books) != proof:
                raise ValueError("Canonical source changed during window production")
            receipt = {
                "schema_version": 1,
                "descriptor": descriptor,
                "rows": rows,
                "rows_by_book": counts,
                "input_chain": chain,
                "embedding_vectors_stored": False,
            }
            target.execute("INSERT INTO metadata VALUES ('receipt',?)", (json.dumps(receipt, sort_keys=True),))
        os.link(temporary, output)
        receipt["database_sha256"] = file_sha256(output)
        _publish_json(receipt_path, receipt)
        return receipt
    finally:
        temporary.unlink(missing_ok=True)


def manifest_windows(path: Path) -> Generator[Window]:
    receipt = json.loads(path.with_suffix(path.suffix + ".receipt.json").read_text())
    expected_sha256 = receipt["database_sha256"]
    if path.is_symlink() or file_sha256(path) != expected_sha256:
        raise ValueError("Sealed window manifest checksum mismatch")
    chain = "0" * 64
    count = 0
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        for row in conn.execute("SELECT id,passage_id,book_id,text,input_sha256 FROM windows ORDER BY id"):
            if hashlib.sha256(row[3].encode()).hexdigest() != row[4]:
                raise ValueError("Window manifest encoder input checksum mismatch")
            window = Window(*row[:4])
            if window.window_id != count:
                raise ValueError("Window manifest IDs are not dense and ordered")
            chain = hashlib.sha256(bytes.fromhex(chain) + window.key()).hexdigest()
            count += 1
            yield window
        if count != receipt["rows"] or chain != receipt["input_chain"] or file_sha256(path) != expected_sha256:
            raise ValueError("Sealed window manifest changed during replay")
    finally:
        conn.close()
