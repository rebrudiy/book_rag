"""Optional local multilingual embeddings, cosine retrieval and reciprocal-rank fusion.

The lexical baseline never needs a model. Vector generation and active-index
replacement are atomic; fingerprints prevent searching stale vector snapshots.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from book_rag.library import Library

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def corpus_fingerprint(library: Library) -> str:
    h = hashlib.sha256()
    with library.connect() as conn:
        for row in conn.execute(
            "SELECT p.id,p.sha256 FROM passages p JOIN books b ON b.id=p.book_id WHERE b.status='indexed' ORDER BY p.id"
        ):
            h.update(f"{row['id']}:{row['sha256']}\n".encode())
    return h.hexdigest()


@lru_cache(maxsize=2)
def _model(name: str, cache: str) -> Any:
    sentence_transformers = importlib.import_module("sentence_transformers")
    return sentence_transformers.SentenceTransformer(name, cache_folder=cache)


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


def build_index(library: Library, model_name: str | None = None, batch_size: int = 32) -> dict[str, Any]:
    if batch_size < 1 or batch_size > 512:
        raise ValueError("batch_size must be between 1 and 512")
    started = time.perf_counter()
    name = model_name or os.getenv("BOOKRAG_EMBEDDING_MODEL", DEFAULT_MODEL)
    model = _model(name, str(library.data_dir / "models"))
    directory = library.data_dir / "vectors"
    directory.mkdir(exist_ok=True)
    generation = uuid.uuid4().hex
    staging = directory / generation
    staging.mkdir()
    fingerprint = corpus_fingerprint(library)
    sql = (
        "SELECT p.id,p.text,b.title,b.author,s.title section_title FROM passages p "
        "JOIN books b ON b.id=p.book_id JOIN sections s ON s.id=p.section_id "
        "WHERE b.status='indexed' ORDER BY p.id"
    )
    try:
        with library.connect() as conn:
            count = 0
            passages = 0
            for row in conn.execute(sql):
                count += len(_windows(model, row))
                passages += 1
            if not count:
                raise ValueError("Import books before building an embedding index")
            dim = model.get_embedding_dimension()
            vectors = np.lib.format.open_memmap(
                staging / "embeddings.npy", mode="w+", dtype="float32", shape=(count, dim)
            )
            ids = np.lib.format.open_memmap(staging / "ids.npy", mode="w+", dtype="int64", shape=(count,))
            pending: list[str] = []
            pending_ids: list[int] = []
            offset = 0

            def flush() -> None:
                nonlocal offset
                batch = model.encode(
                    pending,
                    batch_size=batch_size,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
                vectors[offset : offset + len(pending)] = batch
                ids[offset : offset + len(pending)] = pending_ids
                offset += len(pending)
                pending.clear()
                pending_ids.clear()

            for row in conn.execute(sql):
                for window in _windows(model, row):
                    pending.append(window)
                    pending_ids.append(row["id"])
                    if len(pending) >= batch_size:
                        flush()
            if pending:
                flush()
            vectors.flush()
            ids.flush()
        if corpus_fingerprint(library) != fingerprint:
            raise RuntimeError("Corpus changed during embedding; retry after ingestion finishes")
        metadata = {
            "generation": generation,
            "model": name,
            "corpus_fingerprint": fingerprint,
            "passages": passages,
            "vectors": count,
            "dimensions": dim,
            "seconds": round(time.perf_counter() - started, 3),
            "model_max_tokens": model.max_seq_length,
            "strategy": "token-windows-v2",
            "overlap_tokens": 20,
            "context_header_tokens": 24,
            "context": "bounded title/author/section header + overlapping tokenizer-offset text windows",
        }
        (staging / "metadata.json").write_text(json.dumps(metadata, indent=2))
        temporary = directory / f"active-{generation}.tmp"
        temporary.write_text(json.dumps(metadata, indent=2))
        temporary.replace(directory / "active.json")
        return metadata
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


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
        model = _model(metadata["model"], str(library.data_dir / "models"))
    except ImportError:
        result = library.search(query, "lexical", book_id, language, limit)
        result["warnings"].append("Install the semantic extra to enable multilingual embeddings.")
        return result
    folder = active.parent / metadata["generation"]
    vectors = np.load(folder / "embeddings.npy", mmap_mode="r")
    ids = np.load(folder / "ids.npy", mmap_mode="r")
    query_vector = model.encode([query], normalize_embeddings=True, convert_to_numpy=True)[0]
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
    k = min(len(scores), max(50, limit * 4))
    positions = np.argpartition(-scores, k - 1)[:k] if k else np.array([], dtype=int)
    positions = positions[np.argsort(-scores[positions], kind="stable")]
    threshold = float(os.getenv("BOOKRAG_SEMANTIC_THRESHOLD", "0.30"))
    vector_results = []
    seen: set[int] = set()
    from book_rag.retrieval import prepare_query, rerank_results

    plan = prepare_query(query)
    for pos in positions:
        if float(scores[pos]) < threshold:
            continue
        passage_id = int(ids[pos])
        if passage_id in seen:
            continue
        seen.add(passage_id)
        passage = library.passage(passage_id)
        if passage and passage["verified"]:
            passage["score"] = float(scores[pos])
            passage["semantic_score"] = float(scores[pos])
            vector_results.append(passage)
    # Quoted phrase constraints apply in every retrieval mode, including vectors.
    if plan.phrases:
        vector_results = [r for r in vector_results if rerank_results(plan, [r], limit=1)]
    results = vector_results
    if mode == "hybrid":
        lexical = library.search(query, "lexical", book_id, language, 50)["results"]
        merged: dict[int, dict[str, Any]] = {}
        fused: dict[int, float] = {}
        for ranking in (lexical, vector_results):
            for rank, item in enumerate(ranking, 1):
                merged.setdefault(item["id"], item)
                fused[item["id"]] = fused.get(item["id"], 0) + 1 / (60 + rank)
        results = [merged[i] | {"score": score} for i, score in sorted(fused.items(), key=lambda kv: -kv[1])]
    return {
        "query": query,
        "mode": mode,
        "results": results[:limit],
        "warnings": [],
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "model": metadata["model"],
    }
