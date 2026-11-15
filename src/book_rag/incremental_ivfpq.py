"""Experimental streaming standalone-code builder, separate from live retrieval.

Native operations require a dedicated FAISS-only CPU worker. Metadata sources
are read-only adapters; ready shards carry actual canonical/epoch bindings.
No model, production active pointer, original-source migration, or API is used.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from book_rag.experimental_ivfpq import canonical_bytes, faiss_backend, file_sha256, read_verified_index
from book_rag.index_coverage import CoverageLibrary, bind_coverage, capture_coverage, validate_coverage

ZERO = "0" * 64
MAX_SHARD_ROWS = 65536
Failpoint = Callable[[str, int], None]


@dataclass(frozen=True)
class Window:
    window_id: int
    passage_id: int
    book_id: str
    text: str

    def key(self) -> bytes:
        if type(self.window_id) is not int or not 0 <= self.window_id < 2**63:
            raise ValueError("Window ID must be a nonnegative int64")
        if type(self.passage_id) is not int or not 0 < self.passage_id < 2**63:
            raise ValueError("Passage ID must be a positive int64")
        if not isinstance(self.book_id, str) or not self.book_id or len(self.book_id) > 512:
            raise ValueError("Expected a bounded edition ID")
        if not isinstance(self.text, str) or not self.text or len(self.text) > 32768:
            raise ValueError("Expected bounded exact encoder input")
        return canonical_bytes(
            [self.window_id, self.passage_id, self.book_id, hashlib.sha256(self.text.encode()).hexdigest()]
        )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected a lowercase SHA256")
    return value


def _chain(previous: str, payload: bytes) -> str:
    return hashlib.sha256(bytes.fromhex(_sha(previous)) + payload).hexdigest()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic(path: Path, writer: Callable[[Path], object], *, replace: bool = False) -> str:
    if path.is_symlink():
        raise ValueError("Owned output cannot be a symlink")
    descriptor, name = tempfile.mkstemp(prefix=".incremental-owned-", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        writer(temporary)
        checksum = file_sha256(temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if file_sha256(path) != checksum:
                    raise ValueError("Immutable output differs") from None
        _sync_directory(path.parent)
        return checksum
    finally:
        temporary.unlink(missing_ok=True)


def _json(path: Path, value: Any, *, replace: bool = False) -> str:
    payload = canonical_bytes(value) + b"\n"
    return _atomic(path, lambda temporary: temporary.write_bytes(payload), replace=replace)


def _npz(path: Path, **arrays: np.ndarray) -> str:
    def write(temporary: Path) -> None:
        with temporary.open("wb") as handle:
            np.savez(handle, allow_pickle=False, **arrays)

    return _atomic(path, write)


def _load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024**2:
        raise ValueError("Missing, unsafe or oversized owned metadata")
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        raise ValueError("Expected JSON object")
    return result


@contextmanager
def _lock(directory: Path) -> Generator[None]:
    import fcntl

    path = directory / ".writer.lock"
    if path.is_symlink():
        raise ValueError("Writer lock cannot be a symlink")
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _build_token(source: CoverageLibrary, proof: dict[str, Any]) -> str:
    # An unbound tracked result intentionally has valid=False: this is only
    # an opaque staging-source token, never a ready-index coverage binding.
    state = validate_coverage(source, proof)
    if not state["tracked"]:
        raise ValueError("Explicit existing source epoch tracking is required")
    return state["state_token"]


def _initial_coverage(source: CoverageLibrary, books: list[str]) -> tuple[dict[str, Any], str]:
    """Capture canonical proof and build epochs in the SAME SQL snapshot."""
    path = source.db_path.resolve()
    before = path.stat()
    with source.connect() as connection:
        owned = not connection.in_transaction
        if owned:
            connection.execute("BEGIN")

        class SnapshotSource:
            db_path = path

            @contextmanager
            def connect(self) -> Generator[sqlite3.Connection]:
                yield connection

        try:
            proof = capture_coverage(connection, books)
            token = _build_token(SnapshotSource(), proof)
        finally:
            if owned:
                connection.rollback()
    after = path.stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError("Source database replaced during initial proof")
    return proof, token


def _guard(source: CoverageLibrary, descriptor: dict[str, Any]) -> None:
    if _build_token(source, descriptor["source_coverage"]) != descriptor["build_source_token"]:
        raise ValueError("Covered source changed while building; no automatic rebind")


def _records(records: list[Window], start: int, books: list[str]) -> tuple[np.ndarray, np.ndarray]:
    if any(record.window_id != start + j for j, record in enumerate(records)):
        raise ValueError("Stream window IDs must be dense, ordered and uninterrupted")
    book_index = {identity: j for j, identity in enumerate(books)}
    for record in records:
        record.key()
        if record.book_id not in book_index:
            raise ValueError("Window edition is outside the declared coverage")
    return (
        np.array([record.passage_id for record in records], dtype=np.int64),
        np.array([book_index[record.book_id] for record in records], dtype=np.uint16),
    )


def _check_ownership(source: CoverageLibrary, records: list[Window]) -> dict[int, tuple[str, str]]:
    with source.connect() as conn:
        found = {
            int(row[0]): (row[1], row[2])
            for row in conn.execute(
                "SELECT p.id,p.book_id,p.sha256 FROM passages p JOIN books b ON b.id=p.book_id "
                "JOIN json_each(?) x ON x.value=p.id WHERE b.status='indexed'",
                (json.dumps(sorted({record.passage_id for record in records})),),
            )
        }
    if any(record.passage_id not in found or found[record.passage_id][0] != record.book_id for record in records):
        raise ValueError("Stream passage/edition ownership differs from canonical source")
    return found


def _chunk_name(number: int) -> str:
    return f"chunk-{number:08d}"


def _shard_name(start: int, end: int) -> str:
    return f"shard-{start:09d}-{end:09d}"


def _read_chunk(directory: Path, receipt: dict[str, Any], code_width: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    path = directory / (_chunk_name(receipt["number"]) + ".npz")
    if file_sha256(path) != receipt["payload_sha256"]:
        raise ValueError("Sealed code chunk checksum mismatch")
    with np.load(path, allow_pickle=False) as value:
        if set(value.files) != {"codes", "passages", "books"}:
            raise ValueError("Invalid code chunk array fields")
        codes, passages, books = value["codes"], value["passages"], value["books"]
    count = receipt["end"] - receipt["start"]
    if codes.dtype != np.uint8 or codes.shape != (count, code_width):
        raise ValueError("Invalid standalone code shape")
    if passages.dtype != np.int64 or passages.shape != (count,) or np.any(passages <= 0):
        raise ValueError("Invalid chunk passage mapping")
    if books.dtype != np.uint16 or books.shape != (count,):
        raise ValueError("Invalid chunk edition mapping")
    return codes, passages, books


def _coarse_codes(codec: Any, codes: np.ndarray) -> np.ndarray:
    coarse_width = int(codec.sa_code_size()) - int(codec.code_size)
    if not 1 <= coarse_width <= 8 or codes.dtype != np.uint8 or codes.shape[1] != codec.sa_code_size():
        raise ValueError("Unsupported standalone coarse-list encoding")
    coarse = np.zeros(len(codes), dtype=np.uint64)
    for j in range(coarse_width):
        coarse |= codes[:, j].astype(np.uint64) << (8 * j)
    if np.any(coarse >= codec.nlist):
        raise ValueError("Standalone code coarse-list ID is out of bounds")
    return coarse


def _verify_native(index: Any, codec: Any, codes: np.ndarray, start: int) -> None:
    backend = faiss_backend()
    if index.d != codec.d or index.nlist != codec.nlist or index.ntotal != len(codes):
        raise ValueError("Native shard metadata differs from declared code stream")
    empty = backend.clone_index(index)
    empty.reset()
    if not np.array_equal(backend.serialize_index(empty), backend.serialize_index(codec)):
        raise ValueError("Native shard uses a different codec/model space")
    width = int(codec.code_size)
    coarse_width = int(codec.sa_code_size()) - width
    coarse = _coarse_codes(codec, codes)
    seen = np.zeros(len(codes), dtype=np.bool_)
    for list_id in range(index.nlist):
        count = index.invlists.list_size(list_id)
        if not count:
            continue
        rows = backend.rev_swig_ptr(index.invlists.get_ids(list_id), count)
        local = rows - start
        if np.any(local < 0) or np.any(local >= len(codes)) or len(np.unique(local)) != count or np.any(seen[local]):
            raise ValueError("Native shard window membership differs")
        payload = backend.rev_swig_ptr(index.invlists.get_codes(list_id), count * width).reshape(count, width)
        if not np.all(coarse[local] == list_id) or not np.array_equal(payload, codes[local, coarse_width:]):
            raise ValueError("Native shard code bytes/order differ from sealed chunks")
        seen[local] = True
    if not seen.all():
        raise ValueError("Native shard omits windows")


def _shard_receipt(directory: Path, start: int, end: int) -> dict[str, Any] | None:
    name = _shard_name(start, end)
    path = directory / (name + ".receipt.json")
    if not path.exists():
        return None
    receipt = _load_json(path)
    if receipt["start"] != start or receipt["end"] != end:
        raise ValueError("Shard boundary mismatch")
    for suffix, key in ((".faiss", "index_sha256"), (".mapping.npz", "mapping_sha256")):
        if file_sha256(directory / (name + suffix)) != receipt[key]:
            raise ValueError("Sealed shard artifact checksum mismatch")
    for chunk in receipt["chunks"]:
        chunk_path = directory / (_chunk_name(chunk["number"]) + ".json")
        if file_sha256(chunk_path) != chunk["receipt_sha256"]:
            raise ValueError("Sealed shard chunk receipt changed")
    return receipt


def _shard_mapping(directory: Path, receipt: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    name = _shard_name(receipt["start"], receipt["end"])
    path = directory / (name + ".mapping.npz")
    if file_sha256(path) != receipt["mapping_sha256"]:
        raise ValueError("Sealed mapping checksum mismatch before read")
    with np.load(path, allow_pickle=False) as value:
        passages, books = value["passages"], value["books"]
    if file_sha256(path) != receipt["mapping_sha256"]:
        raise ValueError("Sealed mapping changed during read")
    count = receipt["end"] - receipt["start"]
    if passages.dtype != np.int64 or passages.shape != (count,) or books.dtype != np.uint16 or books.shape != (count,):
        raise ValueError("Invalid sealed shard mapping")
    return passages, books


def _assemble(
    directory: Path,
    descriptor: dict[str, Any],
    start: int,
    end: int,
    codec: Any,
    source: CoverageLibrary,
    failpoint: Failpoint,
) -> dict[str, Any]:
    existing: dict[str, Any] | None = _shard_receipt(directory, start, end)
    name = _shard_name(start, end)
    if existing is None:
        chunks = []
        arrays = []
        for number in range(
            start // descriptor["chunk_rows"], (end + descriptor["chunk_rows"] - 1) // descriptor["chunk_rows"]
        ):
            receipt_path = directory / (_chunk_name(number) + ".json")
            chunk = _load_json(receipt_path)
            arrays.append(_read_chunk(directory, chunk, descriptor["standalone_code_bytes"]))
            chunks.append({"number": number, "receipt_sha256": file_sha256(receipt_path)})
        codes = np.concatenate([value[0] for value in arrays])
        passages = np.concatenate([value[1] for value in arrays])
        books = np.concatenate([value[2] for value in arrays])
        backend = faiss_backend()
        index = backend.clone_index(codec)
        _coarse_codes(codec, codes)
        index.add_sa_codes(codes, np.arange(start, end, dtype=np.int64))
        _verify_native(index, codec, codes, start)
        for suffix in (".faiss", ".mapping.npz"):
            orphan = directory / (name + suffix)
            if orphan.exists():
                if orphan.is_symlink():
                    raise ValueError("Unsealed shard artifact is a symlink")
                orphan.unlink()
        index_sha = _atomic(directory / (name + ".faiss"), lambda p: backend.write_index(index, str(p)))
        failpoint("shard-index-published", start)
        mapping_sha = _npz(directory / (name + ".mapping.npz"), passages=passages, books=books)
        covered = sorted({descriptor["covered_book_ids"][int(value)] for value in books})
        with source.connect() as conn:
            proof = capture_coverage(conn, covered)
        _guard(source, descriptor)
        existing = {
            "schema_version": 1,
            "identity": descriptor["identity"],
            "start": start,
            "end": end,
            "index_sha256": index_sha,
            "mapping_sha256": mapping_sha,
            "chunks": chunks,
            "coverage": proof,
            "model_space_sha256": descriptor["model_space_sha256"],
        }
        _json(directory / (name + ".receipt.json"), existing)
        failpoint("shard-receipt-published", start)
    if existing["identity"] != descriptor["identity"]:
        raise ValueError("Shard belongs to another source/model generation")
    binding_path = directory / (name + ".binding.json")
    receipt_sha = file_sha256(directory / (name + ".receipt.json"))
    if binding_path.exists():
        binding = _load_json(binding_path)
        if (
            binding["receipt_sha256"] != receipt_sha
            or not validate_coverage(source, existing["coverage"], binding)["valid"]
        ):
            raise ValueError("Ready shard source binding changed; no automatic rebind")
    else:
        binding = bind_coverage(source, existing["coverage"], receipt_sha256=receipt_sha)
        _json(binding_path, binding)
    # Immutable receipts remain for logical-prefix replay; only redundant code
    # payloads are reclaimed after index+mapping+binding are sealed and checked.
    for chunk in existing["chunks"]:
        number = chunk["number"]
        if type(number) is not int:
            raise ValueError("Invalid sealed chunk number")
        (directory / (_chunk_name(number) + ".npz")).unlink(missing_ok=True)
    _sync_directory(directory)
    return {
        "start": start,
        "end": end,
        "receipt_sha256": receipt_sha,
        "binding_sha256": file_sha256(binding_path),
        "covered_book_ids": existing["coverage"]["covered_book_ids"],
    }


def build_incremental(
    source: CoverageLibrary,
    windows: Iterable[Window],
    encode: Callable[[list[Window]], np.ndarray],
    output: Path,
    codec_path: Path,
    codec_sha256: str,
    model_space: dict[str, Any],
    rows_by_book: Mapping[str, int],
    *,
    chunk_rows: int = 256,
    shard_rows: int = 8192,
    stop_after_chunks: int | None = None,
    failpoint: Failpoint | None = None,
) -> dict[str, Any]:
    """Encode only missing bounded chunks, seal shards, then publish ready.

    The encode callback must run encoding externally to this FAISS process.
    The pilot callback reads existing frozen vectors; it never loads Torch.
    """
    if not 1 <= len(rows_by_book) <= 50000 or any(type(n) is not int or n < 1 for n in rows_by_book.values()):
        raise ValueError("Expected explicit positive complete window counts per covered edition")
    if any(not isinstance(value, str) or not value or len(value) > 512 for value in rows_by_book):
        raise ValueError("Invalid covered edition identity")
    if type(chunk_rows) is not int or not 1 <= chunk_rows <= 4096:
        raise ValueError("Invalid bounded chunk size")
    if type(shard_rows) is not int or not chunk_rows <= shard_rows <= MAX_SHARD_ROWS or shard_rows % chunk_rows:
        raise ValueError("Shard size must be a bounded multiple of chunk size")
    if stop_after_chunks is not None and (type(stop_after_chunks) is not int or stop_after_chunks < 1):
        raise ValueError("Invalid interruption point")
    if len(canonical_bytes(model_space)) > 32768:
        raise ValueError("Oversized model-space descriptor")
    if not {"model", "revision", "dimensions", "profile", "strategy", "assignment"}.issubset(model_space):
        raise ValueError("Model space must pin model/revision/profile/strategy and input assignment")
    for key in ("model", "revision", "strategy", "assignment"):
        if not isinstance(model_space[key], str) or not 0 < len(model_space[key]) <= 2048:
            raise ValueError("Model-space identities must be bounded nonempty strings")
    if not re.fullmatch(r"[0-9a-f]{40,64}", model_space["revision"]) or not isinstance(model_space["profile"], dict):
        raise ValueError("Model space needs an immutable revision and explicit profile")
    backend = faiss_backend()
    backend.omp_set_num_threads(2)
    codec = read_verified_index(codec_path, _sha(codec_sha256))
    if not isinstance(codec, backend.IndexIVFPQ) or not codec.is_trained or codec.ntotal:
        raise ValueError("Expected an empty trained IVF-PQ codec")
    if model_space.get("dimensions") != codec.d:
        raise ValueError("Codec/model-space dimension mismatch")
    books = sorted(rows_by_book)
    proof, build_source_token = _initial_coverage(source, books)
    descriptor = {
        "schema_version": 1,
        "kind": "experimental-incremental-IVFPQ",
        "model_space": model_space,
        "model_space_sha256": digest(model_space),
        "codec_sha256": codec_sha256,
        "codec_implementation": "faiss-cpu-" + backend.__version__,
        "dimensions": codec.d,
        "nlist": codec.nlist,
        "standalone_code_bytes": codec.sa_code_size(),
        "source_coverage": proof,
        "build_source_token": build_source_token,
        "covered_book_ids": books,
        "rows_by_book": dict(sorted(rows_by_book.items())),
        "rows": sum(rows_by_book.values()),
        "chunk_rows": chunk_rows,
        "shard_rows": shard_rows,
        "input_order": "ascending-passage-id-with-contiguous-windows",
    }
    identity = digest(descriptor)
    descriptor["identity"] = identity
    directory = output.resolve() / ("incremental-" + identity)
    directory.mkdir(parents=True, exist_ok=True)
    event = failpoint or (lambda _event, _number: None)
    with _lock(directory):
        _json(directory / "descriptor.json", descriptor)
        codec_target = directory / "codec.faiss"
        if not codec_target.exists():
            os.link(codec_path, codec_target)
            _sync_directory(directory)
        if file_sha256(codec_target) != codec_sha256:
            raise ValueError("Immutable codec changed")
        checkpoint_path = directory / "checkpoint.json"
        checkpoint = _load_json(checkpoint_path) if checkpoint_path.exists() else None
        if checkpoint is not None:
            fields = {"schema_version", "identity", "completed_chunks", "completed_rows", "input_chain", "chunk_chain"}
            if set(checkpoint) != fields or checkpoint["schema_version"] != 1 or checkpoint["identity"] != identity:
                raise ValueError("Checkpoint input/model identity mismatch")
            chunks, completed = checkpoint["completed_chunks"], checkpoint["completed_rows"]
            if type(chunks) is not int or type(completed) is not int or not 0 <= completed <= descriptor["rows"]:
                raise ValueError("Invalid checkpoint counters")
            if not 0 <= chunks <= (descriptor["rows"] + chunk_rows - 1) // chunk_rows or completed != min(
                chunks * chunk_rows, descriptor["rows"]
            ):
                raise ValueError("Checkpoint is not on a durable chunk boundary")
            _sha(checkpoint["input_chain"])
            _sha(checkpoint["chunk_chain"])
            if not chunks and (checkpoint["input_chain"] != ZERO or checkpoint["chunk_chain"] != ZERO):
                raise ValueError("Invalid empty checkpoint chain")
        iterator = iter(windows)
        input_chain = chunk_chain = ZERO
        source_passages = hashlib.sha256()
        last_passage = 0
        unique_passages = 0
        counts = dict.fromkeys(books, 0)
        created = resumed = 0
        shards = []
        cached_shard: tuple[int, dict[str, Any], np.ndarray, np.ndarray] | None = None
        checkpoint_matched = checkpoint is None or checkpoint["completed_rows"] == 0
        for number, start in enumerate(range(0, descriptor["rows"], chunk_rows)):
            _guard(source, descriptor)
            end = min(start + chunk_rows, descriptor["rows"])
            records = list(itertools.islice(iterator, end - start))
            if len(records) != end - start:
                raise ValueError("Encoder input ended before declared coverage")
            passages, book_codes = _records(records, start, books)
            ownership = _check_ownership(source, records)
            before_chain = input_chain
            for record in records:
                if record.passage_id < last_passage:
                    raise ValueError("Input passages must be ordered with contiguous windows")
                if record.passage_id != last_passage:
                    source_passages.update(f"{record.passage_id}:{ownership[record.passage_id][1]}\n".encode())
                    last_passage = record.passage_id
                    unique_passages += 1
                input_chain = _chain(input_chain, record.key())
                counts[record.book_id] += 1
            receipt_path = directory / (_chunk_name(number) + ".json")
            expected = {
                "schema_version": 1,
                "identity": identity,
                "number": number,
                "start": start,
                "end": end,
                "input_chain_before": before_chain,
                "input_chain_after": input_chain,
            }
            if receipt_path.exists():
                receipt = _load_json(receipt_path)
                if any(receipt.get(key) != value for key, value in expected.items()):
                    raise ValueError("Completed chunk encoder input changed")
                payload = directory / (_chunk_name(number) + ".npz")
                if payload.exists():
                    _, old_passages, old_books = _read_chunk(directory, receipt, descriptor["standalone_code_bytes"])
                else:
                    shard_start = start // shard_rows * shard_rows
                    shard_end = min(shard_start + shard_rows, descriptor["rows"])
                    if cached_shard is None or cached_shard[0] != shard_start:
                        sealed = _shard_receipt(directory, shard_start, shard_end)
                        if sealed is None:
                            raise ValueError("Missing completed code payload without sealed shard")
                        mapped_passages, mapped_books = _shard_mapping(directory, sealed)
                        cached_shard = (shard_start, sealed, mapped_passages, mapped_books)
                    _, _, mapped_passages, mapped_books = cached_shard
                    local = start - shard_start
                    old_passages, old_books = (
                        mapped_passages[local : local + len(records)],
                        mapped_books[local : local + len(records)],
                    )
                if not np.array_equal(old_passages, passages) or not np.array_equal(old_books, book_codes):
                    raise ValueError("Completed chunk source mapping changed")
                resumed += 1
            else:
                if checkpoint is not None and end <= checkpoint["completed_rows"]:
                    raise ValueError("Missing sealed chunk within checkpoint prefix")
                payload = directory / (_chunk_name(number) + ".npz")
                if payload.exists():
                    if payload.is_symlink():
                        raise ValueError("Unsealed chunk payload is a symlink")
                    payload.unlink()
                vectors = encode(records)
                if (
                    vectors.dtype != np.float32
                    or vectors.shape != (len(records), codec.d)
                    or not np.isfinite(vectors).all()
                ):
                    raise ValueError("Encoder returned invalid bounded float32 vectors")
                if np.any(np.abs(np.linalg.norm(vectors, axis=1) - 1) > 0.001):
                    raise ValueError("Expected unit-normalized cosine vectors")
                codes = codec.sa_encode(np.ascontiguousarray(vectors))
                receipt = {
                    **expected,
                    "payload_sha256": _npz(payload, codes=codes, passages=passages, books=book_codes),
                }
                event("chunk-payload-published", number)
                _guard(source, descriptor)
                _json(receipt_path, receipt)
                created += 1
                event("chunk-receipt-published", number)
            chunk_chain = _chain(chunk_chain, bytes.fromhex(file_sha256(receipt_path)))
            cursor = {
                "schema_version": 1,
                "identity": identity,
                "completed_chunks": number + 1,
                "completed_rows": end,
                "input_chain": input_chain,
                "chunk_chain": chunk_chain,
            }
            if checkpoint is not None and end == checkpoint["completed_rows"]:
                if cursor != checkpoint:
                    raise ValueError("Checkpoint differs from verified durable prefix")
                checkpoint_matched = True
            if checkpoint is None or end >= checkpoint["completed_rows"]:
                _json(checkpoint_path, cursor, replace=True)
            event("checkpoint-published", number)
            if end % shard_rows == 0 or end == descriptor["rows"]:
                shards.append(
                    _assemble(directory, descriptor, end - (end % shard_rows or shard_rows), end, codec, source, event)
                )
            if stop_after_chunks is not None and created >= stop_after_chunks:
                return {
                    "complete": False,
                    "directory": str(directory),
                    "created_chunks": created,
                    "resumed_chunks": resumed,
                    "durable_rows": end,
                    "identity": identity,
                }
        if not checkpoint_matched:
            raise ValueError("Checkpoint claims nonexistent durable rows")
        if next(iterator, None) is not None or counts != dict(rows_by_book):
            raise ValueError("Encoder input exceeds or differs from declared per-book coverage")
        if unique_passages != proof["passages"] or source_passages.hexdigest() != proof["passage_fingerprint"]:
            raise ValueError("Encoder input omits canonical passages from claimed edition coverage")
        _guard(source, descriptor)
        ready = {
            "schema_version": 1,
            "identity": identity,
            "descriptor_sha256": file_sha256(directory / "descriptor.json"),
            "coverage_kind": "explicit-subset",
            "model_space_sha256": descriptor["model_space_sha256"],
            "rows": descriptor["rows"],
            "input_chain": input_chain,
            "chunk_chain": chunk_chain,
            "shards": shards,
            "source_passage_coverage_verified": True,
            "source_passage_fingerprint": source_passages.hexdigest(),
        }
        event("before-ready-publication", descriptor["rows"])
        _json(directory / "ready.json", ready)
        _guard(source, descriptor)
        return {
            "complete": True,
            "directory": str(directory),
            "identity": identity,
            "created_chunks": created,
            "resumed_chunks": resumed,
            "durable_rows": descriptor["rows"],
            "ready_sha256": file_sha256(directory / "ready.json"),
        }


def query_incremental(
    source: CoverageLibrary,
    directory: Path,
    vector: np.ndarray,
    model_space: dict[str, Any],
    *,
    book_id: str | None = None,
    language: str | None = None,
    limit: int = 10,
    nprobe: int = 32,
    exclude_passage: int | None = None,
    after_search: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Fixed candidate pool, actual native scoped selectors and pre/post guards."""
    if type(limit) is not int or not 1 <= limit <= 100 or type(nprobe) is not int or nprobe < 1:
        raise ValueError("Invalid bounded query parameters")
    if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in (book_id, language)):
        raise ValueError("Scope must be explicit or absent, never blank")
    ready = _load_json(directory / "ready.json")
    descriptor = _load_json(directory / "descriptor.json")
    if (
        file_sha256(directory / "descriptor.json") != ready["descriptor_sha256"]
        or ready["identity"] != descriptor["identity"]
    ):
        raise ValueError("Ready descriptor identity mismatch")
    expected_ranges = [
        (start, min(start + descriptor["shard_rows"], descriptor["rows"]))
        for start in range(0, descriptor["rows"], descriptor["shard_rows"])
    ]
    if [(item["start"], item["end"]) for item in ready["shards"]] != expected_ranges:
        raise ValueError("Ready generation omits, duplicates or reorders shards")
    if (
        ready.get("source_passage_coverage_verified") is not True
        or ready.get("source_passage_fingerprint") != descriptor["source_coverage"]["passage_fingerprint"]
    ):
        raise ValueError("Ready generation has no complete canonical-passage proof")
    if digest(model_space) != descriptor["model_space_sha256"] or ready["model_space_sha256"] != digest(model_space):
        raise ValueError("Query model space differs from stored codes")
    if vector.dtype != np.float32 or vector.shape != (1, descriptor["dimensions"]) or not np.isfinite(vector).all():
        raise ValueError("Expected one finite float32 query vector")
    if np.any(np.abs(np.linalg.norm(vector, axis=1) - 1) > 0.001) or nprobe > descriptor["nlist"]:
        raise ValueError("Query norm or nprobe is invalid")
    valid = []
    invalid_books: set[str] = set()
    for item in ready["shards"]:
        name = _shard_name(item["start"], item["end"])
        receipt_path, binding_path = directory / (name + ".receipt.json"), directory / (name + ".binding.json")
        if file_sha256(receipt_path) != item["receipt_sha256"] or file_sha256(binding_path) != item["binding_sha256"]:
            raise ValueError("Ready shard receipt/binding changed")
        receipt, binding = _load_json(receipt_path), _load_json(binding_path)
        if (
            receipt["identity"] != descriptor["identity"]
            or receipt["model_space_sha256"] != descriptor["model_space_sha256"]
            or receipt["coverage"]["covered_book_ids"] != item["covered_book_ids"]
        ):
            raise ValueError("Ready shard source/model identity mismatch")
        if binding["receipt_sha256"] != item["receipt_sha256"]:
            raise ValueError("Shard binding points at another index receipt")
        state = validate_coverage(source, receipt["coverage"], binding)
        if state["valid"]:
            valid.append((item, receipt, binding, state))
        else:
            invalid_books.update(item["covered_book_ids"])
    covered = set(descriptor["covered_book_ids"]) - invalid_books
    with source.connect() as conn:
        indexed = conn.execute("SELECT count(*) FROM books WHERE status='indexed'").fetchone()[0]
        scoped = {
            row[0]
            for row in conn.execute("SELECT id,language FROM books WHERE status='indexed'")
            if (book_id is None or row[0] == book_id) and (language is None or row[1] == language)
        }
    eligible = covered & scoped
    warnings = [
        f"Experimental compressed semantic coverage: {len(covered)}/{indexed} indexed editions; "
        "lexical fallback required outside coverage"
    ]
    result = {
        "method": "experimental-IVFPQ-approximate-inner-product",
        "coverage_kind": "explicit-subset",
        "covered_books": len(covered),
        "indexed_books": indexed,
        "covered_book_ids": sorted(covered),
        "warnings": warnings,
        "results": [],
        "fallback_required": not bool(eligible),
        "source_changed": False,
    }
    if not eligible:
        return result
    backend = faiss_backend()
    backend.omp_set_num_threads(2)
    hits: dict[int, dict[str, Any]] = {}
    used = []
    for item, receipt, binding, before in valid:
        if not (set(item["covered_book_ids"]) & eligible):
            continue
        verified = _shard_receipt(directory, item["start"], item["end"])
        if verified != receipt:
            raise ValueError("Shard source receipt changed during query")
        passages, books = _shard_mapping(directory, receipt)
        permitted = np.array([descriptor["covered_book_ids"][int(value)] in eligible for value in books])
        permitted &= passages != exclude_passage if exclude_passage is not None else True
        rows = np.flatnonzero(permitted).astype(np.int64) + item["start"]
        if not len(rows):
            continue
        name = _shard_name(item["start"], item["end"])
        index = read_verified_index(directory / (name + ".faiss"), receipt["index_sha256"])
        parameters = backend.IVFPQSearchParameters()
        parameters.nprobe = nprobe
        selector = backend.IDSelectorBatch(rows)
        parameters.sel = selector
        scores, candidates = index.search(vector, min(4096, len(rows)), params=parameters)
        for score, window_id in zip(scores[0], candidates[0], strict=True):
            if window_id < 0:
                continue
            local = int(window_id) - item["start"]
            if not 0 <= local < len(passages) or not permitted[local] or not np.isfinite(score):
                raise ValueError("Native scoped candidate is invalid")
            passage = int(passages[local])
            hit = {
                "passage_id": passage,
                "book_id": descriptor["covered_book_ids"][int(books[local])],
                "window_id": int(window_id),
                "score": float(score),
            }
            previous = hits.get(passage)
            if previous is None or (-hit["score"], hit["window_id"]) < (-previous["score"], previous["window_id"]):
                hits[passage] = hit
        if len(hits) > 1000:
            hits = {
                hit["passage_id"]: hit
                for hit in sorted(hits.values(), key=lambda h: (-h["score"], h["window_id"]))[:1000]
            }
        used.append((receipt, binding, before))
    if after_search is not None:
        after_search()
    if any(
        not (after := validate_coverage(source, receipt["coverage"], binding))["valid"]
        or after["state_token"] != before["state_token"]
        for _, receipt, binding, before in valid
    ):
        result["warnings"].append("Covered source changed during query; stale candidates discarded")
        result["source_changed"] = True
        result["fallback_required"] = True
        return result
    result["results"] = sorted(hits.values(), key=lambda value: (-value["score"], value["window_id"]))[:limit]
    if not result["results"]:
        result["fallback_required"] = True
        result["warnings"].append(
            "ANN returned no eligible candidates; use lexical fallback rather than claiming absence"
        )
    return result
