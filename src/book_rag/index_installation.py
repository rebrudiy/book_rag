"""Explicit, fail-closed installation of a completed generation as subset coverage.

No Library constructor, model encoder or network access is used. The source
library must be a static SQLite snapshot with no pending WAL. Completed source
files remain byte-for-byte immutable, including when target arrays are hardlinks.
Calling this helper is a separate authorization from building/staging an index.
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from book_rag.artifacts import _publish_no_replace
from book_rag.embedding_profiles import profile_from_metadata
from book_rag.index_coverage import bind_coverage, capture_coverage, ensure_epoch_schema, validate_coverage
from book_rag.vector_reuse import KEYMAP, MAX_BATCH, RECEIPT, GenerationReuse, ModelSpace, window_key

INSTALLATION_SCHEMA = 1
DISK_RESERVE_BYTES = 4 * 1024**3
GENERATION_FILES = ("embeddings.npy", "ids.npy", KEYMAP, "metadata.json", "checkpoint.json", RECEIPT)
_COPY_ERRORS = {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP, errno.EMLINK}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(path: Path) -> dict[str, Any]:
    if _regular(path).st_size > 32 * 1024**2:
        raise ValueError("Index JSON metadata exceeds the bounded size limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path.name}")
    return value


def _regular(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size < 1:
        raise ValueError(f"Expected a nonempty regular file, without symlinks: {path.name}")
    return info


def _directory(path: Path, *, create: bool = False) -> Path:
    candidate = path.expanduser().absolute()
    for component in [*reversed(candidate.parents), candidate]:
        if component.is_symlink():
            raise ValueError("Index paths must not contain symlinks")
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    if not candidate.is_dir():
        raise ValueError(f"Expected an existing directory: {candidate.name}")
    return candidate.resolve()


def _state(path: Path) -> tuple[int, int, int, int]:
    info = _regular(path)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _close_mapping(array: Any) -> None:
    mapping = getattr(array, "_mmap", None)
    if mapping is not None:
        mapping.close()


def _source_quiet(database: Path) -> None:
    wal = database.with_name(database.name + "-wal")
    if wal.is_symlink() or (wal.exists() and wal.stat().st_size):
        raise ValueError("Source must remain a static checkpointed snapshot without a pending WAL")


def _disk_space(directory: Path, needed: int) -> None:
    if shutil.disk_usage(directory).free < needed + DISK_RESERVE_BYTES:
        raise OSError(errno.ENOSPC, "Installation needs complete output bytes plus a 4 GiB free-disk reserve")


@dataclass(slots=True)
class _Database:
    db_path: Path
    readonly: bool = False

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        location = self.db_path.as_uri() + "?mode=ro&immutable=1" if self.readonly else str(self.db_path)
        conn = sqlite3.connect(location, uri=self.readonly, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()


@contextmanager
def _build_lock(directory: Path) -> Generator[None]:
    path = directory / ".build.lock"
    if path.is_symlink():
        raise ValueError("Build lock must not be a symlink")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "a+b") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Build lock must be a regular file")
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            if not stream.read(1):
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("An index build or installation already owns this target") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _load_tokenizer(space: ModelSpace, cache: Path) -> Any:
    try:
        transformers = importlib.import_module("transformers")
        return transformers.AutoTokenizer.from_pretrained(
            space.model,
            revision=space.revision,
            cache_dir=str(cache),
            local_files_only=True,
            trust_remote_code=False,
        )
    except (ImportError, OSError) as exc:
        raise ValueError(
            "A locally cached tokenizer at the exact pinned revision is required; no download attempted"
        ) from exc


def _ids(folder: Path, count: int, checkpoint: Mapping[str, Any]) -> Any:
    array = np.load(folder / "ids.npy", mmap_mode="r", allow_pickle=False)
    try:
        if not isinstance(array, np.memmap) or array.dtype != np.dtype("int64") or array.shape != (count,):
            raise ValueError("Expected one int64 passage-ID row for every completed vector")
        if (folder / "ids.npy").stat().st_size != array.offset + array.nbytes:
            raise ValueError("Passage-ID file has trailing or truncated bytes")
        checksum = hashlib.sha256()
        for start in range(0, count, 4096):
            block = array[start : start + 4096]
            if np.any(block < 1):
                raise ValueError("Passage IDs must be positive")
            checksum.update(block.tobytes(order="C"))
        if checksum.hexdigest() != checkpoint.get("id_checksum"):
            raise ValueError("Completed passage-ID checksum differs from checkpoint")
        return array
    except BaseException:
        mmap = getattr(array, "_mmap", None)
        if mmap is not None:
            mmap.close()
        raise


def _input_proof(
    conn: sqlite3.Connection,
    reuse: GenerationReuse,
    ids: Any,
    metadata: dict[str, Any],
    checkpoint: dict[str, Any],
    tokenizer: Any,
    first_rows: Any,
) -> dict[str, Any]:
    # Import the frozen implementation lazily; query receipt checks never load a
    # model/tokenizer or create a semantic/index-installation import cycle.
    from book_rag.semantic import _profile_windows

    if (
        metadata.get("strategy") not in {"token-windows-v2", "token-windows-e5-v1"}
        or metadata.get("overlap_tokens") != 20
        or metadata.get("context_header_tokens") != 24
    ):
        raise ValueError("Unsupported exact-input window strategy")
    profile = profile_from_metadata(metadata)
    if profile.strategy != metadata["strategy"]:
        raise ValueError("Frozen profile/window strategy mismatch")
    model = SimpleNamespace(tokenizer=tokenizer, max_seq_length=reuse.space.max_tokens)
    if reuse.connection is None:
        raise ValueError("Ready key map is closed")
    ordered = reuse.connection.execute("SELECT key,row_index FROM window_keys ORDER BY row_index")
    next_unique = ordered.fetchone()
    full_checksum = hashlib.sha256()
    unique_checksum = hashlib.sha256()
    offset = passages = unique_count = different_duplicates = 0
    last_cursor = None
    for row in conn.execute(
        "SELECT p.id,p.text,b.title,b.author,s.title section_title FROM passages p "
        "JOIN books b ON b.id=p.book_id JOIN sections s ON s.id=p.section_id "
        "WHERE b.status='indexed' ORDER BY p.id"
    ):
        windows = _profile_windows(model, row, profile)
        if not windows:
            raise ValueError("A claimed covered passage has no encoder input windows")
        for start in range(0, len(windows), MAX_BATCH):
            inputs = windows[start : start + MAX_BATCH]
            keys = [window_key(text, reuse.space) for text in inputs]
            positions = dict(
                reuse.connection.execute(
                    "SELECT key,row_index FROM window_keys WHERE key IN (" + ",".join("?" for _ in keys) + ")", keys
                )
            )
            for index, key in enumerate(keys):
                position = offset + index
                if position >= len(ids) or int(ids[position]) != row["id"]:
                    raise ValueError("Every window ID must match its exact source passage in order")
                full_checksum.update(key + position.to_bytes(8, "big"))
                first = positions.get(key)
                if type(first) is not int or not 0 <= first <= position:
                    raise ValueError("Window has no verified exact-input first row")
                first_rows[position] = first
                if next_unique is not None and next_unique[1] == position:
                    if next_unique[0] != key or positions.get(key) != position:
                        raise ValueError("First-row encoder input differs from ready key map")
                    unique_checksum.update(key + position.to_bytes(8, "big"))
                    unique_count += 1
                    next_unique = ordered.fetchone()
                elif positions.get(key, position) >= position or (
                    next_unique is not None and next_unique[1] < position
                ):
                    raise ValueError("Duplicate input does not refer to an already verified first row")
                elif reuse.vectors[position].tobytes() != reuse.vectors[first].tobytes():
                    different_duplicates += 1
                last_cursor = {"passage_id": row["id"], "window_index": start + index}
            offset += len(keys)
        passages += 1
    if offset != len(ids) or next_unique is not None or passages != metadata["passages"]:
        raise ValueError("Ready vectors do not cover every source passage/window exactly")
    if checkpoint.get("cursor") != last_cursor or unique_checksum.hexdigest() != checkpoint.get("key_checksum"):
        raise ValueError("Ready cursor or unique input-key checksum differs from actual model inputs")
    expected_full = checkpoint.get("window_input_checksum")
    if expected_full is not None and full_checksum.hexdigest() != expected_full:
        raise ValueError("Every-window encoder-input checksum differs from completed checkpoint")
    if unique_count == offset:
        if expected_full is None and full_checksum.hexdigest() != checkpoint.get("key_checksum"):
            raise ValueError("Unique-key checksum differs from the all-unique window stream")
        kind = "recorded_window_stream" if expected_full is not None else "recorded_unique_equals_window_stream"
    else:
        # Schema-2 receipts do not define a recorded all-row association. Even an
        # optional checksum field cannot replace bitwise reconciliation/reuse.
        kind = "schema2_reused_first_rows" if different_duplicates else "schema2_reconciled_exact"
    return {
        "schema_version": 1,
        "kind": kind,
        "windows": offset,
        "passages": passages,
        "unique_keys": unique_count,
        "duplicate_rows": offset - unique_count,
        "different_duplicate_rows": different_duplicates,
        "requires_materialization": kind == "schema2_reused_first_rows",
        "window_input_checksum": full_checksum.hexdigest(),
        "unique_key_checksum": unique_checksum.hexdigest(),
        "cursor": last_cursor,
    }


def _copy_or_link(source: Path, destination: Path, *, hardlink: bool) -> str:
    if hardlink and (not hasattr(os, "getuid") or source.stat().st_uid == os.getuid()):
        try:
            os.link(source, destination, follow_symlinks=False)
            return "hardlink"
        except OSError as exc:
            if exc.errno not in _COPY_ERRORS:
                raise
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        _disk_space(destination.parent, source.stat().st_size)
        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    return "copy"


def _write_json(path: Path, value: Mapping[str, Any], *, replace: bool = False) -> str:
    payload = _canonical(value)
    scratch = path.with_name(".install-json-" + uuid.uuid4().hex)
    try:
        with scratch.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(scratch, path)
        else:
            _publish_no_replace(scratch, path)
    finally:
        scratch.unlink(missing_ok=True)
    return hashlib.sha256(payload).hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _activate(path: Path, metadata: Mapping[str, Any]) -> None:
    """Replace one pointer, restoring its prior bytes if publication/fsync raises."""
    if path.exists() or path.is_symlink():
        _regular(path)
    previous = path.read_bytes() if path.exists() else None
    scratch = path.with_name(".install-active-" + uuid.uuid4().hex)
    backup = path.with_name(".install-previous-" + uuid.uuid4().hex)
    try:
        with scratch.open("xb") as stream:
            stream.write(_canonical(metadata))
            stream.flush()
            os.fsync(stream.fileno())
        if previous is not None:
            with backup.open("xb") as stream:
                stream.write(previous)
                stream.flush()
                os.fsync(stream.fileno())
        try:
            os.replace(scratch, path)
            _fsync_directory(path.parent)
        except BaseException:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                os.replace(backup, path)
            _fsync_directory(path.parent)
            raise
    finally:
        scratch.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)


def _derived_metadata(
    metadata: dict[str, Any], inputs: dict[str, Any], source_receipt_sha: str
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    origin = {
        "kind": "exact-first-row-reuse-v1",
        "source_generation": metadata["generation"],
        "source_receipt_sha256": source_receipt_sha,
        "window_input_checksum": inputs["window_input_checksum"],
    }
    identity = hashlib.sha256(_canonical(origin)).hexdigest()
    generation = "gen-reconciled-" + identity
    rewritten = metadata | {
        "generation": generation,
        "build_identity": identity,
        "encoded_vectors": 0,
        "reused_vectors": metadata["vectors"],
        "resumed_vectors": 0,
        "reuse_source_generation": metadata["generation"],
        "installation_derivation": origin,
    }
    rewritten.pop("seconds", None)
    # Freeze the same model/window/corpus descriptor with a distinct generation
    # identity; counters and timings do not belong to the build descriptor.
    excluded = {
        "generation",
        "encoded_vectors",
        "reused_vectors",
        "resumed_vectors",
        "reuse_source_generation",
        "seconds",
        "requested_revision",
        "warnings",
        "disk_reserve_bytes",
        "estimated_bytes_per_vector",
        "installation_derivation",
    }
    descriptor = {key: value for key, value in rewritten.items() if key not in excluded}
    return generation, rewritten, descriptor


def _reused_rows(reuse: GenerationReuse, first_rows: Any) -> Generator[tuple[int, np.ndarray]]:
    for start in range(0, len(first_rows), MAX_BATCH):
        positions = np.asarray(first_rows[start : start + MAX_BATCH])
        if np.any(positions < 0) or np.any(positions >= len(first_rows)):
            raise ValueError("Verified first-row mapping is out of bounds")
        yield start, np.asarray(reuse.vectors[positions], dtype=np.float32)


def _prepare_generation(
    vectors: Path,
    source: Path,
    reuse: GenerationReuse,
    first_rows: Any,
    metadata: dict[str, Any],
    checkpoint: dict[str, Any],
    inputs: dict[str, Any],
    source_checksums: dict[str, str],
    *,
    hardlink: bool,
) -> tuple[Path, dict[str, Any], dict[str, str], dict[str, str]]:
    materialize = inputs["requires_materialization"]
    if materialize:
        generation, selected, descriptor = _derived_metadata(metadata, inputs, source_checksums[RECEIPT])
    else:
        generation, selected, descriptor = metadata["generation"], metadata, checkpoint["descriptor"]
    destination = vectors / generation
    methods: dict[str, str] = {}
    if destination.exists() or destination.is_symlink():
        _directory(destination)
        if _json(destination / "metadata.json") != selected:
            raise ValueError("Existing target generation metadata differs; no immutable file is overwritten")
        for name in GENERATION_FILES:
            _regular(destination / name)
            if not materialize and _hash(destination / name) != source_checksums[name]:
                raise ValueError("Existing target generation bytes differ; no immutable file is overwritten")
            methods[name] = "existing"
        if materialize:
            with GenerationReuse.open(destination, reuse.space) as existing:
                if (
                    _json(destination / "checkpoint.json").get("window_input_checksum")
                    != inputs["window_input_checksum"]
                ):
                    raise ValueError("Existing derived generation has a different window stream")
                for start, rows in _reused_rows(reuse, first_rows):
                    if existing.vectors[start : start + len(rows)].tobytes() != rows.tobytes():
                        raise ValueError("Existing derived rows differ from verified exact-input first rows")
            for name in ("ids.npy", KEYMAP):
                if _hash(destination / name) != source_checksums[name]:
                    raise ValueError("Existing derived IDs/key map differ from verified source")
    else:
        with tempfile.TemporaryDirectory(prefix=".install-generation-", dir=vectors) as temporary:
            staging = Path(temporary) / generation
            staging.mkdir()
            if materialize:
                _disk_space(staging, metadata["vectors"] * reuse.space.dimensions * 4 + 256)
                array = np.lib.format.open_memmap(
                    staging / "embeddings.npy",
                    mode="w+",
                    dtype=np.float32,
                    shape=(metadata["vectors"], reuse.space.dimensions),
                )
                checksum = hashlib.sha256()
                try:
                    for start, rows in _reused_rows(reuse, first_rows):
                        if start % (MAX_BATCH * 128) == 0:
                            _disk_space(staging, 0)
                        array[start : start + len(rows)] = rows
                        checksum.update(rows.tobytes(order="C"))
                    array.flush()
                finally:
                    _close_mapping(array)
                with (staging / "embeddings.npy").open("rb") as stream:
                    os.fsync(stream.fileno())
                methods["embeddings.npy"] = "materialized-exact-reuse"
                for name in ("ids.npy", KEYMAP):
                    methods[name] = _copy_or_link(source / name, staging / name, hardlink=hardlink)
                ready_checkpoint = checkpoint | {
                    "descriptor": descriptor,
                    "vector_checksum": checksum.hexdigest(),
                    "window_input_checksum": inputs["window_input_checksum"],
                }
                _write_json(staging / "metadata.json", selected)
                _write_json(staging / "checkpoint.json", ready_checkpoint)
                receipt = _json(source / RECEIPT) | {
                    "generation": generation,
                    "vector_checksum": checksum.hexdigest(),
                    "metadata_sha256": _hash(staging / "metadata.json"),
                    "checkpoint_sha256": _hash(staging / "checkpoint.json"),
                    "window_input_checksum": inputs["window_input_checksum"],
                }
                _write_json(staging / RECEIPT, receipt)
                methods.update(dict.fromkeys(("metadata.json", "checkpoint.json", RECEIPT), "derived"))
            else:
                for name, expected in source_checksums.items():
                    methods[name] = _copy_or_link(source / name, staging / name, hardlink=hardlink)
                    if _hash(staging / name) != expected:
                        raise ValueError("Generation bytes changed during link/copy")
            with GenerationReuse.open(staging, reuse.space):
                pass
            _fsync_directory(staging)
            _publish_no_replace(staging, destination)
            _fsync_directory(vectors)
    with GenerationReuse.open(destination, reuse.space):
        pass
    checked_ids = _ids(destination, metadata["vectors"], _json(destination / "checkpoint.json"))
    checked_ids._mmap.close()
    return destination, selected, {name: _hash(destination / name) for name in GENERATION_FILES}, methods


def install_partial_index(
    source_directory: Path, target_directory: Path, *, model_cache: Path | None = None, hardlink: bool = True
) -> dict[str, Any]:
    """Validate and explicitly activate a completed source generation as a subset.

    Source generation files are never modified. Hardlink mode requires the caller
    to retain their immutable-generation contract; no chmod/mmap writes occur.
    Existing target active bytes survive every failure before the atomic commit.
    """
    source_root = _directory(source_directory)
    target_root = _directory(target_directory)
    source_db, target_db = source_root / "library.sqlite3", target_root / "library.sqlite3"
    _regular(source_db)
    _regular(target_db)
    if os.path.samefile(source_db, target_db):
        raise ValueError("Source and target must be independent library databases")
    _source_quiet(source_db)
    source_vectors = _directory(source_root / "vectors")
    _regular(source_vectors / "active.json")
    metadata = _json(source_vectors / "active.json")
    generation = metadata.get("generation")
    if not isinstance(generation, str) or not re.fullmatch(r"gen-[A-Za-z0-9_.-]{1,180}", generation):
        raise ValueError("Only an active completed generation can be installed; staging is forbidden")
    folder = _directory(source_vectors / generation)
    watched = [source_db, source_vectors / "active.json", *(folder / name for name in GENERATION_FILES)]
    source_states = {str(path): _state(path) for path in watched}
    if _canonical(_json(folder / "metadata.json")) != _canonical(metadata):
        raise ValueError("Source active metadata must exactly match the sealed generation metadata")
    checkpoint = _json(folder / "checkpoint.json")
    descriptor = checkpoint.get("descriptor")
    if not isinstance(descriptor, dict) or any(
        _canonical(metadata.get(key)) != _canonical(value) for key, value in descriptor.items()
    ):
        raise ValueError("Completed checkpoint descriptor differs from source metadata")
    space = ModelSpace.from_metadata(metadata, normalized=True)
    source_adapter, target_adapter = _Database(source_db, True), _Database(target_db)
    cache = model_cache or Path(os.getenv("BOOKRAG_MODEL_CACHE", str(source_root / "models"))).expanduser()
    # The first-row map is bounded disk scratch (8 bytes/window), never source
    # text. It permits exact reuse materialization without a second tokenization.
    with (
        GenerationReuse.open(folder, space) as reuse,
        tempfile.TemporaryDirectory(prefix=".install-input-proof-", dir=target_root) as temporary,
    ):
        ids = _ids(folder, metadata["vectors"], checkpoint)
        first_rows = None
        try:
            _disk_space(target_root, metadata["vectors"] * 8 + 256)
            first_rows = np.lib.format.open_memmap(
                Path(temporary) / "first-rows.npy", mode="w+", dtype=np.int64, shape=(metadata["vectors"],)
            )
            with source_adapter.connect() as conn:
                conn.execute("BEGIN")
                proof = capture_coverage(conn, kind="subset")
                if (
                    type(metadata.get("passages")) is not int
                    or proof["passage_fingerprint"] != metadata.get("corpus_fingerprint")
                    or proof["passages"] != metadata.get("passages")
                ):
                    raise ValueError("Source corpus differs from completed vector coverage")
                inputs = _input_proof(conn, reuse, ids, metadata, checkpoint, _load_tokenizer(space, cache), first_rows)
            first_rows.flush()
            source_checksums = {name: _hash(folder / name) for name in GENERATION_FILES}
            _source_quiet(source_db)
            if any(_state(path) != source_states[str(path)] for path in watched):
                raise ValueError("Immutable source changed during verification")
            with target_adapter.connect() as conn:
                if capture_coverage(conn, proof["covered_book_ids"], kind="subset") != proof:
                    raise ValueError("Target actual source differs from the verified ready generation")
            vectors = _directory(target_root / "vectors", create=True)
            active = vectors / "active.json"
            if active.exists() or active.is_symlink():
                _regular(active)
            with _build_lock(vectors):
                destination, selected, target_checksums, methods = _prepare_generation(
                    vectors,
                    folder,
                    reuse,
                    first_rows,
                    metadata,
                    checkpoint,
                    inputs,
                    source_checksums,
                    hardlink=hardlink,
                )
                if any(_state(path) != source_states[str(path)] for path in watched):
                    raise ValueError("Immutable source changed before installation commit")
                _source_quiet(source_db)
                with target_adapter.connect() as conn:
                    ensure_epoch_schema(conn)
                binding = bind_coverage(target_adapter, proof, receipt_sha256=target_checksums[RECEIPT])
                installations = _directory(vectors / "installations", create=True)
                receipt_path = installations / ("install-" + uuid.uuid4().hex + ".json")
                receipt = {
                    "schema_version": INSTALLATION_SCHEMA,
                    "kind": "subset",
                    "generation": destination.name,
                    "source_generation": generation,
                    "source_files": source_checksums,
                    "generation_files": target_checksums,
                    "generation_file_states": {name: list(_state(destination / name)) for name in GENERATION_FILES},
                    "source_input_proof": inputs,
                    "model_space": space.metadata(),
                    "coverage": proof,
                    "coverage_binding": binding,
                    "file_methods": methods,
                }
                # Only the cheap epoch/schema guard and small JSON publication
                # occur under this final writer lock; lexical readers continue.
                with target_adapter.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    validity = validate_coverage(target_adapter, proof, binding)
                    if not validity["valid"]:
                        raise ValueError("Target source changed before activation: " + validity["reason"])
                    _source_quiet(source_db)
                    receipt_sha = _write_json(receipt_path, receipt)
                    _fsync_directory(installations)
                    installed = selected | {
                        "coverage_kind": "subset",
                        "coverage": proof,
                        "coverage_binding": binding,
                        "installation_receipt": {"path": str(receipt_path.relative_to(vectors)), "sha256": receipt_sha},
                    }
                    _activate(active, installed)
                return installed
        finally:
            _close_mapping(first_rows)
            _close_mapping(ids)


def read_installation_receipt(target_directory: Path, metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a persisted local installation identity without scanning vectors.

    Pair this with validate_coverage before/after queries. File identity/mtime
    checks rely on the explicit immutable-generation ownership contract; this is
    not a replacement for a full payload checksum audit after storage corruption.
    """
    root = _directory(target_directory)
    vectors = _directory(root / "vectors")
    locator = metadata.get("installation_receipt")
    if metadata.get("coverage_kind") != "subset" or not isinstance(locator, Mapping):
        raise ValueError("Subset metadata needs an explicit installation receipt")
    relative, expected = locator.get("path"), locator.get("sha256")
    if (
        not isinstance(relative, str)
        or not re.fullmatch(r"installations/install-[a-f0-9]{32}\.json", relative)
        or not isinstance(expected, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected)
    ):
        raise ValueError("Invalid installation receipt locator/digest")
    _directory(vectors / "installations")
    path = vectors / relative
    if _regular(path).st_size > 32 * 1024**2 or _hash(path) != expected:
        raise ValueError("Installation receipt checksum/size differs")
    receipt = _json(path)
    generation = metadata.get("generation")
    if not isinstance(generation, str) or not re.fullmatch(r"gen-[A-Za-z0-9_.-]{1,180}", generation):
        raise ValueError("Invalid installed generation identity")
    if (
        receipt.get("schema_version") != INSTALLATION_SCHEMA
        or receipt.get("kind") != "subset"
        or receipt.get("generation") != generation
        or receipt.get("coverage") != metadata.get("coverage")
        or receipt.get("coverage_binding") != metadata.get("coverage_binding")
        or receipt.get("model_space") != ModelSpace.from_metadata(metadata, normalized=True).metadata()
    ):
        raise ValueError("Installed metadata differs from its verified installation receipt")
    checksums, states = receipt.get("generation_files"), receipt.get("generation_file_states")
    if (
        not isinstance(checksums, dict)
        or set(checksums) != set(GENERATION_FILES)
        or not isinstance(states, dict)
        or set(states) != set(GENERATION_FILES)
    ):
        raise ValueError("Installation receipt has incomplete file identities")
    binding = receipt.get("coverage_binding")
    if not isinstance(binding, Mapping) or not isinstance(receipt.get("coverage"), Mapping):
        raise ValueError("Installation receipt has invalid coverage/binding objects")
    folder = _directory(vectors / generation)
    base = _json(folder / "metadata.json")
    wrappers = {"coverage_kind", "coverage", "coverage_binding", "installation_receipt"}
    if _canonical({key: value for key, value in metadata.items() if key not in wrappers}) != _canonical(base):
        raise ValueError("Active generation fields differ from sealed metadata")
    for name, digest in checksums.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid installed file checksum")
        if list(_state(folder / name)) != states[name]:
            raise ValueError("Immutable installed generation file changed")
    if _hash(folder / RECEIPT) != checksums[RECEIPT] or binding.get("receipt_sha256") != checksums[RECEIPT]:
        raise ValueError("Coverage binding differs from the verified generation receipt")
    return receipt
