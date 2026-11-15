"""Optional local multilingual embeddings, cosine retrieval and reciprocal-rank fusion.

The lexical baseline never needs a model. Vector generation and active-index
replacement are atomic; fingerprints prevent searching stale vector snapshots.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from book_rag.embedding_profiles import (
    MINILM_MODEL,
    MINILM_REVISION,
    EmbeddingProfile,
    profile_for_model,
    profile_from_metadata,
)
from book_rag.vector_reuse import KEYMAP, MAX_BATCH, RECEIPT, SCHEMA_VERSION, GenerationReuse, KeyMapWriter, ModelSpace

if TYPE_CHECKING:
    from book_rag.library import Library

DEFAULT_MODEL = MINILM_MODEL
DEFAULT_REVISION = MINILM_REVISION


def corpus_fingerprint(library: Library) -> str:
    h = hashlib.sha256()
    with library.connect() as conn:
        for row in conn.execute(
            "SELECT p.id,p.sha256 FROM passages p JOIN books b ON b.id=p.book_id WHERE b.status='indexed' ORDER BY p.id"
        ):
            h.update(f"{row['id']}:{row['sha256']}\n".encode())
    return h.hexdigest()


@lru_cache(maxsize=2)
def _model(name: str, cache: str, revision: str | None = None) -> Any:
    sentence_transformers = importlib.import_module("sentence_transformers")
    return sentence_transformers.SentenceTransformer(
        name, cache_folder=cache, revision=revision or _requested_revision(name), trust_remote_code=False
    )


def token_windows(text: str, offsets: list[tuple[int, int]], budget: int, overlap: int = 20) -> Iterator[str]:
    """Emit exact text windows whose tokenizer spans fit a bounded token budget."""
    if budget <= overlap or overlap < 0:
        raise ValueError("Token budget must exceed nonnegative overlap")
    for start in range(0, len(offsets), budget - overlap):
        end = min(start + budget, len(offsets))
        if end > start:
            yield text[offsets[start][0] : offsets[end - 1][1]]
        if end == len(offsets):
            break


def _windows(model: Any, row: Any) -> list[str]:
    header = f"{row['title']} — {row['author']} / {row['section_title']}"
    header_ids = model.tokenizer.encode(header, add_special_tokens=False)[:24]
    prefix = model.tokenizer.decode(header_ids, skip_special_tokens=True)
    budget = max(32, model.max_seq_length - len(header_ids) - 6)
    offsets = model.tokenizer(row["text"], add_special_tokens=False, return_offsets_mapping=True, truncation=False)[
        "offset_mapping"
    ]
    return [prefix + "\n" + window for window in token_windows(row["text"], offsets, budget)]


def _profile_windows(model: Any, row: Any, profile: EmbeddingProfile | None = None) -> list[str]:
    if profile is None or (not profile.document_prefix and profile.max_tokens is None):
        return _windows(model, row)
    header = f"{row['title']} — {row['author']} / {row['section_title']}"
    header_ids = model.tokenizer.encode(header, add_special_tokens=False)[:24]
    header_text = model.tokenizer.decode(header_ids, skip_special_tokens=True)
    prefix = profile.document_prefix + header_text + "\n"
    fixed_tokens = len(model.tokenizer.encode(prefix, add_special_tokens=False))
    maximum = min(model.max_seq_length, profile.max_tokens or model.max_seq_length)
    budget = maximum - fixed_tokens - 6
    if budget <= 20:
        raise ValueError("Embedding profile leaves no usable source-window budget")
    offsets = model.tokenizer(row["text"], add_special_tokens=False, return_offsets_mapping=True, truncation=False)[
        "offset_mapping"
    ]
    return [prefix + window for window in token_windows(row["text"], offsets, budget)]


_SQL = (
    "SELECT p.id,p.text,b.title,b.author,s.title section_title FROM passages p "
    "JOIN books b ON b.id=p.book_id JOIN sections s ON s.id=p.section_id "
    "WHERE b.status='indexed'"
)
_STRATEGY = {
    "strategy": "token-windows-v2",
    "overlap_tokens": 20,
    "context_header_tokens": 24,
    "context": "bounded title/author/section header + overlapping tokenizer-offset text windows",
}


def _model_cache(library: Library) -> Path:
    configured = os.getenv("BOOKRAG_MODEL_CACHE")
    return (Path(configured).expanduser() if configured else library.data_dir / "models").resolve()


def _requested_revision(name: str) -> str | None:
    return profile_for_model(name).revision or os.getenv("BOOKRAG_EMBEDDING_REVISION")


def _resolved_revision(model: Any, name: str, cache: Path, requested: str | None) -> str:
    if requested and re.fullmatch(r"[0-9a-f]{40,64}", requested):
        return requested
    first_module = getattr(model, "_first_module", lambda: None)()
    config = getattr(getattr(first_module, "auto_model", None), "config", None)
    resolved = getattr(config, "_commit_hash", None)
    if isinstance(resolved, str) and re.fullmatch(r"[0-9a-f]{40,64}", resolved):
        return resolved
    refs = cache / ("models--" + name.replace("/", "--")) / "refs"
    reference = refs / (requested or "main")
    if reference.is_file() and reference.resolve().is_relative_to(refs.resolve()):
        resolved = reference.read_text().strip()
        if re.fullmatch(r"[0-9a-f]{40,64}", resolved):
            return resolved
    raise ValueError("Cannot resolve model revision; set BOOKRAG_EMBEDDING_REVISION to an immutable commit")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def _exclusive_build(directory: Path) -> Generator[None]:
    # Kernel locks are released even after a killed process; no stale PID lock.
    with (directory / ".build.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            stream.write(b"\0")
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("An embedding build is already running for this library") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("An embedding build is already running for this library") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _read_checkpoint(folder: Path) -> dict[str, Any]:
    try:
        value = json.loads((folder / "checkpoint.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _count_windows(
    conn: Any, model: Any, completed: int, profile: EmbeddingProfile | None = None
) -> tuple[int, int, dict[str, int] | None, dict[str, int] | None]:
    count, passages, cursor, final_cursor = 0, 0, None, None
    for row in conn.execute(_SQL + " ORDER BY p.id"):
        windows = len(_profile_windows(model, row, profile))
        if count < completed <= count + windows:
            cursor = {"passage_id": row["id"], "window_index": completed - count - 1}
        if windows:
            final_cursor = {"passage_id": row["id"], "window_index": windows - 1}
        count += windows
        passages += 1
    return count, passages, cursor, final_cursor


def _prefix_hash(array: Any, completed: int) -> Any:
    checksum = hashlib.sha256()
    for start in range(0, completed, 1024):
        chunk = array[start : min(start + 1024, completed)]
        if not np.isfinite(chunk).all():
            raise ValueError("Nonfinite values in completed embedding checkpoint")
        checksum.update(chunk.tobytes(order="C"))
    return checksum


def _resume_arrays(
    folder: Path, checkpoint: dict[str, Any], descriptor: dict[str, Any], cursor: dict[str, int] | None
) -> tuple[Any, Any, Any, Any]:
    completed = checkpoint.get("completed_vectors")
    count, dim = descriptor["vectors"], descriptor["dimensions"]
    if (
        checkpoint.get("version") != 1
        or checkpoint.get("descriptor") != descriptor
        or type(completed) is not int
        or not 0 <= completed <= count
        or checkpoint.get("cursor") != cursor
    ):
        raise ValueError("Embedding checkpoint identity/count/cursor mismatch")
    arrays: list[Any] = []
    try:
        vectors = np.lib.format.open_memmap(folder / "embeddings.npy", mode="r+")
        arrays.append(vectors)
        ids = np.lib.format.open_memmap(folder / "ids.npy", mode="r+")
        arrays.append(ids)
        if vectors.shape != (count, dim) or vectors.dtype != np.dtype("float32"):
            raise ValueError("Embedding checkpoint shape/dtype mismatch")
        if ids.shape != (count,) or ids.dtype != np.dtype("int64"):
            raise ValueError("Passage ID checkpoint shape/dtype mismatch")
        vector_hash, id_hash = _prefix_hash(vectors, completed), _prefix_hash(ids, completed)
        if (
            checkpoint.get("vector_checksum") != vector_hash.hexdigest()
            or checkpoint.get("id_checksum") != id_hash.hexdigest()
        ):
            raise ValueError("Embedding checkpoint completed-prefix checksum mismatch")
        return vectors, ids, vector_hash, id_hash
    except BaseException:
        for array in arrays:
            array._mmap.close()
        raise


def _new_arrays(folder: Path, descriptor: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    # Only staging data is discarded. Active generations are immutable.
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir()
    count, dim = descriptor["vectors"], descriptor["dimensions"]
    vectors = np.lib.format.open_memmap(folder / "embeddings.npy", mode="w+", dtype="float32", shape=(count, dim))
    ids = np.lib.format.open_memmap(folder / "ids.npy", mode="w+", dtype="int64", shape=(count,))
    return vectors, ids, hashlib.sha256(), hashlib.sha256()


DISK_RESERVE_BYTES = 4 * 1024**3
KEYMAP_BYTES_PER_VECTOR = 256


def _require_disk_space(directory: Path, remaining: int, dimensions: int) -> None:
    # Conservative per-window SQLite allowance, two NPY headers and fixed reserve.
    required = DISK_RESERVE_BYTES + remaining * (4 * dimensions + 8 + KEYMAP_BYTES_PER_VECTOR) + 8192
    available = shutil.disk_usage(directory).free
    if available < required:
        raise OSError(
            f"Insufficient free disk for embedding checkpoint: need {required} bytes including "
            f"4 GiB reserve, available {available}; completed work and active index retained"
        )


def _reuse_source(directory: Path, space: ModelSpace, warnings: list[str]) -> GenerationReuse | None:
    active = directory / "active.json"
    if not active.exists():
        return None
    try:
        metadata = json.loads(active.read_text())
        if not isinstance(metadata, dict):
            raise ValueError("Active metadata must be an object")
        generation = metadata.get("generation")
        if not isinstance(generation, str) or not re.fullmatch(r"gen-[A-Za-z0-9_.-]+", generation):
            raise ValueError("Invalid active generation name")
        if ModelSpace.from_metadata(metadata, normalized=True) != space:
            return None
        folder = directory / generation
        if not (folder / RECEIPT).exists():
            warnings.append("Active generation has no finalized exact-input key map; encoding fresh windows")
            return None
        return GenerationReuse.open(folder, space)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        warnings.append(f"Exact-vector reuse unavailable; encoding fresh windows: {exc}")
        return None


def _ready_metadata(folder: Path, descriptor: dict[str, Any], cursor: dict[str, int] | None) -> dict[str, Any]:
    saved = _read_checkpoint(folder)
    if saved.get("completed_vectors") != descriptor["vectors"]:
        raise ValueError("Ready generation is incomplete")
    arrays = _resume_arrays(folder, saved, descriptor, cursor)
    for array in arrays[:2]:
        array._mmap.close()
    metadata = json.loads((folder / "metadata.json").read_text())
    if not isinstance(metadata, dict):
        raise ValueError("Ready metadata must be an object")
    if metadata.get("generation") != folder.name or any(metadata.get(k) != v for k, v in descriptor.items()):
        raise ValueError("Ready generation metadata differs from requested build")
    # A new builder never activates a renamed but unsealed key map. Legacy query
    # indexes remain supported independently of this stricter build recovery.
    with GenerationReuse.open(folder, ModelSpace.from_metadata(descriptor, normalized=True)):
        pass
    return metadata


def _promote_sealed_staging(
    staging: Path, descriptor: dict[str, Any], cursor: dict[str, int] | None, warnings: list[str]
) -> None:
    """Recover a crash after receipt creation but before the immutable rename."""
    if not (staging / RECEIPT).exists():
        return
    try:
        metadata = json.loads((staging / "metadata.json").read_text())
        if not isinstance(metadata, dict):
            raise ValueError("Sealed staging metadata must be an object")
        generation = metadata.get("generation")
        if not isinstance(generation, str) or not re.fullmatch(r"gen-[A-Za-z0-9_.-]+", generation):
            raise ValueError("Invalid sealed generation name")
        if any(metadata.get(k) != v for k, v in descriptor.items()):
            raise ValueError("Sealed generation descriptor mismatch")
        arrays = _resume_arrays(staging, _read_checkpoint(staging), descriptor, cursor)
        for array in arrays[:2]:
            array._mmap.close()
        staging.rename(staging.parent / generation)
    except (ValueError, OSError, KeyError, TypeError):
        # Rejected staging data can be rebuilt; never repair an immutable source.
        warnings.append("Sealed staging recovery failed; validating or rebuilding before activation")


def build_index(library: Library, model_name: str | None = None, batch_size: int = 32) -> dict[str, Any]:
    """Build durable exact-key batches, reusing only a validated immutable source."""
    if not 1 <= batch_size <= 512:
        raise ValueError("batch_size must be between 1 and 512")
    started = time.perf_counter()
    name = model_name or os.getenv("BOOKRAG_EMBEDDING_MODEL", DEFAULT_MODEL)
    profile = profile_for_model(name, os.getenv("BOOKRAG_EMBEDDING_PROFILE"))
    cache = _model_cache(library)
    requested_revision = profile.revision or _requested_revision(name)
    model = _model(name, str(cache), requested_revision)
    revision = _resolved_revision(model, name, cache, requested_revision)
    if type(model.get_embedding_dimension()) is not int or model.get_embedding_dimension() <= 0:
        raise ValueError("Model embedding dimension must be a positive integer")
    directory = library.data_dir / "vectors"
    directory.mkdir(exist_ok=True)
    warnings: list[str] = []
    with _exclusive_build(directory):
        fingerprint = corpus_fingerprint(library)
        identity_fields: dict[str, Any] = {
            "model": name,
            "model_revision": revision,
            "corpus_fingerprint": fingerprint,
            **_STRATEGY,
            "embedding_profile": profile.metadata(revision),
            "strategy": profile.strategy,
            "vector_reuse_schema": SCHEMA_VERSION,
        }
        identity_fields.update(model_max_tokens=model.max_seq_length, dimensions=model.get_embedding_dimension())
        space = ModelSpace.from_metadata(identity_fields, normalized=True)
        identity = hashlib.sha256(json.dumps(identity_fields, sort_keys=True).encode()).hexdigest()
        staging = directory / ("staging-" + identity)
        checkpoint = _read_checkpoint(staging)
        completed = checkpoint.get("completed_vectors", 0)
        if type(completed) is not int or completed < 0:
            completed = 0
        vectors: Any = None
        ids: Any = None
        keys, source = None, None
        try:
            with library.connect() as conn:
                conn.execute("BEGIN")
                count, passages, cursor, final_cursor = _count_windows(conn, model, completed, profile)
                if not count:
                    raise ValueError("Import books before building an embedding index")
                descriptor: dict[str, Any] = {
                    **identity_fields,
                    "vectors": count,
                    "passages": passages,
                    "build_identity": identity,
                }
                if corpus_fingerprint(library) != fingerprint:
                    raise RuntimeError("Corpus changed during embedding planning; retry after ingestion finishes")

                _promote_sealed_staging(staging, descriptor, final_cursor, warnings)
                for ready in sorted(directory.glob("gen-" + identity + "-*")):
                    try:
                        metadata = _ready_metadata(ready, descriptor, final_cursor)
                    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
                        warnings.append(f"Ready generation rejected before activation: {exc}")
                        continue
                    if corpus_fingerprint(library) != fingerprint:
                        raise RuntimeError("Corpus changed during index reuse; retry after ingestion finishes")
                    _atomic_json(directory / "active.json", metadata)
                    return metadata | {
                        "reused_active": True,
                        "resumed_vectors": count,
                        "encoded_vectors": 0,
                        "reused_vectors": 0,
                        "warnings": warnings,
                    }

                resumed = 0
                try:
                    vectors, ids, vector_hash, id_hash = _resume_arrays(staging, checkpoint, descriptor, cursor)
                    if not (staging / KEYMAP).exists():
                        raise ValueError("Checkpoint has no durable exact-input key map")
                    keys = KeyMapWriter(staging, space, count, resume=True)
                    if keys.rows_written < completed or (keys.finalized and completed != count):
                        raise ValueError("Key-map prefix is behind its vector checkpoint")
                    if not keys.finalized:
                        keys.truncate_to(completed)
                    if keys.checksum != checkpoint.get("key_checksum"):
                        raise ValueError("Key-map prefix differs from vector checkpoint checksum")
                    resumed = completed
                except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
                    if keys is not None:
                        keys.close()
                        keys = None
                    for array in (vectors, ids):
                        if array is not None:
                            array._mmap.close()
                    vectors, ids = None, None
                    # Space errors are outside checkpoint rejection: do not delete
                    # otherwise recoverable data when a rebuild cannot fit.
                    _require_disk_space(directory, count, descriptor["dimensions"])
                    vectors, ids, vector_hash, id_hash = _new_arrays(staging, descriptor)
                    keys = KeyMapWriter(staging, space, count)
                    completed, cursor = 0, None
                _require_disk_space(directory, count - completed, descriptor["dimensions"])
                offset = completed
                encoded, reused = 0, 0
                source = _reuse_source(directory, space, warnings)
                source_generation = source.folder.name if source is not None else None

                def save_checkpoint(last_cursor: dict[str, int] | None) -> None:
                    if keys is None or keys.rows_written != offset:
                        raise RuntimeError("Durable key prefix must precede vector checkpoint")
                    assert vectors is not None and ids is not None
                    vectors.flush()
                    ids.flush()
                    for filename in ("embeddings.npy", "ids.npy"):
                        with (staging / filename).open("rb") as stream:
                            os.fsync(stream.fileno())
                    _atomic_json(
                        staging / "checkpoint.json",
                        {
                            "version": 1,
                            "descriptor": descriptor,
                            "completed_vectors": offset,
                            "cursor": last_cursor,
                            "vector_checksum": vector_hash.hexdigest(),
                            "id_checksum": id_hash.hexdigest(),
                            "key_checksum": keys.checksum,
                        },
                    )

                # Preserve sealed metadata/checkpoint bytes until receipt recovery.
                if not keys.finalized:
                    save_checkpoint(cursor)
                pending: list[str] = []
                pending_ids: list[int] = []
                last_cursor = cursor

                def flush() -> None:
                    nonlocal offset, encoded, reused, source
                    assert vectors is not None and ids is not None
                    _require_disk_space(directory, count - offset, descriptor["dimensions"])
                    hits: list[Any] = [None] * len(pending)
                    if source is not None:
                        try:
                            hits = []
                            for start in range(0, len(pending), MAX_BATCH):
                                hits.extend(source.lookup(pending[start : start + MAX_BATCH]))
                        except (ValueError, OSError, sqlite3.Error) as exc:
                            warnings.append(f"Exact-vector source rejected during lookup; encoding fresh: {exc}")
                            source.close()
                            source = None
                            hits = [None] * len(pending)
                    missing = [index for index, hit in enumerate(hits) if hit is None]
                    batch = np.empty((len(pending), descriptor["dimensions"]), dtype="float32")
                    for index, hit in enumerate(hits):
                        if hit is not None:
                            batch[index] = hit
                    if missing:
                        fresh = np.asarray(
                            model.encode(
                                [pending[index] for index in missing],
                                batch_size=batch_size,
                                normalize_embeddings=True,
                                convert_to_numpy=True,
                                show_progress_bar=False,
                            ),
                            dtype="float32",
                        )
                        if fresh.shape != (len(missing), descriptor["dimensions"]) or not np.isfinite(fresh).all():
                            raise ValueError("Model returned invalid embedding dimensions or nonfinite values")
                        batch[missing] = fresh
                    batch_ids = np.asarray(pending_ids, dtype="int64")
                    vectors[offset : offset + len(pending)] = batch
                    ids[offset : offset + len(pending)] = batch_ids
                    # Each key transaction is durable before the atomic vector
                    # checkpoint; an interrupted leading key suffix is truncated.
                    assert keys is not None
                    for start in range(0, len(pending), MAX_BATCH):
                        keys.add_batch(pending[start : start + MAX_BATCH], start_row=offset + start)
                    vector_hash.update(batch.tobytes(order="C"))
                    id_hash.update(batch_ids.tobytes(order="C"))
                    offset += len(pending)
                    save_checkpoint(last_cursor)
                    encoded += len(missing)
                    reused += len(pending) - len(missing)
                    pending.clear()
                    pending_ids.clear()

                sql = _SQL + (" AND p.id>=?" if cursor else "") + " ORDER BY p.id"
                parameters = (cursor["passage_id"],) if cursor else ()
                for row in conn.execute(sql, parameters):
                    skip = cursor["window_index"] + 1 if cursor and row["id"] == cursor["passage_id"] else 0
                    for index, window in enumerate(_profile_windows(model, row, profile)):
                        if index < skip:
                            continue
                        pending.append(window)
                        pending_ids.append(row["id"])
                        last_cursor = {"passage_id": row["id"], "window_index": index}
                        if len(pending) >= batch_size:
                            flush()
                if pending:
                    flush()
                if offset != count:
                    raise RuntimeError("Embedding window count changed; incomplete generation retained")
            if corpus_fingerprint(library) != fingerprint:
                raise RuntimeError(
                    "Corpus changed during embedding; checkpoint retained, retry after ingestion finishes"
                )
            _require_disk_space(directory, 0, descriptor["dimensions"])
            generation = "gen-" + identity + "-" + uuid.uuid4().hex[:12]
            metadata = {
                **descriptor,
                "generation": generation,
                "seconds": round(time.perf_counter() - started, 3),
                "resumed_vectors": resumed,
                "encoded_vectors": encoded,
                "reused_vectors": reused,
                "reuse_source_generation": source_generation,
                "disk_reserve_bytes": DISK_RESERVE_BYTES,
                "estimated_bytes_per_vector": 4 * descriptor["dimensions"] + 8 + KEYMAP_BYTES_PER_VECTOR,
                "requested_revision": requested_revision,
                "warnings": warnings,
            }
            _atomic_json(staging / "metadata.json", metadata)
            keys.finalize()
            keys = None
            vectors._mmap.close()
            ids._mmap.close()
            vectors, ids = None, None
            staging.rename(directory / generation)
            _atomic_json(directory / "active.json", metadata)
            return metadata
        finally:
            if keys is not None:
                keys.close()
            if source is not None:
                source.close()
            for array in (vectors, ids):
                if array is not None:
                    array._mmap.close()


# A retrieval pool is independent of the number of results displayed. These
# bounds affect query ranking only; stored vectors and their windows are unchanged.
SEMANTIC_WINDOW_CANDIDATES = 2000
SEMANTIC_PASSAGE_CANDIDATES = 100
HYBRID_LEXICAL_CANDIDATES = 50
RETRIEVAL_STRATEGY = "fixed-passage-pool-v1"


def _candidate_passages(scores: Any, ids: Any, threshold: float) -> tuple[list[int], dict[int, float], int]:
    k = min(len(scores), SEMANTIC_WINDOW_CANDIDATES)
    positions = np.argpartition(-scores, k - 1)[:k] if k else np.array([], dtype=int)
    # Tie ordering uses the immutable window position, not requested output size.
    positions = positions[np.lexsort((positions, -scores[positions]))]
    ordered: list[int] = []
    best: dict[int, float] = {}
    for position in positions:
        score = float(scores[position])
        if not np.isfinite(score) or score < threshold:
            continue
        passage_id = int(ids[position])
        if passage_id in best:
            continue
        ordered.append(passage_id)
        best[passage_id] = score
        if len(ordered) >= SEMANTIC_PASSAGE_CANDIDATES:
            break
    return ordered, best, k


def _score_vectors(
    vectors: Any,
    ids: Any,
    query_vector: np.ndarray,
    eligible_ids: np.ndarray | None = None,
    *,
    ordered_ids: bool = False,
    block_rows: int = 8192,
) -> np.ndarray:
    """Score only an explicit scope with bounded matrix reads.

    Installed receipts prove window IDs follow ascending source passage order.
    Only that path may use binary-search bounds. Legacy arrays can be unordered.
    Unfiltered retrieval retains its existing single matrix multiplication.
    """
    if eligible_ids is None:
        scores = np.asarray(vectors @ query_vector)
        if not np.isfinite(scores).all():
            raise ValueError("Nonfinite semantic scores")
        return scores
    if block_rows < 1:
        raise ValueError("Scoring blocks must contain at least one row")
    scores = np.full(len(ids), -np.inf, dtype=np.result_type(vectors.dtype, query_vector.dtype))
    if not len(eligible_ids):
        return scores
    low, high = 0, len(ids)
    if ordered_ids:
        low = int(np.searchsorted(ids, eligible_ids.min(), side="left"))
        high = int(np.searchsorted(ids, eligible_ids.max(), side="right"))
    positions = np.flatnonzero(np.isin(ids[low:high], eligible_ids)) + low
    for start in range(0, len(positions), block_rows):
        selected = positions[start : start + block_rows]
        values = np.asarray(vectors[selected] @ query_vector)
        if not np.isfinite(values).all():
            raise ValueError("Nonfinite scoped semantic scores")
        scores[selected] = values
    return scores


def _scoped_passage_query(book_id: str | None, language: str | None) -> tuple[str, tuple[str, ...]]:
    """Filter the small books table first, then seek its passage index.

    CROSS JOIN fixes this eligibility-only join order: language scopes must not
    scan every passage before checking book metadata. It does not change ranks.
    """
    predicates = ["b.status='indexed'"]
    parameters: list[str] = []
    if book_id is not None:
        predicates.append("b.id=?")
        parameters.append(book_id)
    if language is not None:
        predicates.append("b.language=?")
        parameters.append(language)
    return (
        "SELECT p.id FROM books b CROSS JOIN passages p ON p.book_id=b.id "
        "JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
        "WHERE " + " AND ".join(predicates) + " ORDER BY p.id",
        tuple(parameters),
    )


def semantic_search(
    library: Library, query: str, mode: str, book_id: str | None, language: str | None, limit: int
) -> dict[str, Any]:
    started = time.perf_counter()
    active = library.data_dir / "vectors" / "active.json"

    def fallback(reason: str, coverage: dict[str, Any] | None = None) -> dict[str, Any]:
        result = library.search(query, "lexical", book_id, language, limit)
        result["warnings"].append(reason)
        if coverage is not None:
            result["semantic_coverage"] = coverage
        return result

    # An explicitly configured, source-bound overlay may serve a narrow scope.
    # Global queries and ordinary installations retain the original index path.
    if os.getenv("BOOKRAG_SEMANTIC_OVERLAY") and (book_id is not None or language == "ru"):
        try:
            from book_rag.semantic_overlay import overlay_search

            overlay = overlay_search(library, query, mode, book_id, language, limit)
            if overlay is not None:
                return overlay
        except (ImportError, OSError, ValueError, TypeError, RuntimeError, sqlite3.Error):
            return fallback("Дополнительный русский смысловой индекс недоступен. Показан поиск по словам.")

    if not active.exists():
        return fallback("Semantic index unavailable. Showing lexical search. Run bookrag embed.")
    receipt: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None
    before_state: Mapping[str, Any] | None = None
    reference = getattr(library, "semantic_reference", None) or library
    try:
        if active.is_symlink() or active.stat().st_size > 32 * 1024**2:
            raise ValueError("Invalid active metadata file")
        metadata = json.loads(active.read_text())
        if not isinstance(metadata, dict):
            raise ValueError("Active metadata must be an object")
        if metadata.get("coverage_kind") == "subset":
            from book_rag.index_coverage import validate_coverage
            from book_rag.index_installation import read_installation_receipt

            # Isolated evaluations retain evidence in an owned DB while their
            # immutable generation/epoch proof belongs to the original input.
            receipt = read_installation_receipt(reference.data_dir, metadata)
            before_state = validate_coverage(reference, receipt["coverage"], receipt["coverage_binding"])
            if not before_state["valid"]:
                return fallback("Смысловой индекс требует обновления. Показан актуальный поиск по словам.")
            with library.connect() as conn:
                searchable = conn.execute("SELECT COUNT(*) FROM books WHERE status='indexed'").fetchone()[0]
            coverage = {
                "indexed_books": receipt["coverage"]["books"],
                "searchable_books": searchable,
                "active": True,
                "requested_book_covered": book_id in receipt["coverage"]["covered_book_ids"] if book_id else None,
            }
            if coverage["requested_book_covered"] is False:
                return fallback(
                    "Для выбранной книги доступен поиск по словам; она ещё не входит в смысловой индекс.", coverage
                )
            if language:
                with library.connect() as conn:
                    language_books = {
                        row[0]
                        for row in conn.execute(
                            "SELECT id FROM books WHERE status='indexed' AND language=? AND (? IS NULL OR id=?)",
                            (language, book_id, book_id),
                        )
                    }
                if not language_books.intersection(receipt["coverage"]["covered_book_ids"]):
                    return fallback(
                        "Для выбранного языка доступен поиск по словам; эти издания ещё не входят в смысловой индекс.",
                        coverage,
                    )
        elif metadata.get("coverage_kind") is not None:
            raise ValueError("Unknown semantic coverage kind")
        elif metadata.get("corpus_fingerprint") != corpus_fingerprint(library):
            return fallback("Semantic index stale. Showing lexical search; run bookrag embed.")
        generation = metadata.get("generation")
        # Legacy local indexes used other simple basenames. Installed subsets
        # have the stronger gen-* identity checked by their sealed receipt.
        if not isinstance(generation, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}", generation):
            raise ValueError("Invalid vector generation name")
        folder = (reference.data_dir / "vectors" if receipt is not None else active.parent) / generation
        vectors = np.load(folder / "embeddings.npy", mmap_mode="r", allow_pickle=False)
        ids = np.load(folder / "ids.npy", mmap_mode="r", allow_pickle=False)
        if (
            vectors.ndim != 2
            or vectors.dtype != np.dtype("float32")
            or ids.ndim != 1
            or ids.dtype != np.dtype("int64")
            or vectors.shape[0] != len(ids)
            or not len(ids)
            or metadata.get("vectors", len(ids)) != len(ids)
        ):
            raise ValueError("Vector/ID arrays have incompatible shapes or types")
        eligible = None
        if book_id is not None or language is not None:
            with library.connect() as conn:
                eligibility_sql, eligibility_parameters = _scoped_passage_query(book_id, language)
                eligible = np.fromiter(
                    (row[0] for row in conn.execute(eligibility_sql, eligibility_parameters)),
                    dtype=np.int64,
                )
        profile = profile_from_metadata(metadata)
        model = _model(metadata["model"], str(_model_cache(library)), metadata.get("model_revision"))
        query_vector = np.asarray(
            model.encode([profile.query_input(query)], normalize_embeddings=True, convert_to_numpy=True)[0]
        )
        if query_vector.shape != (vectors.shape[1],) or not np.isfinite(query_vector).all():
            raise ValueError("Invalid query vector")
        scores = _score_vectors(vectors, ids, query_vector, eligible, ordered_ids=receipt is not None)
    except ImportError:
        return fallback("Install the semantic extra to enable multilingual embeddings.")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, MemoryError, sqlite3.Error):
        return fallback("Смысловой индекс недоступен или не прошёл проверку. Показан поиск по словам.")
    threshold = float(os.getenv("BOOKRAG_SEMANTIC_THRESHOLD", "0.30"))
    candidate_ids, best_scores, window_count = _candidate_passages(scores, ids, threshold)
    evidence = {passage["id"]: passage for passage in library.passages(candidate_ids)}
    vector_results: list[dict[str, Any]] = []
    warnings: list[str] = []
    if coverage and coverage["indexed_books"] < coverage["searchable_books"] and not book_id:
        warnings.append(
            f"Поиск по смыслу охватывает {coverage['indexed_books']} из {coverage['searchable_books']} изданий; "
            "поиск по словам работает по всей библиотеке."
        )
    from book_rag.retrieval import prepare_query, rerank_results

    plan = prepare_query(query)
    for passage_id in candidate_ids:
        passage = evidence.get(passage_id)
        if passage and passage["verified"]:
            score = best_scores[passage_id]
            vector_results.append(passage | {"score": score, "semantic_score": score})
        elif passage:
            warnings.append("Corrupt semantic citation suppressed; rebuild the affected book.")
    # Quoted phrase constraints apply in every retrieval mode, including vectors.
    if plan.phrases:
        vector_results = [r for r in vector_results if rerank_results(plan, [r], limit=1)]
    results = vector_results
    if mode == "hybrid":
        lexical = library.search(query, "lexical", book_id, language, HYBRID_LEXICAL_CANDIDATES)["results"]
        merged: dict[int, dict[str, Any]] = {}
        fused: dict[int, float] = {}
        for ranking in (lexical, vector_results):
            for rank, item in enumerate(ranking, 1):
                merged.setdefault(item["id"], item)
                fused[item["id"]] = fused.get(item["id"], 0) + 1 / (60 + rank)
        results = [merged[i] | {"score": score} for i, score in sorted(fused.items(), key=lambda kv: -kv[1])]
    if receipt is not None:
        try:
            read_installation_receipt(reference.data_dir, metadata)
            after_state = validate_coverage(reference, receipt["coverage"], receipt["coverage_binding"])
            if (
                not after_state["valid"]
                or before_state is None
                or after_state["state_token"] != before_state["state_token"]
            ):
                return fallback("Текст изменился во время поиска. Показаны актуальные результаты по словам.")
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            return fallback("Смысловой индекс изменился во время поиска. Показаны результаты по словам.")
    elif corpus_fingerprint(library) != metadata["corpus_fingerprint"]:
        return fallback("Corpus changed during semantic search. Showing current lexical results.")
    response = {
        "query": query,
        "mode": mode,
        "results": results[:limit],
        "warnings": list(dict.fromkeys(warnings)),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "model": metadata["model"],
        "retrieval_strategy": RETRIEVAL_STRATEGY,
        "candidate_pool": {
            "window_limit": SEMANTIC_WINDOW_CANDIDATES,
            "passage_limit": SEMANTIC_PASSAGE_CANDIDATES,
            "ranked_windows": window_count,
            "unique_passages": len(candidate_ids),
            "verified_passages": len(vector_results),
            "lexical_limit": HYBRID_LEXICAL_CANDIDATES if mode == "hybrid" else 0,
        },
    }
    if coverage is not None:
        response["semantic_coverage"] = coverage
    return response
