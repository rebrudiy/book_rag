"""Exact encoder-input key maps referencing vectors in completed immutable generations.

No source text or second vector copy is stored. This module does not encode,
score queries, activate generations or modify an existing completed source.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import numpy as np
from numpy.typing import NDArray

SCHEMA_VERSION = 2
MAX_BATCH = 500
KEYMAP = "window-keys.sqlite3"
RECEIPT = "vector-reuse.json"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected metadata object: {path.name}")
    return value


@dataclass(frozen=True, slots=True)
class ModelSpace:
    """An immutable encoder identity, independent of corpus IDs and row positions."""

    model: str
    revision: str
    dimensions: int
    max_tokens: int
    profile_json: str
    normalized: bool

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("A nonempty model identity is required")
        if not isinstance(self.revision, str) or not re.fullmatch(r"[0-9a-f]{40,64}", self.revision):
            raise ValueError("Model revision must be an immutable hexadecimal commit/digest")
        if type(self.dimensions) is not int or not 1 <= self.dimensions <= 65536:
            raise ValueError("Dimensions must be an integer in 1..65536")
        if type(self.max_tokens) is not int or self.max_tokens < 1 or type(self.normalized) is not bool:
            raise ValueError("Explicit token capacity and boolean normalization are required")
        profile = json.loads(self.profile_json)
        if not isinstance(profile, dict) or not profile or _canonical(profile) != self.profile_json:
            raise ValueError("Profile must be nonempty, canonical frozen JSON")
        for key, expected in (("model", self.model), ("revision", self.revision)):
            if key in profile and profile[key] != expected:
                raise ValueError(f"Profile {key} differs from model space")

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any], *, normalized: bool) -> Self:
        """Read current generation fields; never guess an absent/legacy profile."""
        required = {"model", "model_revision", "dimensions", "model_max_tokens", "embedding_profile"}
        if not isinstance(metadata, Mapping) or not required.issubset(metadata):
            raise ValueError("Generation lacks explicit model-space fields")
        return cls(
            metadata["model"],
            metadata["model_revision"],
            metadata["dimensions"],
            metadata["model_max_tokens"],
            _canonical(metadata["embedding_profile"]),
            normalized,
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "model_revision": self.revision,
            "dimensions": self.dimensions,
            "model_max_tokens": self.max_tokens,
            "embedding_profile": json.loads(self.profile_json),
            "normalized": self.normalized,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical(self.metadata()).encode("utf-8")).hexdigest()


def window_key(text: str, space: ModelSpace) -> bytes:
    """Hash exact UTF-8 model input, preserving every prefix/space/code point."""
    if not isinstance(text, str):
        raise TypeError("Encoder input must be a string")
    return hashlib.sha256(b"book-rag-window-v1\0" + bytes.fromhex(space.digest) + text.encode("utf-8")).digest()


def _vectors(folder: Path, count: int, space: ModelSpace) -> tuple[Any, str]:
    vectors = np.load(folder / "embeddings.npy", mmap_mode="r", allow_pickle=False)
    if not isinstance(vectors, np.memmap):
        close = getattr(vectors, "close", None)
        if close:
            close()
        raise ValueError("Expected one memory-mapped NPY vector array")
    try:
        if vectors.shape != (count, space.dimensions) or vectors.dtype != np.dtype("float32"):
            raise ValueError("Vector shape/dtype differs from frozen model space")
        if (folder / "embeddings.npy").stat().st_size != vectors.offset + vectors.nbytes:
            raise ValueError("Vector file length differs from declared array")
        digest = hashlib.sha256()
        rows_per_chunk = max(1, (1024 * 1024) // (space.dimensions * 4))
        for start in range(0, count, rows_per_chunk):
            chunk = vectors[start : min(count, start + rows_per_chunk)]
            if not np.isfinite(chunk).all():
                raise ValueError("Nonfinite source vectors")
            if space.normalized and not np.allclose(np.linalg.norm(chunk, axis=1), 1.0, rtol=1e-3, atol=1e-4):
                raise ValueError("Source vectors violate declared L2 normalization")
            digest.update(chunk.tobytes(order="C"))
        return vectors, digest.hexdigest()
    except BaseException:
        vectors._mmap.close()
        raise


def _complete_descriptor(folder: Path, space: ModelSpace, count: int, *, source: bool) -> tuple[dict[str, Any], str]:
    metadata = _json(folder / "metadata.json")
    checkpoint = _json(folder / "checkpoint.json")
    name = metadata.get("generation")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.startswith("staging"):
        raise ValueError("A final generation name is required")
    if source and (folder.name.startswith("staging") or name != folder.name):
        raise ValueError("Staging or mismatched generation cannot be a reuse source")
    if type(count) is not int or count < 1 or type(metadata.get("vectors")) is not int or metadata["vectors"] != count:
        raise ValueError("Generation vector count mismatch")
    if ModelSpace.from_metadata(metadata, normalized=space.normalized) != space:
        raise ValueError("Generation model space mismatch")
    if "normalized" in metadata and metadata["normalized"] != space.normalized:
        raise ValueError("Generation normalization mismatch")
    if (
        type(checkpoint.get("version")) is not int
        or checkpoint["version"] != 1
        or type(checkpoint.get("completed_vectors")) is not int
    ):
        raise ValueError("Incomplete or unsupported generation checkpoint")
    descriptor = checkpoint.get("descriptor")
    if (
        not isinstance(descriptor, dict)
        or type(descriptor.get("vectors")) is not int
        or checkpoint["completed_vectors"] != count
        or descriptor["vectors"] != count
    ):
        raise ValueError("Incomplete generation checkpoint")
    if ModelSpace.from_metadata(checkpoint["descriptor"], normalized=space.normalized) != space:
        raise ValueError("Checkpoint model space mismatch")
    checksum = checkpoint.get("vector_checksum")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("Complete vector checksum is required")
    return metadata, checksum


def _validate_map(connection: sqlite3.Connection, count: int) -> int:
    schema = connection.execute("SELECT sql FROM sqlite_master WHERE name='window_keys'").fetchone()
    if not schema or "WITHOUT ROWID" not in schema[0].upper():
        raise ValueError("Expected WITHOUT ROWID window-key table")
    if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise ValueError("Unsupported key-map schema")
    row_index = connection.execute("SELECT sql FROM sqlite_master WHERE name='window_keys_row'").fetchone()
    if (
        not row_index
        or "UNIQUE" not in row_index[0].upper()
        or [row[2] for row in connection.execute("PRAGMA index_info(window_keys_row)")] != ["row_index"]
    ):
        raise ValueError("Expected unique first-row index")
    entries = 0
    cursor = connection.execute("SELECT key,row_index FROM window_keys ORDER BY key")
    while rows := cursor.fetchmany(MAX_BATCH):
        for key, row in rows:
            if not isinstance(key, bytes) or len(key) != 32 or type(row) is not int or not 0 <= row < count:
                raise ValueError("Invalid key or vector row in reuse map")
            entries += 1
    return entries


def _map_hash(connection: sqlite3.Connection) -> Any:
    """Reconstruct a unique-key stream once at resume, in first-row order."""
    checksum = hashlib.sha256()
    cursor = connection.execute("SELECT key,row_index FROM window_keys ORDER BY row_index")
    while rows := cursor.fetchmany(MAX_BATCH):
        for key, row in rows:
            checksum.update(key + row.to_bytes(8, "big"))
    return checksum


class KeyMapWriter:
    """Durable contiguous key batches in an owned staging generation.

    Seeding an already completed generation is an explicit separate authorization
    through ``seed_completed=True``. Existing final receipts are never overwritten.
    """

    def __init__(
        self, folder: Path, space: ModelSpace, vector_count: int, *, resume: bool = False, seed_completed: bool = False
    ) -> None:
        if type(vector_count) is not int or vector_count < 1:
            raise ValueError("Positive expected vector count required")
        if not folder.is_dir():
            raise FileNotFoundError(folder)
        if not folder.name.startswith("staging-") and not seed_completed:
            raise ValueError("Existing completed-generation seeding requires explicit seed_completed=True")
        if (folder / RECEIPT).exists():
            raise ValueError("Finalized key map is immutable; refusing to overwrite")
        self.folder, self.space, self.vector_count = folder, space, vector_count
        self.connection: sqlite3.Connection | None = None
        path = folder / KEYMAP
        if path.exists() and not resume:
            raise ValueError("Key map already exists; explicit resume required")
        self.connection = sqlite3.connect(path)
        try:
            self.connection.execute("PRAGMA journal_mode=DELETE")
            self.connection.execute("PRAGMA synchronous=FULL")
            if resume:
                state = self.connection.execute(
                    "SELECT space,vector_count,rows_written,finalized,key_checksum FROM build_state"
                ).fetchone()
                if (
                    not state
                    or state[0] != _canonical(space.metadata())
                    or state[1] != vector_count
                    or type(state[2]) is not int
                    or not 0 <= state[2] <= vector_count
                    or state[3] not in (0, 1)
                ):
                    raise ValueError("Key-map resume identity/state mismatch")
                _validate_map(self.connection, vector_count)
                if self.connection.execute(
                    "SELECT 1 FROM window_keys WHERE row_index>=? LIMIT 1", (state[2],)
                ).fetchone():
                    raise ValueError("Key-map rows exceed its durable prefix")
                self._checksum = _map_hash(self.connection)
                if state[4] != self._checksum.hexdigest():
                    raise ValueError("Durable key-map prefix checksum mismatch")
            else:
                self.connection.executescript(
                    "CREATE TABLE window_keys(key BLOB PRIMARY KEY CHECK(length(key)=32),"
                    "row_index INTEGER NOT NULL CHECK(row_index>=0)) WITHOUT ROWID;"
                    "CREATE UNIQUE INDEX window_keys_row ON window_keys(row_index);"
                    "CREATE TABLE build_state(space TEXT NOT NULL,vector_count INTEGER NOT NULL,"
                    "rows_written INTEGER NOT NULL,finalized INTEGER NOT NULL,key_checksum TEXT NOT NULL);"
                    f"PRAGMA user_version={SCHEMA_VERSION};"
                )
                self._checksum = hashlib.sha256()
                self.connection.execute(
                    "INSERT INTO build_state VALUES (?,?,0,0,?)",
                    (_canonical(space.metadata()), vector_count, self._checksum.hexdigest()),
                )
                self.connection.commit()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("Key-map writer is closed")
        return self.connection

    @property
    def rows_written(self) -> int:
        return self._connection().execute("SELECT rows_written FROM build_state").fetchone()[0]

    @property
    def finalized(self) -> bool:
        """Whether only receipt recovery remains after a durable seal."""
        return bool(self._connection().execute("SELECT finalized FROM build_state").fetchone()[0])

    @property
    def checksum(self) -> str:
        """Digest of unique keys and their first rows, incrementally maintained."""
        return self._checksum.hexdigest()

    def _require_unsealed(self) -> None:
        if self._connection().execute("SELECT finalized FROM build_state").fetchone()[0]:
            raise ValueError("Sealed key map only permits completing its final receipt")

    def add_batch(self, texts: Sequence[str], *, start_row: int) -> None:
        self._require_unsealed()
        if len(texts) > MAX_BATCH or type(start_row) is not int or start_row != self.rows_written:
            raise ValueError("Batch must contain <=500 inputs at the exact next row")
        if start_row + len(texts) > self.vector_count:
            raise ValueError("Key batch exceeds declared vector count")
        # Deduplicate only this bounded batch, preserving the stable first row.
        first_rows: dict[bytes, int] = {}
        for index, text in enumerate(texts):
            first_rows.setdefault(window_key(text, self.space), start_row + index)
        rows = list(first_rows.items())
        connection = self._connection()
        existing = (
            {
                row[0]
                for row in connection.execute(
                    "SELECT key FROM window_keys WHERE key IN (" + ",".join("?" for _ in rows) + ")",
                    [key for key, _ in rows],
                )
            }
            if rows
            else set()
        )
        checksum = self._checksum.copy()
        for key, row in rows:
            if key not in existing:
                checksum.update(key + row.to_bytes(8, "big"))
        with connection:
            connection.executemany("INSERT INTO window_keys VALUES (?,?) ON CONFLICT(key) DO NOTHING", rows)
            connection.execute(
                "UPDATE build_state SET rows_written=?,key_checksum=?", (start_row + len(texts), checksum.hexdigest())
            )
        self._checksum = checksum

    def truncate_to(self, completed_rows: int) -> None:
        """Reconcile a durable key prefix to the caller's vector checkpoint."""
        self._require_unsealed()
        if type(completed_rows) is not int or not 0 <= completed_rows <= self.rows_written:
            raise ValueError("Can only truncate an existing key prefix")
        if completed_rows == self.rows_written:
            return
        connection = self._connection()
        with connection:
            connection.execute("DELETE FROM window_keys WHERE row_index>=?", (completed_rows,))
            checksum = _map_hash(connection)
            connection.execute(
                "UPDATE build_state SET rows_written=?,key_checksum=?", (completed_rows, checksum.hexdigest())
            )
        self._checksum = checksum

    def finalize(self) -> dict[str, Any]:
        """Seal count/checksum metadata after the generation's full checkpoint."""
        if self.rows_written != self.vector_count:
            raise ValueError("Cannot finalize a partial key stream")
        metadata, expected = _complete_descriptor(self.folder, self.space, self.vector_count, source=False)
        vectors, checksum = _vectors(self.folder, self.vector_count, self.space)
        vectors._mmap.close()
        if checksum != expected:
            raise ValueError("Generation vector checksum differs from complete checkpoint")
        connection = self._connection()
        entries = _validate_map(connection, self.vector_count)
        with connection:
            connection.execute("UPDATE build_state SET finalized=1")
        self.close()
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "complete": True,
            "generation": metadata["generation"],
            "space": self.space.metadata(),
            "space_sha256": self.space.digest,
            "vectors": self.vector_count,
            "unique_keys": entries,
            "rows_written": self.vector_count,
            "vector_checksum": checksum,
            "key_checksum": self.checksum,
            "keymap_sha256": _file_hash(self.folder / KEYMAP),
            "metadata_sha256": _file_hash(self.folder / "metadata.json"),
            "checkpoint_sha256": _file_hash(self.folder / "checkpoint.json"),
        }
        temporary = self.folder / (RECEIPT + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.folder / RECEIPT)
        return receipt

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


class GenerationReuse:
    """Read-only exact-vector lookup from one validated completed generation."""

    def __init__(self, folder: Path, expected_space: ModelSpace) -> None:
        self.folder, self.space = folder, expected_space
        self.connection: sqlite3.Connection | None = None
        self.vectors: Any = None
        receipt = _json(folder / RECEIPT)
        if (
            type(receipt.get("schema_version")) is not int
            or receipt["schema_version"] != SCHEMA_VERSION
            or receipt.get("complete") is not True
        ):
            raise ValueError("Reuse source is not finalized")
        if receipt.get("space") != expected_space.metadata() or receipt.get("space_sha256") != expected_space.digest:
            raise ValueError("Reuse source model/profile/normalization mismatch")
        if type(receipt.get("rows_written")) is not int or type(receipt.get("unique_keys")) is not int:
            raise ValueError("Reuse receipt counts must be integers")
        count = receipt.get("vectors")
        if type(count) is not int or count < 1:
            raise ValueError("Invalid reuse receipt vector count")
        metadata, expected = _complete_descriptor(folder, expected_space, count, source=True)
        if receipt.get("generation") != metadata["generation"] or receipt.get("rows_written") != count:
            raise ValueError("Reuse receipt count/generation mismatch")
        for filename, field in (
            (KEYMAP, "keymap_sha256"),
            ("metadata.json", "metadata_sha256"),
            ("checkpoint.json", "checkpoint_sha256"),
        ):
            if _file_hash(folder / filename) != receipt.get(field):
                raise ValueError(f"Reuse {filename} checksum mismatch")
        try:
            self.vectors, checksum = _vectors(folder, count, expected_space)
            if checksum != expected or checksum != receipt.get("vector_checksum"):
                raise ValueError("Reuse vector checksum mismatch")
            self.connection = sqlite3.connect((folder / KEYMAP).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
            entries = _validate_map(self.connection, count)
            state = self.connection.execute(
                "SELECT space,vector_count,rows_written,finalized,key_checksum FROM build_state"
            ).fetchone()
            key_checksum = _map_hash(self.connection).hexdigest()
            if (
                entries != receipt.get("unique_keys")
                or not state
                or state != (_canonical(expected_space.metadata()), count, count, 1, key_checksum)
                or receipt.get("key_checksum") != key_checksum
            ):
                raise ValueError("Reuse key-map finalized state/count mismatch")
            self._file_states = self._states()
        except BaseException:
            self.close()
            raise

    @classmethod
    def open(cls, folder: Path, expected_space: ModelSpace) -> Self:
        return cls(folder, expected_space)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _states(self) -> tuple[tuple[int, int, int], ...]:
        return tuple(
            (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            for stat in ((self.folder / name).stat() for name in (KEYMAP, "embeddings.npy"))
        )

    def lookup(self, texts: Sequence[str]) -> list[NDArray[np.float32] | None]:
        """Return independent float32 copies or None, using at most 500 keys."""
        if self.connection is None or self.vectors is None:
            raise RuntimeError("Reuse source is closed")
        if len(texts) > MAX_BATCH:
            raise ValueError("Lookup batches must contain <=500 inputs")
        if self._states() != self._file_states:
            raise ValueError("Immutable reuse files changed after opening")
        keys = [window_key(text, self.space) for text in texts]
        if not keys:
            return []
        mapping = dict(
            self.connection.execute(
                "SELECT key,row_index FROM window_keys WHERE key IN (" + ",".join("?" for _ in keys) + ")", keys
            )
        )
        result = []
        for key in keys:
            row = mapping.get(key)
            if row is None:
                result.append(None)
                continue
            if type(row) is not int or not 0 <= row < self.vectors.shape[0]:
                raise ValueError("Reuse row outside source vector array")
            vector = np.asarray(self.vectors[row], dtype="float32").copy()
            if vector.shape != (self.space.dimensions,) or not np.isfinite(vector).all():
                raise ValueError("Invalid reused vector")
            result.append(vector)
        return result

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.vectors is not None:
            self.vectors._mmap.close()
            self.vectors = None
