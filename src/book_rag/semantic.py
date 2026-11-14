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
import time
import uuid
from collections.abc import Generator, Iterator
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


def build_index(library: Library, model_name: str | None = None, batch_size: int = 32) -> dict[str, Any]:
    """Build or resume durable batches, activating only a complete current index."""
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
    with _exclusive_build(directory):
        fingerprint = corpus_fingerprint(library)
        identity_fields = {
            "model": name,
            "model_revision": revision,
            "corpus_fingerprint": fingerprint,
            **_STRATEGY,
            "embedding_profile": profile.metadata(revision),
            "strategy": profile.strategy,
        }
        identity_fields.update(model_max_tokens=model.max_seq_length, dimensions=model.get_embedding_dimension())
        identity = hashlib.sha256(json.dumps(identity_fields, sort_keys=True).encode()).hexdigest()
        staging = directory / ("staging-" + identity)
        checkpoint = _read_checkpoint(staging)
        completed = checkpoint.get("completed_vectors", 0)
        if type(completed) is not int or completed < 0:
            completed = 0
        with library.connect() as conn:
            conn.execute("BEGIN")
            count, passages, cursor, final_cursor = _count_windows(conn, model, completed, profile)
            if not count:
                raise ValueError("Import books before building an embedding index")
            descriptor = {**identity_fields, "vectors": count, "passages": passages, "build_identity": identity}
            if corpus_fingerprint(library) != fingerprint:
                raise RuntimeError("Corpus changed during embedding planning; retry after ingestion finishes")

            # Recover a completed rename if interruption happened just before activation.
            for ready in sorted(directory.glob("gen-" + identity + "-*")):
                saved = _read_checkpoint(ready)
                try:
                    if saved.get("completed_vectors") != count:
                        continue
                    ready_arrays = _resume_arrays(ready, saved, descriptor, final_cursor)
                    for array in ready_arrays[:2]:
                        array._mmap.close()
                    metadata = json.loads((ready / "metadata.json").read_text())
                    if metadata.get("generation") != ready.name or any(
                        metadata.get(k) != v for k, v in descriptor.items()
                    ):
                        continue
                    if corpus_fingerprint(library) != fingerprint:
                        raise RuntimeError("Corpus changed during index reuse; retry after ingestion finishes")
                    _atomic_json(directory / "active.json", metadata)
                    return metadata | {"reused_active": True, "resumed_vectors": count}
                except (ValueError, OSError, KeyError):
                    continue

            resumed = 0
            try:
                vectors, ids, vector_hash, id_hash = _resume_arrays(staging, checkpoint, descriptor, cursor)
                resumed = completed
            except (ValueError, OSError, KeyError, TypeError):
                vectors, ids, vector_hash, id_hash = _new_arrays(staging, descriptor)
                completed, cursor = 0, None
            offset = completed

            def save_checkpoint(last_cursor: dict[str, int] | None) -> None:
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
                    },
                )

            save_checkpoint(cursor)
            pending: list[str] = []
            pending_ids: list[int] = []
            last_cursor = cursor

            def flush() -> None:
                nonlocal offset
                batch = np.asarray(
                    model.encode(
                        pending,
                        batch_size=batch_size,
                        normalize_embeddings=True,
                        convert_to_numpy=True,
                        show_progress_bar=False,
                    ),
                    dtype="float32",
                )
                if batch.shape != (len(pending), descriptor["dimensions"]) or not np.isfinite(batch).all():
                    raise ValueError("Model returned invalid embedding dimensions or nonfinite values")
                batch_ids = np.asarray(pending_ids, dtype="int64")
                vectors[offset : offset + len(pending)] = batch
                ids[offset : offset + len(pending)] = batch_ids
                vector_hash.update(batch.tobytes(order="C"))
                id_hash.update(batch_ids.tobytes(order="C"))
                offset += len(pending)
                save_checkpoint(last_cursor)
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
            raise RuntimeError("Corpus changed during embedding; checkpoint retained, retry after ingestion finishes")
        generation = "gen-" + identity + "-" + uuid.uuid4().hex[:12]
        metadata = {
            **descriptor,
            "generation": generation,
            "seconds": round(time.perf_counter() - started, 3),
            "resumed_vectors": resumed,
            "requested_revision": requested_revision,
        }
        _atomic_json(staging / "metadata.json", metadata)
        vectors._mmap.close()
        ids._mmap.close()
        staging.rename(directory / generation)
        _atomic_json(directory / "active.json", metadata)
        return metadata


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


def semantic_search(
    library: Library, query: str, mode: str, book_id: str | None, language: str | None, limit: int
) -> dict[str, Any]:
    started = time.perf_counter()
    active = library.data_dir / "vectors" / "active.json"
    if not active.exists():
        result = library.search(query, "lexical", book_id, language, limit)
        result["warnings"].append("Semantic index unavailable. Showing lexical search. Run bookrag embed.")
        return result
    metadata = json.loads(active.read_text())
    if metadata["corpus_fingerprint"] != corpus_fingerprint(library):
        result = library.search(query, "lexical", book_id, language, limit)
        result["warnings"].append("Semantic index stale. Showing lexical search; run bookrag embed.")
        return result
    try:
        model = _model(metadata["model"], str(_model_cache(library)), metadata.get("model_revision"))
    except ImportError:
        result = library.search(query, "lexical", book_id, language, limit)
        result["warnings"].append("Install the semantic extra to enable multilingual embeddings.")
        return result
    folder = active.parent / metadata["generation"]
    vectors = np.load(folder / "embeddings.npy", mmap_mode="r")
    ids = np.load(folder / "ids.npy", mmap_mode="r")
    profile = profile_from_metadata(metadata)
    query_vector = model.encode([profile.query_input(query)], normalize_embeddings=True, convert_to_numpy=True)[0]
    scores = np.asarray(vectors @ query_vector)
    if book_id or language:
        with library.connect() as conn:
            eligible = {
                r[0]
                for r in conn.execute(
                    "SELECT p.id FROM passages p JOIN books b ON b.id=p.book_id "
                    "WHERE (? IS NULL OR b.id=?) AND (? IS NULL OR b.language=?) AND b.status='indexed'",
                    (book_id, book_id, language, language),
                )
            }
        scores[~np.isin(ids, list(eligible))] = -np.inf
    threshold = float(os.getenv("BOOKRAG_SEMANTIC_THRESHOLD", "0.30"))
    candidate_ids, best_scores, window_count = _candidate_passages(scores, ids, threshold)
    evidence = {passage["id"]: passage for passage in library.passages(candidate_ids)}
    vector_results: list[dict[str, Any]] = []
    warnings: list[str] = []
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
    if corpus_fingerprint(library) != metadata["corpus_fingerprint"]:
        result = library.search(query, "lexical", book_id, language, limit)
        result["warnings"].append("Corpus changed during semantic search. Showing current lexical results.")
        return result
    return {
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
