"""Standalone exact offset-based window manifest prototype; never activated.

Original source/manifest/registries are read-only. Offsets come from the pinned
tokenizer recipe, never substring search. This format requires its original SQL
namespace and canonical passages; it is not a self-contained text archive.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from book_rag.embedding_profiles import MINILM_MODEL, MINILM_REVISION
from book_rag.experimental_ivfpq import file_sha256
from book_rag.incremental_ivfpq import Window
from book_rag.semantic import _windows, token_windows
from book_rag.semantic_batches import RESERVE_BYTES, verify_batch_source
from book_rag.semantic_pilot import ReadOnlyLibrary, _publish_json

KIND = "bookrag-compact-offset-window-manifest-prototype-v1"
MAX_BYTES = 250 * 1024**2


def load_pinned_tokenizer(cache: Path) -> tuple[Any, dict[str, Any]]:
    """Load only a local pinned fast tokenizer, in a standalone no-Torch process."""
    if "torch" in sys.modules:
        raise RuntimeError("Run the tokenizer-only prototype in a fresh no-Torch interpreter")
    for name in ("USE_TORCH", "USE_TF", "USE_FLAX"):
        os.environ[name] = "0"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    cache = cache.resolve(strict=True)
    snapshot = cache / ("models--" + MINILM_MODEL.replace("/", "--")) / "snapshots" / MINILM_REVISION
    files = {}
    for name in ("config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"):
        path = snapshot / name
        if path.exists():
            if not path.resolve().is_relative_to(cache):
                raise ValueError("Tokenizer cache reference escapes its owned cache")
            files[name] = file_sha256(path.resolve())
    if "tokenizer.json" not in files or "tokenizer_config.json" not in files:
        raise ValueError("Pinned fast tokenizer files are not cached; downloads are forbidden")
    # Installed Transformers probes package availability even with USE_TORCH=0.
    # A None sentinel makes find_spec("torch") return unavailable without ever
    # executing Torch. Only this fresh tokenizer-only interpreter owns it.
    cast(dict[str, ModuleType | None], sys.modules)["torch"] = None
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            str(snapshot), local_files_only=True, trust_remote_code=False, use_fast=True
        )
    finally:
        if sys.modules.get("torch") is None:
            sys.modules.pop("torch", None)
    if tokenizer is None or not tokenizer.is_fast or "torch" in sys.modules:
        raise RuntimeError("Expected a fast tokenizer without importing Torch")
    return tokenizer, {
        "model": MINILM_MODEL,
        "revision": MINILM_REVISION,
        "files_sha256": files,
        "torch_import_disabled": True,
    }


def offset_windows(tokenizer: Any, row: Any, max_seq_length: int = 128) -> Generator[tuple[str, int, int]]:
    """The exact production recipe, returning unambiguous passage char offsets."""
    if type(max_seq_length) is not int or max_seq_length != 128:
        raise ValueError("This prototype supports the pinned production 128-token recipe only")
    header = f"{row['title']} — {row['author']} / {row['section_title']}"
    ids = tokenizer.encode(header, add_special_tokens=False)[:24]
    prefix = tokenizer.decode(ids, skip_special_tokens=True)
    budget = max(32, max_seq_length - len(ids) - 6)
    offsets = tokenizer(row["text"], add_special_tokens=False, return_offsets_mapping=True, truncation=False)[
        "offset_mapping"
    ]
    for start in range(0, len(offsets), budget - 20):
        end = min(start + budget, len(offsets))
        if end > start:
            left, right = offsets[start][0], offsets[end - 1][1]
            if type(left) is not int or type(right) is not int or not 0 <= left <= right <= len(row["text"]):
                raise ValueError("Tokenizer emitted invalid canonical character offsets")
            yield prefix, left, right
        if end == len(offsets):
            break


def _source_passage(connection: sqlite3.Connection, identity: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT p.id,p.text,p.sha256,p.book_id,b.title,b.author,s.title section_title "
        "FROM passages p JOIN books b ON b.id=p.book_id "
        "JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
        "WHERE p.id=? AND b.status='indexed'",
        (identity,),
    ).fetchone()
    if row is None or hashlib.sha256(row["text"].encode()).hexdigest() != row["sha256"]:
        raise ValueError("Missing or corrupt canonical source passage")
    return row


def verify_compact_manifest(source: ReadOnlyLibrary, compact: Path, original: Path, receipt: dict[str, Any]) -> None:
    """Reconstruct every persisted row and compare the sealed input and chain."""
    expected_sha256 = receipt.get("database_sha256")
    if "database_sha256" in receipt and file_sha256(compact) != expected_sha256:
        raise ValueError("Compact sealed file checksum changed")
    original_receipt = receipt["original_receipt"]
    verify_batch_source(source, original_receipt)
    chain = "0" * 64
    count = 0
    passage = None
    counts = dict.fromkeys(original_receipt["rows_by_book"], 0)
    with (
        source.connect() as canonical,
        sqlite3.connect(compact.resolve().as_uri() + "?mode=ro", uri=True) as packed,
        sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True) as sealed,
    ):
        sql = (
            "SELECT w.id,w.passage_id,b.book_id,p.text,w.start,w.end,w.input_sha256 "
            "FROM windows w LEFT JOIN books b ON b.id=w.book_index "
            "LEFT JOIN prefixes p ON p.id=w.prefix_id ORDER BY w.id"
        )
        if packed.execute("SELECT COUNT(*) FROM windows").fetchone()[0] != original_receipt["rows"]:
            raise ValueError("Compact raw row count changed")
        old = iter(sealed.execute("SELECT id,passage_id,book_id,text,input_sha256 FROM windows ORDER BY id"))
        for row in packed.execute(sql):
            if row[2] is None or row[3] is None:
                raise ValueError("Compact window has a missing edition or prefix reference")
            if row[0] != count:
                raise ValueError("Compact window IDs are not dense")
            if passage is None or passage["id"] != row[1]:
                passage = _source_passage(canonical, row[1])
            if passage["book_id"] != row[2] or not 0 <= row[4] <= row[5] <= len(passage["text"]):
                raise ValueError("Compact window ownership or offsets changed")
            text = row[3] + "\n" + passage["text"][row[4] : row[5]]
            digest = hashlib.sha256(text.encode()).digest()
            previous = next(old, None)
            if previous != (row[0], row[1], row[2], text, digest.hex()) or row[6] != digest:
                raise ValueError("Compact reconstruction differs from the original sealed input")
            chain = hashlib.sha256(bytes.fromhex(chain) + Window(row[0], row[1], row[2], text).key()).hexdigest()
            counts[row[2]] += 1
            count += 1
        if next(old, None) is not None:
            raise ValueError("Compact representation omits sealed windows")
    if count != original_receipt["rows"] or counts != original_receipt["rows_by_book"]:
        raise ValueError("Compact row or edition counts changed")
    if chain != original_receipt["input_chain"]:
        raise ValueError("Compact input hashchain changed")
    verify_batch_source(source, original_receipt)
    if "database_sha256" in receipt and file_sha256(compact) != expected_sha256:
        raise ValueError("Compact sealed file checksum changed during verification")


def repack_window_manifest(
    source_directory: Path,
    original: Path,
    output: Path,
    tokenizer: Any,
    *,
    tokenizer_receipt: dict[str, Any] | None = None,
    reserve_bytes: int = RESERVE_BYTES,
    max_bytes: int = MAX_BYTES,
) -> dict[str, Any]:
    """Publish a new immutable prototype only after two exact full-stream checks."""
    started = time.monotonic()
    source = ReadOnlyLibrary(source_directory)
    original = original.resolve(strict=True)
    output = output.absolute()
    if output.is_symlink() or output.resolve().is_relative_to(source.data_dir):
        raise ValueError("Write outside the original library")
    if output.resolve().is_relative_to(original.parent) or output.exists():
        raise ValueError("Write a new owned file outside the sealed original batch")
    if (
        type(reserve_bytes) is not int
        or reserve_bytes < 0
        or type(max_bytes) is not int
        or not 4096 <= max_bytes <= MAX_BYTES
    ):
        raise ValueError("Invalid bounded output size or free-space reserve")
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt_path = output.with_suffix(output.suffix + ".receipt.json")
    if receipt_path.exists() or receipt_path.is_symlink():
        raise ValueError("An existing receipt cannot be overwritten")
    free_before = shutil.disk_usage(output.parent).free
    if free_before < reserve_bytes + max_bytes:
        raise RuntimeError("Insufficient disk for owned output plus configured reserve")
    old_receipt_path = original.with_suffix(original.suffix + ".receipt.json")
    original_receipt = json.loads(old_receipt_path.read_text())
    old_sha = file_sha256(original)
    if old_sha != original_receipt["database_sha256"]:
        raise ValueError("Original sealed window manifest hash changed")
    recipe = hashlib.sha256((inspect.getsource(_windows) + inspect.getsource(token_windows)).encode()).hexdigest()
    descriptor = original_receipt["descriptor"]
    if descriptor["window_recipe_sha256"] != recipe:
        raise ValueError("Production window recipe changed")
    if tokenizer_receipt is not None:
        space = descriptor["model_space"]
        if tokenizer_receipt["model"] != space["model"] or tokenizer_receipt["revision"] != space["revision"]:
            raise ValueError("Tokenizer pin differs from sealed model space")
        if not set(tokenizer_receipt["files_sha256"].values()) <= set(space["model_files_sha256"]):
            raise ValueError("Tokenizer files differ from the sealed model pin")
    verify_batch_source(source, original_receipt)
    staged = output.with_name(output.name + ".owned-staging-" + uuid.uuid4().hex)
    chain = "0" * 64
    rows = 0
    prefixes: dict[str, int] = {}
    books = {book: index for index, book in enumerate(descriptor["book_ids"])}
    counts = dict.fromkeys(books, 0)
    receipt: dict[str, Any] = {}
    owned_output_inode: tuple[int, int] | None = None
    try:
        with (
            source.connect() as canonical,
            sqlite3.connect(original.as_uri() + "?mode=ro", uri=True) as sealed,
            sqlite3.connect(staged) as packed,
        ):
            packed.executescript(
                "CREATE TABLE windows(id INTEGER PRIMARY KEY,passage_id INTEGER NOT NULL,book_index INTEGER NOT NULL,"
                "prefix_id INTEGER NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,input_sha256 BLOB NOT NULL);"
                "CREATE TABLE books(id INTEGER PRIMARY KEY,book_id TEXT NOT NULL UNIQUE);"
                "CREATE TABLE prefixes(id INTEGER PRIMARY KEY,text TEXT NOT NULL UNIQUE);"
                "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
            )
            packed.executemany("INSERT INTO books VALUES (?,?)", ((index, book) for book, index in books.items()))
            old = iter(sealed.execute("SELECT id,passage_id,book_id,text,input_sha256 FROM windows ORDER BY id"))
            pending = next(old, None)
            previous_passage = -1
            while pending is not None:
                passage_id = pending[1]
                if passage_id <= previous_passage:
                    raise ValueError("Original passage groups are not strictly ordered")
                previous_passage = passage_id
                passage = _source_passage(canonical, passage_id)
                for prefix, left, right in offset_windows(tokenizer, passage):
                    text = prefix + "\n" + passage["text"][left:right]
                    digest = hashlib.sha256(text.encode()).digest()
                    expected = (rows, passage_id, passage["book_id"], text, digest.hex())
                    if pending != expected:
                        raise ValueError("Tokenizer recipe or offsets do not reproduce the original sealed window")
                    if prefix not in prefixes:
                        prefixes[prefix] = len(prefixes)
                        packed.execute("INSERT INTO prefixes VALUES (?,?)", (prefixes[prefix], prefix))
                    packed.execute(
                        "INSERT INTO windows VALUES (?,?,?,?,?,?,?)",
                        (rows, passage_id, books[passage["book_id"]], prefixes[prefix], left, right, digest),
                    )
                    chain = hashlib.sha256(
                        bytes.fromhex(chain) + Window(rows, passage_id, passage["book_id"], text).key()
                    ).hexdigest()
                    rows += 1
                    counts[passage["book_id"]] += 1
                    pending = next(old, None)
                    if rows % 512 == 0:
                        if staged.stat().st_size > max_bytes or shutil.disk_usage(output.parent).free < reserve_bytes:
                            raise RuntimeError("Compact repack reached its disk limit or free-space reserve")
                if pending is not None and pending[1] == passage_id:
                    raise ValueError("Tokenizer recipe omitted original passage windows")
            if (
                rows != original_receipt["rows"]
                or chain != original_receipt["input_chain"]
                or counts != original_receipt["rows_by_book"]
            ):
                raise ValueError("Repacked stream does not match the sealed rows, editions or input chain")
            receipt = {
                "schema_version": 1,
                "kind": KIND,
                "prototype_only": True,
                "production_activation": False,
                "original_receipt": original_receipt,
                "original_manifest": str(original),
                "original_manifest_sha256": old_sha,
                "original_receipt_sha256": file_sha256(old_receipt_path),
                "rows": rows,
                "prefixes": len(prefixes),
                "books": len(books),
                "input_chain": chain,
                "offset_unit": "Python Unicode code points relative to canonical passage.text",
                "max_seq_length": 128,
                "tokenizer": tokenizer_receipt,
                "body_text_stored": False,
                "model_weights_loaded": False,
                "source_database_copied": False,
            }
            packed.execute("INSERT INTO metadata VALUES ('receipt',?)", (json.dumps(receipt, sort_keys=True),))
        if staged.stat().st_size > max_bytes:
            raise RuntimeError("Committed compact representation exceeded its byte budget")
        verify_compact_manifest(source, staged, original, receipt)
        if file_sha256(original) != old_sha or file_sha256(old_receipt_path) != receipt["original_receipt_sha256"]:
            raise ValueError("Original manifest changed during repack")
        if shutil.disk_usage(output.parent).free < reserve_bytes:
            raise RuntimeError("Free disk reached the reserve before publication")
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        info = staged.stat()
        owned_output_inode = (info.st_dev, info.st_ino)
        os.link(staged, output)
        receipt.update(
            database_sha256=file_sha256(output),
            original_bytes=original.stat().st_size,
            compact_bytes=output.stat().st_size,
            elapsed_seconds=time.monotonic() - started,
            free_bytes_before=free_before,
            free_bytes_after=shutil.disk_usage(output.parent).free,
            reconstructed_rows_verified_twice=rows,
            source_guards_verified_before_after=True,
        )
        _publish_json(receipt_path, receipt)
        return receipt
    except BaseException:
        if owned_output_inode is not None:
            try:
                info = output.stat(follow_symlinks=False)
                if (info.st_dev, info.st_ino) == owned_output_inode:
                    output.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        staged.unlink(missing_ok=True)
