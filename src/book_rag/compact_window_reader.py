"""Exact read-only compact windows; no tokenizer, semantic model or native imports."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import Any

from book_rag.experimental_ivfpq import file_sha256
from book_rag.incremental_ivfpq import Window
from book_rag.library import Library
from book_rag.semantic_batches import verify_batch_source

KIND = "bookrag-compact-offset-window-manifest-prototype-v1"
FORMAT = "canonical-offset-v1"
MAX_COMPACT_BYTES = 250 * 1024**2
MAX_RECEIPT_BYTES = 1024**2


def validate_compact_paths(path: Path, receipt_path: Path) -> None:
    """Reject nonregular/oversized inputs before any potentially expensive hash."""
    for artifact, budget in ((path, MAX_COMPACT_BYTES), (receipt_path, MAX_RECEIPT_BYTES)):
        if artifact.is_symlink() or not artifact.is_file() or artifact.stat().st_size > budget:
            raise ValueError("Expected bounded regular compact artifacts")


def compact_receipt(
    path: Path,
    receipt_path: Path,
    *,
    expected_sha256: str,
    expected_receipt_sha256: str,
    producer_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Authenticate immutable payload and conversion proof without model imports."""
    validate_compact_paths(path, receipt_path)
    if file_sha256(path) != expected_sha256 or file_sha256(receipt_path) != expected_receipt_sha256:
        raise ValueError("Compact payload or receipt checksum changed")
    receipt = json.loads(receipt_path.read_text())
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("kind") != KIND
        or receipt.get("database_sha256") != expected_sha256
        or receipt.get("original_receipt") != producer_receipt
        or receipt.get("original_manifest_sha256") != producer_receipt["database_sha256"]
        or receipt.get("original_receipt_sha256") is None
        or receipt.get("rows") != producer_receipt["rows"]
        or receipt.get("input_chain") != producer_receipt["input_chain"]
        or receipt.get("offset_unit") != "Python Unicode code points relative to canonical passage.text"
        or receipt.get("max_seq_length") != 128
        or receipt.get("body_text_stored") is not False
        or receipt.get("production_activation") is not False
    ):
        raise ValueError("Compact proof differs from the original producer receipt")
    return receipt


def compact_windows(
    source: Library,
    path: Path,
    receipt_path: Path,
    *,
    expected_sha256: str,
    expected_receipt_sha256: str,
    producer_receipt: dict[str, Any],
) -> Generator[Window]:
    """Reconstruct all windows and validate the complete original ordered chain.

    Callers must exhaust the iterator. The original text manifest is not needed;
    its producer receipt/chain remains the authority, alongside live source proof.
    """
    compact_receipt(
        path,
        receipt_path,
        expected_sha256=expected_sha256,
        expected_receipt_sha256=expected_receipt_sha256,
        producer_receipt=producer_receipt,
    )
    verify_batch_source(source, producer_receipt)
    counts = dict.fromkeys(producer_receipt["rows_by_book"], 0)
    chain, count, passage = "0" * 64, 0, None
    with source.connect() as canonical, sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as packed:
        if not canonical.in_transaction:
            canonical.execute("BEGIN")
        packed.execute("PRAGMA query_only=ON")
        packed.execute("BEGIN")
        raw_count = packed.execute("SELECT count(*) FROM windows").fetchone()[0]
        if raw_count != producer_receipt["rows"]:
            raise ValueError("Compact raw row count differs from sealed windows")
        rows = packed.execute(
            "SELECT w.id,w.passage_id,b.book_id,p.text,w.start,w.end,w.input_sha256 "
            "FROM windows w LEFT JOIN books b ON b.id=w.book_index "
            "LEFT JOIN prefixes p ON p.id=w.prefix_id ORDER BY w.id"
        )
        for row in rows:
            if row[0] != count or row[2] not in counts or not isinstance(row[3], str):
                raise ValueError("Compact IDs or ownership references changed")
            if passage is None or passage["id"] != row[1]:
                passage = canonical.execute(
                    "SELECT p.id,p.book_id,p.text,p.sha256 FROM passages p "
                    "JOIN books b ON b.id=p.book_id JOIN sections s "
                    "ON s.id=p.section_id AND s.book_id=p.book_id WHERE p.id=? AND b.status='indexed'",
                    (row[1],),
                ).fetchone()
                if passage is None or hashlib.sha256(passage["text"].encode()).hexdigest() != passage["sha256"]:
                    raise ValueError("Missing or corrupt canonical compact source")
            if (
                passage["book_id"] != row[2]
                or type(row[4]) is not int
                or type(row[5]) is not int
                or not 0 <= row[4] <= row[5] <= len(passage["text"])
            ):
                raise ValueError("Compact ownership or Unicode offsets changed")
            text = row[3] + "\n" + passage["text"][row[4] : row[5]]
            if hashlib.sha256(text.encode()).digest() != row[6]:
                raise ValueError("Compact input checksum changed")
            window = Window(row[0], row[1], row[2], text)
            chain = hashlib.sha256(bytes.fromhex(chain) + window.key()).hexdigest()
            counts[row[2]] += 1
            count += 1
            yield window
    if count != producer_receipt["rows"] or counts != producer_receipt["rows_by_book"]:
        raise ValueError("Compact complete-stream counts changed")
    if chain != producer_receipt["input_chain"]:
        raise ValueError("Compact full input chain changed")
    verify_batch_source(source, producer_receipt)
    compact_receipt(
        path,
        receipt_path,
        expected_sha256=expected_sha256,
        expected_receipt_sha256=expected_receipt_sha256,
        producer_receipt=producer_receipt,
    )
