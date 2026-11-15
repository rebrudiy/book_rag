"""Owned CC0 compact reader tests; no tokenizer, models, native backend or live DB."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from book_rag.compact_window_reader import KIND, compact_windows
from book_rag.experimental_ivfpq import file_sha256
from book_rag.incremental_ivfpq import Window
from book_rag.library import Library
from book_rag.semantic_batches import batch_source_state
from book_rag.semantic_pilot import ReadOnlyLibrary


def write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True))


@pytest.fixture
def packed(tmp_path: Path) -> SimpleNamespace:
    library = Library(tmp_path / "owned")
    text = tmp_path / "own-cc0.txt"
    text.write_text("Эхо 🦉 повтор эхо. This original CC0 archive retains exact Unicode offsets.")
    library.import_book(text, {"id": "cc0", "title": "Owned CC0", "author": "CC0 fixture", "language": "ru"})
    source = ReadOnlyLibrary(library.data_dir)
    with source.connect() as conn:
        passage = conn.execute("SELECT id,text FROM passages WHERE book_id='cc0' ORDER BY id LIMIT 1").fetchone()
    spans = [(0, 10), (5, len(passage["text"]))]
    records = [
        Window(i, passage["id"], "cc0", "Own prefix\n" + passage["text"][start:end])
        for i, (start, end) in enumerate(spans)
    ]
    original = tmp_path / "original.sqlite3"
    with sqlite3.connect(original) as conn:
        conn.execute("CREATE TABLE windows(id,passage_id,book_id,text,input_sha256)")
        conn.executemany(
            "INSERT INTO windows VALUES (?,?,?,?,?)",
            [
                (w.window_id, w.passage_id, w.book_id, w.text, hashlib.sha256(w.text.encode()).hexdigest())
                for w in records
            ],
        )
    chain = "0" * 64
    for record in records:
        chain = hashlib.sha256(bytes.fromhex(chain) + record.key()).hexdigest()
    producer = {
        "descriptor": {"book_ids": ["cc0"], "source_state": batch_source_state(source, ["cc0"])},
        "rows": len(records),
        "rows_by_book": {"cc0": len(records)},
        "input_chain": chain,
        "database_sha256": file_sha256(original),
    }
    compact = tmp_path / "compact.sqlite3"
    with sqlite3.connect(compact) as conn:
        conn.executescript(
            "CREATE TABLE books(id INTEGER PRIMARY KEY,book_id TEXT);"
            "CREATE TABLE prefixes(id INTEGER PRIMARY KEY,text TEXT);"
            "CREATE TABLE windows(id INTEGER PRIMARY KEY,passage_id,book_index,prefix_id,start,end,input_sha256);"
        )
        conn.execute("INSERT INTO books VALUES (0,'cc0')")
        conn.execute("INSERT INTO prefixes VALUES (0,'Own prefix')")
        conn.executemany(
            "INSERT INTO windows VALUES (?,?,0,0,?,?,?)",
            [
                (w.window_id, w.passage_id, start, end, hashlib.sha256(w.text.encode()).digest())
                for w, (start, end) in zip(records, spans, strict=True)
            ],
        )
    receipt = {
        "schema_version": 1,
        "kind": KIND,
        "database_sha256": file_sha256(compact),
        "original_receipt": producer,
        "original_manifest_sha256": producer["database_sha256"],
        "original_manifest": str(original),
        "original_receipt_sha256": "a" * 64,
        "rows": len(records),
        "books": 1,
        "prefixes": 1,
        "input_chain": chain,
        "offset_unit": "Python Unicode code points relative to canonical passage.text",
        "max_seq_length": 128,
        "body_text_stored": False,
        "production_activation": False,
    }
    sidecar = tmp_path / "compact.receipt.json"
    write(sidecar, receipt)
    return SimpleNamespace(
        library=library,
        source=source,
        original=original,
        compact=compact,
        receipt=receipt,
        sidecar=sidecar,
        producer=producer,
        records=records,
    )


def read(state: SimpleNamespace) -> list[Window]:
    return list(
        compact_windows(
            state.source,
            state.compact,
            state.sidecar,
            expected_sha256=state.receipt["database_sha256"],
            expected_receipt_sha256=file_sha256(state.sidecar),
            producer_receipt=state.producer,
        )
    )


def reseal_owned_test(state: SimpleNamespace) -> None:
    state.receipt["database_sha256"] = file_sha256(state.compact)
    write(state.sidecar, state.receipt)


def test_original_text_payload_can_be_removed_and_exact_windows_still_stream(packed: SimpleNamespace) -> None:
    before = file_sha256(packed.source.db_path)
    packed.original.unlink()
    assert read(packed) == packed.records
    assert file_sha256(packed.source.db_path) == before


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE windows SET start=start+1 WHERE id=0",
        "UPDATE windows SET end=99999 WHERE id=0",
        "UPDATE windows SET id=10 WHERE id=0",
        "UPDATE windows SET passage_id=99999 WHERE id=0",
        "UPDATE windows SET book_index=99999 WHERE id=0",
        "UPDATE windows SET prefix_id=99999 WHERE id=0",
        "UPDATE windows SET input_sha256=zeroblob(31) WHERE id=0",
        "UPDATE prefixes SET text='Changed prefix'",
        "INSERT INTO windows VALUES (99,99999,99999,99999,0,1,zeroblob(32))",
    ],
)
def test_resealed_corrupt_offsets_hashes_and_orphans_are_rejected(packed: SimpleNamespace, sql: str) -> None:
    with sqlite3.connect(packed.compact) as conn:
        conn.execute(sql)
    reseal_owned_test(packed)
    with pytest.raises(ValueError):
        read(packed)


def test_wrong_outer_receipt_and_payload_identity_fail_before_stream(packed: SimpleNamespace) -> None:
    with pytest.raises(ValueError):
        list(
            compact_windows(
                packed.source,
                packed.compact,
                packed.sidecar,
                expected_sha256="0" * 64,
                expected_receipt_sha256=file_sha256(packed.sidecar),
                producer_receipt=packed.producer,
            )
        )
    packed.receipt["original_manifest_sha256"] = "0" * 64
    write(packed.sidecar, packed.receipt)
    with pytest.raises(ValueError):
        read(packed)


def test_wrong_input_digest_is_rejected(packed: SimpleNamespace) -> None:
    with sqlite3.connect(packed.compact) as conn:
        conn.execute("UPDATE windows SET input_sha256=zeroblob(32) WHERE id=1")
    reseal_owned_test(packed)
    with pytest.raises(ValueError):
        read(packed)


def test_matching_rows_with_wrong_authoritative_chain_cannot_finish(packed: SimpleNamespace) -> None:
    packed.producer["input_chain"] = "0" * 64
    packed.receipt["input_chain"] = "0" * 64
    write(packed.sidecar, packed.receipt)
    with pytest.raises(ValueError, match="chain"):
        read(packed)


def test_source_provenance_change_after_first_yield_is_caught_by_fresh_guard(packed: SimpleNamespace) -> None:
    iterator = compact_windows(
        packed.source,
        packed.compact,
        packed.sidecar,
        expected_sha256=packed.receipt["database_sha256"],
        expected_receipt_sha256=file_sha256(packed.sidecar),
        producer_receipt=packed.producer,
    )
    assert next(iterator) == packed.records[0]
    with packed.library.connect() as conn:
        conn.execute("UPDATE books SET rights='Changed during iterator' WHERE id='cc0'")
    with pytest.raises(ValueError):
        list(iterator)


def test_receipt_changed_after_first_yield_cannot_finish(packed: SimpleNamespace) -> None:
    iterator = compact_windows(
        packed.source,
        packed.compact,
        packed.sidecar,
        expected_sha256=packed.receipt["database_sha256"],
        expected_receipt_sha256=file_sha256(packed.sidecar),
        producer_receipt=packed.producer,
    )
    next(iterator)
    packed.receipt["rows"] = 999
    write(packed.sidecar, packed.receipt)
    with pytest.raises(ValueError):
        list(iterator)


def test_changed_canonical_source_is_rejected_before_reconstruction(packed: SimpleNamespace) -> None:
    with packed.library.connect() as conn:
        conn.execute("UPDATE passages SET text=text||'changed' WHERE book_id='cc0'")
    with pytest.raises(ValueError):
        read(packed)


def test_import_in_fresh_process_has_no_tokenizer_or_model_side_effects() -> None:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    code = (
        "import sys; import book_rag.compact_window_reader; "
        "assert not ({'torch','faiss','llama_cpp','transformers','book_rag.semantic'} & set(sys.modules))"
    )
    subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True, timeout=10)
