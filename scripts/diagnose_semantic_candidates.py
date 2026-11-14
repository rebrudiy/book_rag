"""Inspect a known retrieval failure in a frozen completed MiniLM generation.

No Library constructor, server, index writes, remote weights or activation.
Run only after coordinating other model jobs. The default inference device is
CPU; --help does not import Torch or load a model. Public JSON contains metadata,
canonical IDs and input hashes, never document excerpts. This is a diagnostic of
an inspected development failure, not a held-out relevance evaluation.

Example, AFTER the root agent releases the model slot:
  uv run python scripts/diagnose_semantic_candidates.py \
    data/snapshots/library1308-v1 \
    --query 'Человек просыпается и обнаруживает, что превратился в насекомое' \
    --target-book pg22367-full --model-cache data/models --device cpu \
    --output data/diagnostics/kafka-candidates.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import re
import resource
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from book_rag.embedding_profiles import MINILM_MODEL, MINILM_REVISION, profile_from_metadata
from book_rag.semantic import (
    SEMANTIC_PASSAGE_CANDIDATES,
    SEMANTIC_WINDOW_CANDIDATES,
    _candidate_passages,
    _profile_windows,
)

BLOCK_ROWS = 8192


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_state(path: Path) -> tuple[int, int, int, int, int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular nonsymlink file: {path.name}")
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def read_json(path: Path) -> dict[str, Any]:
    if file_state(path)[2] > 32 * 1024**2:
        raise ValueError("Metadata JSON is unexpectedly large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Metadata must be a JSON object")
    return value


@contextmanager
def frozen_connection(database: Path) -> Generator[sqlite3.Connection]:
    before = file_state(database)
    wal = database.with_name(database.name + "-wal")
    if wal.is_symlink() or (wal.exists() and wal.stat().st_size):
        raise ValueError("Select a checkpointed frozen SQLite source without a pending WAL")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        yield connection
    finally:
        connection.close()
        if file_state(database) != before or (wal.exists() and wal.stat().st_size):
            raise RuntimeError("Frozen database changed during diagnosis")


def local_model(cache: Path, device: str) -> Any:
    snapshot = cache / ("models--" + MINILM_MODEL.replace("/", "--")) / "snapshots" / MINILM_REVISION
    if not snapshot.is_dir():
        raise ValueError("The exact pinned MiniLM snapshot must already exist locally; downloads are disabled")
    module = importlib.import_module("sentence_transformers")
    return module.SentenceTransformer(str(snapshot), local_files_only=True, trust_remote_code=False, device=device)


def rank_at(scores: np.ndarray, position: int, eligible: np.ndarray | None = None) -> int:
    """Exact one-based rank; equal scores use immutable window position."""
    if eligible is not None and not eligible[position]:
        raise ValueError("Window is outside the requested rank scope")
    selected = scores if eligible is None else scores[eligible]
    better = int(np.count_nonzero(selected > scores[position]))
    earlier = scores[:position] == scores[position]
    if eligible is not None:
        earlier &= eligible[:position]
    return 1 + better + int(np.count_nonzero(earlier))


def top_positions(scores: np.ndarray, count: int) -> np.ndarray:
    """Stable exact top K, including deterministic selection of boundary ties."""
    count = min(count, len(scores))
    if count < 1:
        return np.array([], dtype=np.int64)
    cutoff = np.partition(scores, len(scores) - count)[len(scores) - count]
    higher = np.flatnonzero(scores > cutoff)
    tied = np.flatnonzero(scores == cutoff)[: count - len(higher)]
    positions = np.concatenate((higher, tied))
    return positions[np.lexsort((positions, -scores[positions]))]


def passage_metadata(connection: sqlite3.Connection, identities: list[int]) -> dict[int, dict[str, Any]]:
    result = {}
    for start in range(0, len(identities), 400):
        batch = identities[start : start + 400]
        marks = ",".join("?" for _ in batch)
        for row in connection.execute(
            "SELECT p.id,p.book_id,p.section_id,b.title,b.author,b.language "
            f"FROM passages p JOIN books b ON b.id=p.book_id WHERE p.id IN ({marks})",
            batch,
        ):
            result[row["id"]] = dict(row)
    if set(result) != set(identities):
        raise ValueError("A ranked index ID is absent from the frozen source")
    return result


def distribution(connection: sqlite3.Connection, identities: list[int]) -> dict[str, Any]:
    metadata = passage_metadata(connection, list(dict.fromkeys(identities)))
    books = Counter(metadata[identity]["book_id"] for identity in identities)
    languages = Counter(metadata[identity]["language"] for identity in identities)
    return {
        "rows": len(identities),
        "distinct_passages": len(set(identities)),
        "distinct_books": len(books),
        "books": [{"book_id": key, "count": count} for key, count in books.most_common()],
        "languages": dict(languages.most_common()),
    }


def first_sentence(text: str) -> str:
    """Return an exact source prefix; punctuation splitting can stop at abbreviations."""
    boundary = re.search(r"[.!?。！？](?:[\"'»”’)]*)(?=\s|$)", text)
    return text[: boundary.end()] if boundary else text


def encode(model: Any, texts: list[str], dimensions: int) -> np.ndarray:
    values = np.asarray(
        model.encode(texts, normalize_embeddings=True, convert_to_numpy=True, batch_size=4), dtype=np.float32
    )
    if values.shape != (len(texts), dimensions) or not np.isfinite(values).all():
        raise ValueError("Encoder returned incompatible or nonfinite vectors")
    if not np.allclose(np.linalg.norm(values, axis=1), 1.0, atol=0.002):
        raise ValueError("Encoder did not return unit normalized vectors")
    return values


def run(
    source: Path,
    query: str,
    target_book: str,
    *,
    target_passage: int | None = None,
    model_cache: Path = Path("data/models"),
    device: str = "cpu",
    top_k: int = 100,
    fresh_windows: int = 3,
    threshold: float = 0.30,
    model: Any = None,
) -> dict[str, Any]:
    if not query.strip() or not target_book or not 1 <= top_k <= 2000 or not 1 <= fresh_windows <= 8:
        raise ValueError("Provide a query/target, top K 1..2000 and fresh-window count 1..8")
    if not math.isfinite(threshold) or not -1 <= threshold <= 1:
        raise ValueError("Threshold must be finite and in [-1,1]")
    started = time.perf_counter()
    source = source.expanduser().resolve(strict=True)
    active = source / "vectors" / "active.json"
    metadata = read_json(active)
    generation = metadata.get("generation", "")
    if not isinstance(generation, str) or not re.fullmatch(r"gen-[A-Za-z0-9_-]{1,160}", generation):
        raise ValueError("Select an immutable completed gen-* generation, never staging")
    folder = active.parent / generation
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("Generation must be a real local directory")
    profile = profile_from_metadata(metadata)
    if (
        metadata.get("model") != MINILM_MODEL
        or metadata.get("model_revision") != MINILM_REVISION
        or metadata.get("model_max_tokens") != 128
        or metadata.get("dimensions") != 384
        or metadata.get("strategy") != "token-windows-v2"
        or metadata.get("context_header_tokens") != 24
        or metadata.get("overlap_tokens") != 20
        or profile.query_prefix
        or profile.document_prefix
        or profile.max_tokens is not None
    ):
        raise ValueError("This diagnostic supports only the frozen pinned unprefixed MiniLM128/v2 contract")
    checkpoint = read_json(folder / "checkpoint.json")
    receipt = read_json(folder / "vector-reuse.json")
    if (
        receipt.get("complete") is not True
        or receipt.get("generation") != generation
        or receipt.get("vectors") != metadata.get("vectors")
        or checkpoint.get("completed_vectors") != metadata.get("vectors")
        or read_json(folder / "metadata.json") != metadata
        or receipt.get("metadata_sha256") != digest((folder / "metadata.json").read_bytes())
        or receipt.get("checkpoint_sha256") != digest((folder / "checkpoint.json").read_bytes())
    ):
        raise ValueError("Incomplete generation or mismatched completed metadata receipt")
    watched = [active, source / "library.sqlite3"] + [
        folder / name for name in ("embeddings.npy", "ids.npy", "metadata.json", "checkpoint.json", "vector-reuse.json")
    ]
    before = {path: file_state(path) for path in watched}
    if read_json(active) != metadata:
        raise RuntimeError("Active metadata changed before diagnosis")
    vectors = np.load(folder / "embeddings.npy", mmap_mode="r", allow_pickle=False)
    identities = None
    try:
        identities = np.load(folder / "ids.npy", mmap_mode="r", allow_pickle=False)
        if (
            vectors.dtype != np.dtype("float32")
            or vectors.shape != (metadata["vectors"], 384)
            or identities.dtype != np.dtype("int64")
            or identities.shape != (len(vectors),)
            or vectors.offset + vectors.nbytes != (folder / "embeddings.npy").stat().st_size
            or identities.offset + identities.nbytes != (folder / "ids.npy").stat().st_size
            or not len(vectors)
        ):
            raise ValueError("Vector/ID arrays have incompatible dtype/shape/exact file length")
        with frozen_connection(source / "library.sqlite3") as connection:
            target = connection.execute(
                "SELECT * FROM books WHERE id=? AND status='indexed'", (target_book,)
            ).fetchone()
            if target is None:
                raise ValueError("Target edition is absent or not indexed in the frozen source")
            target_ids = [
                row[0] for row in connection.execute("SELECT id FROM passages WHERE book_id=?", (target_book,))
            ]
            if target_passage is not None and target_passage not in target_ids:
                raise ValueError("Requested passage does not belong to the target edition")
            fingerprint = hashlib.sha256()
            for row in connection.execute(
                "SELECT p.id,p.sha256 FROM passages p JOIN books b ON b.id=p.book_id "
                "WHERE b.status='indexed' ORDER BY p.id"
            ):
                fingerprint.update(f"{row[0]}:{row[1]}\n".encode())
            if fingerprint.hexdigest() != metadata.get("corpus_fingerprint"):
                raise ValueError("Frozen corpus does not match the active generation fingerprint")
            model = model if model is not None else local_model(model_cache.expanduser().resolve(), device)
            if model.max_seq_length != 128:
                raise ValueError("Loaded model's sequence limit differs from frozen input contract")
            query_vector = encode(model, [profile.query_input(query)], 384)[0]
            scores = np.empty(len(vectors), dtype=np.float32)
            vector_hash = hashlib.sha256()
            id_hash = hashlib.sha256()
            for start in range(0, len(vectors), BLOCK_ROWS):
                block = vectors[start : start + BLOCK_ROWS]
                if not np.isfinite(block).all() or not np.allclose(np.linalg.norm(block, axis=1), 1.0, atol=0.002):
                    raise ValueError("Stored vectors are nonfinite or not unit normalized")
                vector_hash.update(memoryview(block).cast("B"))
                id_hash.update(memoryview(identities[start : start + BLOCK_ROWS]).cast("B"))
                scores[start : start + len(block)] = block @ query_vector
            if (
                vector_hash.hexdigest() != checkpoint.get("vector_checksum")
                or id_hash.hexdigest() != checkpoint.get("id_checksum")
                or vector_hash.hexdigest() != receipt.get("vector_checksum")
            ):
                raise ValueError("Actual completed vector/ID payload checksums do not match")
            positions = np.flatnonzero(np.isin(identities, target_ids))
            if not len(positions):
                raise ValueError("The target has no windows in this completed generation")
            ordered = positions[np.lexsort((positions, -scores[positions]))]
            language_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT p.id FROM passages p JOIN books b ON b.id=p.book_id "
                    "WHERE b.status='indexed' AND b.language=?",
                    (target["language"],),
                )
            ]
            language_mask = np.isin(identities, language_ids)
            candidates, _, _ = _candidate_passages(scores, identities, threshold)
            raw_windows = top_positions(scores, SEMANTIC_WINDOW_CANDIDATES)

            def window_info(position: int) -> dict[str, Any]:
                identity = int(identities[position])
                ordinal = int(np.count_nonzero(identities[:position] == identity))
                return {
                    "window_position": position,
                    "passage_id": identity,
                    "passage_window_ordinal": ordinal,
                    "cosine": float(scores[position]),
                    "global_window_rank": rank_at(scores, position),
                    "target_language_window_rank": rank_at(scores, position, language_mask),
                    "above_floor": bool(scores[position] >= threshold),
                    "in_global_2000_windows": bool(position in raw_windows),
                    "in_first_stage_100_passages": identity in candidates,
                }

            sample_positions: list[int] = []
            if target_passage is not None:
                passage_positions = ordered[identities[ordered] == target_passage]
                if not len(passage_positions):
                    raise ValueError("Requested passage has no windows in the completed generation")
                sample_positions.append(int(passage_positions[0]))
            for position in ordered:
                if len(sample_positions) >= fresh_windows:
                    break
                if all(identities[position] != identities[other] for other in sample_positions):
                    sample_positions.append(int(position))
            samples = []
            for position in sample_positions:
                info = window_info(position)
                row = connection.execute(
                    "SELECT p.id,p.text,p.sha256,b.title,b.author,s.title section_title "
                    "FROM passages p JOIN books b ON b.id=p.book_id JOIN sections s ON s.id=p.section_id "
                    "WHERE p.id=?",
                    (info["passage_id"],),
                ).fetchone()
                if row is None or digest(row["text"].encode()) != row["sha256"]:
                    raise ValueError("Canonical target passage hash mismatch")
                inputs = _profile_windows(model, row, profile)
                if len(inputs) != np.count_nonzero(identities == row["id"]):
                    raise ValueError("Regenerated target window count differs from frozen IDs")
                original = inputs[info["passage_window_ordinal"]]
                header_ids = model.tokenizer.encode(
                    f"{row['title']} — {row['author']} / {row['section_title']}", add_special_tokens=False
                )[:24]
                header = model.tokenizer.decode(header_ids, skip_special_tokens=True) + "\n"
                if not original.startswith(header):
                    raise ValueError("Regenerated header differs from the frozen window contract")
                variants = {
                    "frozen_header_window_128": original,
                    "same_exact_window_without_header": original[len(header) :],
                    "exact_passage_first_sentence_without_header": first_sentence(row["text"]),
                }
                fresh = encode(model, list(variants.values()), 384)
                info["canonical_text_sha256"] = row["sha256"]
                info["input_variants"] = []
                for index, (name, text) in enumerate(variants.items()):
                    tokens = len(model.tokenizer.encode(text, add_special_tokens=True))
                    info["input_variants"].append(
                        {
                            "name": name,
                            "input_sha256": digest(text.encode()),
                            "characters": len(text),
                            "tokens_including_special": tokens,
                            "truncated_by_128_limit": tokens > 128,
                            "fresh_query_cosine": float(fresh[index] @ query_vector),
                            "stored_query_cosine": float(scores[position]) if index == 0 else None,
                            "fresh_stored_vector_cosine": float(fresh[index] @ vectors[position])
                            if index == 0
                            else None,
                            "fresh_stored_max_abs_difference": float(np.max(np.abs(fresh[index] - vectors[position])))
                            if index == 0
                            else None,
                        }
                    )
                samples.append(info)
            top = top_positions(scores, top_k)
            report = {
                "created_at": datetime.now(UTC).isoformat(),
                "kind": "known_inspected_failure_diagnostic_not_heldout_or_promotion",
                "query": query,
                "target_book": {key: target[key] for key in ("id", "title", "author", "language")},
                "target_passage_id": target_passage,
                "index": {
                    key: metadata[key] for key in ("generation", "model", "model_revision", "vectors", "passages")
                },
                "model_device": str(getattr(model, "device", device)),
                "score_math": "blocked float32 cosine; exact ranks break score ties by immutable window position",
                "semantic_floor": threshold,
                "pool": {"passages": SEMANTIC_PASSAGE_CANDIDATES, "windows": SEMANTIC_WINDOW_CANDIDATES},
                "best_target_window": window_info(int(ordered[0])),
                "target_book_in_first_stage": bool(set(target_ids).intersection(candidates)),
                "target_book_candidates": [identity for identity in candidates if identity in set(target_ids)],
                "top_window_distribution": distribution(connection, [int(identities[position]) for position in top]),
                "first_stage_distribution": distribution(connection, candidates),
                "target_reencoding": samples,
                "source_guards": {
                    "sqlite_mode": "ro&immutable=1; query_only; no Library constructor",
                    "database_stat_identity_unchanged": True,
                    "active_and_array_stat_identities_unchanged": True,
                    "source_passage_fingerprint_matches": True,
                    "complete_vector_and_id_payload_checksums_verified": True,
                    "database_full_file_sha256_checked": False,
                },
                "limitations": [
                    "Known inspected regression, not a blind relevance test or evidence of improvement.",
                    "Input variants change only sampled inputs; there is no alternative full-corpus index or ranking.",
                    "Fresh device/batch arithmetic can differ from storage; drift is measured, not hidden.",
                    "First-sentence boundaries use simple punctuation and can stop at abbreviations.",
                    "Rank tie selection at the production argpartition boundary can differ from stable raw top-K.",
                    "No external text, translations, models or index generations are downloaded or activated.",
                ],
            }
        if any(file_state(path) != value for path, value in before.items()):
            raise RuntimeError("Frozen source/index changed during diagnosis")
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        report["process_peak_rss_mib"] = round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**2 if sys.platform == "darwin" else 1024), 2
        )
        return report
    finally:
        for array in (vectors, identities):
            mapping = getattr(array, "_mmap", None)
            if mapping is not None:
                mapping.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--query", required=True)
    parser.add_argument("--target-book", required=True)
    parser.add_argument("--target-passage", type=int)
    parser.add_argument("--model-cache", type=Path, default=Path("data/models"))
    parser.add_argument("--device", choices=("cpu", "mps"), default="cpu")
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument("--fresh-windows", type=int, default=3)
    parser.add_argument("--threshold", type=float, default=0.30)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.output and arguments.output.expanduser().resolve().is_relative_to(
        arguments.source.expanduser().resolve()
    ):
        parser.error("Diagnostic output must remain outside the frozen source directory")
    report = run(
        arguments.source,
        arguments.query,
        arguments.target_book,
        target_passage=arguments.target_passage,
        model_cache=arguments.model_cache,
        device=arguments.device,
        top_k=arguments.top_k,
        fresh_windows=arguments.fresh_windows,
        threshold=arguments.threshold,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = arguments.output.with_name(arguments.output.name + f".{os.getpid()}.tmp")
        try:
            temporary.write_text(encoded, encoding="utf-8")
            temporary.replace(arguments.output)
        finally:
            temporary.unlink(missing_ok=True)
    print(encoded, end="")


if __name__ == "__main__":
    main()
