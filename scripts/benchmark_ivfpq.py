"""Bounded stored-vector ANN probe; no Library, SQLite, encoders or final labels.

The parent process watches child RSS and time. Source arrays are opened only
read-only and fully hashed before/after; all writes belong to the output folder.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import re
import resource
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __name__ == "__main__":
    for variable in ("OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[variable] = "2"

import numpy as np  # noqa: E402

from book_rag.experimental_ivfpq import (  # noqa: E402
    PQConfig,
    array_sha256,
    build_shards,
    faiss_backend,
    file_sha256,
    projected_capacity,
    search_shards,
    unique_passages,
)

MAX_DISK = 300 * 1024**2
MAX_RSS = 1024**3
RESERVE = int(4.5 * 1024**3)


def peak_rss() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def identity(path: Path) -> list[int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Source must be a regular nonsymlink file")
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def footprint(paths: list[Path]) -> dict[str, Any]:
    before = {str(path): identity(path) for path in paths}
    result = {str(path): {"identity": before[str(path)], "sha256": file_sha256(path)} for path in paths}
    if before != {str(path): identity(path) for path in paths}:
        raise ValueError("Source changed during integrity capture")
    return result


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".owned-tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def exact_candidates(
    vectors: np.ndarray, ids: np.ndarray, query: np.ndarray, query_passage: int, limit: int = 1000
) -> list[int]:
    scores = np.empty(len(vectors), dtype=np.float32)
    for start in range(0, len(vectors), 4096):
        end = min(start + 4096, len(vectors))
        scores[start:end] = vectors[start:end] @ query
    scores[ids == query_passage] = -np.inf
    order = np.lexsort((np.arange(len(vectors)), -scores))
    return unique_passages(order, ids, query_passage, limit)


def choose_query_rows(ids: np.ndarray, count: int) -> np.ndarray:
    seen: set[int] = set()
    selected: list[int] = []
    primary = np.linspace(0, len(ids) - 1, count, dtype=np.int64)
    fallback = np.linspace(0, len(ids) - 1, min(len(ids), count * 16), dtype=np.int64)
    candidates = np.concatenate((primary, fallback))
    for row in candidates:
        passage = int(ids[row])
        if passage not in seen:
            seen.add(passage)
            selected.append(int(row))
            if len(selected) == count:
                return np.array(selected, dtype=np.int64)
    raise ValueError("Not enough distinct passage IDs for query sample")


def run_probe(
    vectors: np.ndarray, ids: np.ndarray, output: Path, source: dict[str, Any], *, small: bool
) -> dict[str, Any]:
    config = (
        PQConfig(dimensions=vectors.shape[1], nlist=4, subquantizers=2, bits=4, iterations=3) if small else PQConfig()
    )
    query_count = 8 if small else 64
    query_rows = choose_query_rows(ids, query_count)
    queries = np.ascontiguousarray(vectors[query_rows])
    query_ids = ids[query_rows]
    eligible_training = np.flatnonzero(~np.isin(ids, query_ids))
    rng = np.random.default_rng(config.seed)
    training_rows = np.sort(rng.choice(eligible_training, min(32768, len(eligible_training)), replace=False))
    training = np.ascontiguousarray(vectors[training_rows])
    if np.isin(ids[training_rows], query_ids).any():
        raise AssertionError("Query passages leaked into codebook training")
    source = {**source, "passage_ids_sha256": array_sha256(ids), "training_rows_sha256": array_sha256(training_rows)}
    started = time.perf_counter()
    interrupted = build_shards(
        vectors, training, output, source, config=config, shard_rows=512 if small else 8192, stop_after_shards=1
    )
    first_seconds = time.perf_counter() - started
    started = time.perf_counter()
    build = build_shards(vectors, training, output, source, config=config, shard_rows=512 if small else 8192)
    resume_seconds = time.perf_counter() - started
    if not build["complete"] or build["resumed_shards"] < 1:
        raise AssertionError("Interrupt/resume demonstration failed")
    del training
    gc.collect()
    exact: list[list[int]] = []
    exact_seconds: list[float] = []
    for query, passage in zip(queries, query_ids, strict=True):
        started = time.perf_counter()
        exact.append(exact_candidates(vectors, ids, query, int(passage)))
        exact_seconds.append(time.perf_counter() - started)
    trials: list[dict[str, Any]] = []
    for nprobe in (1, 4) if small else (1, 8, 32, 128):
        started = time.perf_counter()
        scores, rows = search_shards(build, queries, nprobe=nprobe)
        search_seconds = time.perf_counter() - started
        pool = [
            unique_passages(np.asarray(rows[j], dtype=np.int64), ids, int(query_ids[j]), 1000)
            for j in range(len(queries))
        ]
        metrics = {}
        for target_k, candidate_k in ((10, 10), (100, 100), (10, 100), (100, 1000)):
            values = [
                len(set(target[:target_k]) & set(candidates[:candidate_k])) / min(target_k, len(target))
                for target, candidates in zip(exact, pool, strict=True)
            ]
            metrics[f"exact_top{target_k}_in_ann_top{candidate_k}"] = float(np.mean(values))
        errors = []
        for j in range(len(queries)):
            query = queries[j]
            query_passage = query_ids[j]
            query_scores = np.asarray(scores[j], dtype=np.float32)
            query_window_rows = np.asarray(rows[j], dtype=np.int64)
            valid = query_window_rows >= 0
            valid[valid] &= ids[query_window_rows[valid]] != query_passage
            valid_rows = query_window_rows[valid][:100]
            if len(valid_rows):
                errors.extend(np.abs(query_scores[valid][:100] - vectors[valid_rows] @ query).tolist())
        trials.append(
            {
                "nprobe": nprobe,
                "recall": metrics,
                "unique_candidate_count_min": min(map(len, pool)),
                "unique_candidate_count_median": float(np.median(list(map(len, pool)))),
                "top100_score_absolute_error_mean": float(np.mean(errors)),
                "top100_score_absolute_error_p95": float(np.percentile(errors, 95)),
                "batch_search_seconds": search_seconds,
                "batch_search_includes": (
                    "Sequential shard IO+SHA verification+FAISS search+stable window merge; excludes passage dedup"
                ),
                "queries_in_batch": len(queries),
            }
        )
    encoded_bytes = sum((Path(build["directory"]) / shard["file"]).stat().st_size for shard in build["shards"])
    return {
        "rows": len(vectors),
        "unique_passages": len(np.unique(ids)),
        "dimensions": vectors.shape[1],
        "query_count": len(queries),
        "query_rows_sha256": array_sha256(query_rows),
        "training_rows": len(training_rows),
        "query_passages_excluded_from_training_and_neighbors": True,
        "configuration": asdict(config),
        "candidate_window_pool": 4096,
        "candidate_unique_passage_cap": 1000,
        "build": build,
        "first_phase_seconds": first_seconds,
        "first_phase_created_shards": interrupted["created_shards"],
        "first_phase_resumed_shards": interrupted["resumed_shards"],
        "resume_phase_seconds": resume_seconds,
        "first_build_incomplete": not interrupted["complete"],
        "encoded_shards_bytes_including_repeated_codebooks": encoded_bytes,
        "source_float32_sample_bytes": vectors.nbytes,
        "exact_query_seconds_p50": float(np.median(exact_seconds)),
        "exact_query_seconds_p95": float(np.percentile(exact_seconds, 95)),
        "trials": trials,
        "limitations": [
            "Deterministic sampled corpus document vectors are not natural-language user query embeddings.",
            "ANN recall is against exact top passages within this sample, not the full1308 or a relevance evaluation.",
            "All windows sharing the query passage ID are excluded; other neighboring passages/editions may remain.",
            "Training excludes all query passages, but index and codebooks use other sample corpus vectors.",
            "Approximate IP scores have quantization error; existing semantic thresholds are not calibrated here.",
            "Batch ANN timing and sequential exact-query timing are different workloads, not a speedup ratio.",
            "Peak RSS includes imports, source mmap pages, training and integrity scans; not online resident usage.",
            "No live source binding, filters, production activation or embedding throughput are evaluated.",
        ],
    }


def worker(args: argparse.Namespace) -> None:
    os.environ["BOOKRAG_IVFPQ_WORKER"] = "1"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    code_paths = [Path(__file__).resolve(), Path(__file__).resolve().parents[1] / "src/book_rag/experimental_ivfpq.py"]
    code_before = footprint(code_paths)
    source: dict[str, Any]
    if args.self_test:
        rng = np.random.default_rng(20261115)
        vectors = rng.standard_normal((2048, 8), dtype=np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        ids = np.repeat(np.arange(1, 1025, dtype=np.int64), 2)
        source = {"kind": "owned-synthetic-CC0", "model_space": "random-normal-unit8", "source_db_opened": False}
        before = None
        paths: list[Path] = []
    else:
        source_dir = args.source_vectors.resolve()
        if output == source_dir or source_dir in output.parents or output in source_dir.parents:
            raise ValueError("Output must be separate from the source tree")
        active = source_dir / "active.json"
        metadata = json.loads(active.read_text())
        generation = metadata["generation"]
        if not re.fullmatch(r"gen-[0-9a-f]{64}-[0-9a-f]{12}", generation):
            raise ValueError("Unsupported source generation identity")
        folder = source_dir / generation
        paths = [
            active,
            folder / "metadata.json",
            folder / "checkpoint.json",
            folder / "embeddings.npy",
            folder / "ids.npy",
        ]
        before = footprint(paths)
        if json.loads(active.read_text()) != metadata:
            raise ValueError("Active metadata changed during integrity capture")
        matrix = np.load(folder / "embeddings.npy", mmap_mode="r", allow_pickle=False)
        source_ids = np.load(folder / "ids.npy", mmap_mode="r", allow_pickle=False)
        if matrix.dtype != np.float32 or source_ids.dtype != np.int64 or matrix.ndim != 2:
            raise ValueError("Unsupported immutable source arrays")
        if matrix.shape != (metadata["vectors"], metadata["dimensions"]) or len(source_ids) != len(matrix):
            raise ValueError("Source metadata/array shape mismatch")
        if matrix.flags.writeable or source_ids.flags.writeable or np.any(source_ids[:1] <= 0):
            raise ValueError("Expected read-only source arrays and positive passage IDs")
        rng = np.random.default_rng(20261115)
        source_rows = np.sort(rng.choice(len(matrix), min(args.rows, len(matrix)), replace=False))
        # Explicit bounded pread avoids faulting a large fraction of an mmap
        # into this process on macOS while gathering widely distributed rows.
        vectors = np.empty((len(source_rows), matrix.shape[1]), dtype=np.float32)
        ids = np.empty(len(source_rows), dtype=np.int64)
        with (folder / "embeddings.npy").open("rb") as vector_file, (folder / "ids.npy").open("rb") as id_file:
            width = matrix.shape[1] * 4
            for j, source_row in enumerate(source_rows):
                raw = os.pread(vector_file.fileno(), width, matrix.offset + int(source_row) * width)
                raw_id = os.pread(id_file.fileno(), 8, source_ids.offset + int(source_row) * 8)
                if len(raw) != width or len(raw_id) != 8:
                    raise ValueError("Truncated immutable source during bounded read")
                vectors[j] = np.frombuffer(raw, dtype=np.float32)
                ids[j] = np.frombuffer(raw_id, dtype=np.int64)[0]
        del matrix, source_ids
        gc.collect()
        np.save(output / "source_rows.npy", source_rows, allow_pickle=False)
        np.save(output / "sample_passage_ids.npy", ids, allow_pickle=False)
        source = {
            "kind": "readonly-frozen-vector-sample",
            "active_sha256": before[str(active)]["sha256"],
            "generation": generation,
            "source_vector_count": metadata["vectors"],
            "source_passage_count": metadata["passages"],
            "source_book_count_for_extrapolation": 1308,
            "model": metadata["model"],
            "model_revision": metadata["model_revision"],
            "embedding_profile": metadata["embedding_profile"],
            "strategy": metadata["strategy"],
            "corpus_fingerprint": metadata["corpus_fingerprint"],
            "arrays_sha256": {name: before[str(folder / name)]["sha256"] for name in ("embeddings.npy", "ids.npy")},
            "sample_source_rows_sha256": array_sha256(source_rows),
            "source_db_opened": False,
        }
    result = run_probe(vectors, ids, output, source, small=args.self_test)
    if before is not None:
        after = footprint(paths)
        if before != after:
            raise ValueError("Source bytes/identity changed: discard this run")
    else:
        after = None
    if code_before != footprint(code_paths):
        raise ValueError("Probe implementation changed during measurement; discard run")
    used = sum(path.stat().st_size for path in output.rglob("*") if path.is_file())
    if used > MAX_DISK or peak_rss() > MAX_RSS or shutil.disk_usage(output).free < RESERVE:
        failure = {
            "status": "budget-rejected",
            "owned_bytes": used,
            "peak_rss": peak_rss(),
            "free_bytes": shutil.disk_usage(output).free,
            "source_byte_identical": before == after,
        }
        write_json(output / "budget-failure.json", failure)
        raise ValueError("Experiment exceeded disk/RSS/reserve budget: " + json.dumps(failure))
    capacity = (
        []
        if args.self_test
        else [
            {
                "editions": count,
                "assumption": "Linear1308mean-window-density; not an actual11k count",
                **projected_capacity(round(source["source_vector_count"] * count / 1308)),
            }
            for count in (11000, 50000)
        ]
    )
    report = {
        "schema_version": 1,
        "status": "experimental-only-complete",
        "created_at": datetime.now(UTC).isoformat(),
        "source": source,
        "source_before": before,
        "source_after": after,
        "source_byte_identical": before == after,
        "faiss_version": faiss_backend().__version__,
        "faiss_license": "MIT",
        "implementation": code_before,
        "platform": platform.platform(),
        "process_peak_rss_bytes": peak_rss(),
        "owned_output_bytes": used,
        "limits": {"disk_bytes": MAX_DISK, "rss_bytes": MAX_RSS, "free_reserve_bytes": RESERVE},
        "free_bytes_at_end": shutil.disk_usage(output).free,
        "capacity_projections": capacity,
        "result": result,
    }
    write_json(output / "report.json", report)
    print(json.dumps({"report": str(output / "report.json"), "peak_rss": peak_rss(), "owned_bytes": used}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-vectors", type=Path, default=Path("data/snapshots/library1308-v1/vectors"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=65536)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Resume an incomplete owned output without a report")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 16384 <= args.rows <= 65536:
        parser.error("Rows must be16384..65536")
    if args.worker:
        worker(args)
        return
    if args.output.exists() and any(args.output.iterdir()):
        if not args.resume or (args.output / "report.json").exists():
            parser.error("Choose a new folder, or --resume an incomplete owned run without a report")
    args.output.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.output).free < RESERVE + MAX_DISK:
        parser.error("Not enough free space for4.5GiB reserve plus300MiB experiment budget")
    command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = time.monotonic()
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                check = subprocess.run(
                    ["ps", "-o", "rss=", "-p", str(process.pid)], capture_output=True, text=True, check=False
                )
                rss = int(check.stdout.strip() or "0") * 1024
                if rss > MAX_RSS or time.monotonic() - started > 900:
                    raise RuntimeError("Owned worker exceeded1GiB RSS or900second deadline") from None
                if shutil.disk_usage(args.output).free < RESERVE:
                    raise RuntimeError("Concurrent disk consumption crossed4.5GiB reserve") from None
    except BaseException:
        process.kill()
        process.wait()
        raise
    if stderr:
        print(stderr, file=sys.stderr)
    print(stdout.strip())
    if process.returncode:
        raise SystemExit(process.returncode)


if __name__ == "__main__":
    main()
