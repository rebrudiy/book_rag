"""Measure vector-storage approximation, not relevance, without changing any index.

Reads an existing generation with mmap_mode='r'. Writes experimental encodings and
metrics under a new owned output directory; no Library, SQLite, models or MPS.
Each measurement runs sequentially in a fresh one-BLAS-thread CPU subprocess.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Apply before importing NumPy, in this script's process tree only. Shared-host
# measurements must not consume the BLAS cores used by the ongoing model build.
THREAD_VARIABLES = (
    "OPENBLAS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS",
)
for thread_variable in THREAD_VARIABLES:
    os.environ[thread_variable] = "1"

import numpy as np  # noqa: E402

FORMATS = ("float32", "float16", "int8_row_symmetric")
TOP_K = (10, 100)


def sha256(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            checksum.update(block)
    return checksum.hexdigest()


def peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def symmetric_codes(rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    maximum = np.max(np.abs(rows), axis=1)
    scales = np.where(maximum > 0, maximum / np.float32(127), np.float32(1)).astype(np.float32)
    scaled = rows / scales[:, None]
    np.rint(scaled, out=scaled)
    np.clip(scaled, -127, 127, out=scaled)
    return scaled.astype(np.int8), scales


class Store:
    """Decode bounded blocks; cosine uses fp32 accumulation and decoded row norms."""

    def __init__(self, job: dict[str, Any], variant: str) -> None:
        self.block_rows = job["block_rows"]
        self.ids = np.load(job["ids"], mmap_mode="r")
        self.unique_ids, self.inverse = np.unique(self.ids, return_inverse=True)
        paths = job["formats"][variant]
        self.rows = np.load(paths["vectors"], mmap_mode="r")
        self.scales = np.load(paths["scales"], mmap_mode="r") if "scales" in paths else None
        self.norms = np.empty(len(self.rows), dtype=np.float32)
        for start in range(0, len(self.rows), self.block_rows):
            block = self.decode(start)
            self.norms[start : start + len(block)] = np.linalg.norm(block, axis=1)
        self.zero = self.norms == 0
        self.norms[self.zero] = np.inf

    def decode(self, start: int) -> np.ndarray:
        end = min(start + self.block_rows, len(self.rows))
        block = np.asarray(self.rows[start:end], dtype=np.float32)
        if self.scales is not None:
            # Explicit fp32 reconstruction avoids int8 overflow; scales are part
            # of the stored encoding even though positive row scales cancel in cosine.
            block *= self.scales[start:end, None]
        return block

    def scores(self, query: np.ndarray, query_passage: int) -> np.ndarray:
        scores = np.empty(len(self.rows), dtype=np.float32)
        for start in range(0, len(self.rows), self.block_rows):
            block = self.decode(start)
            end = start + len(block)
            scores[start:end] = (block @ query) / self.norms[start:end]
        scores[self.zero | (self.ids == query_passage)] = -np.inf
        return scores

    def aggregate(self, scores: np.ndarray) -> np.ndarray:
        passage_scores = np.full(len(self.unique_ids), -np.inf, dtype=np.float32)
        np.maximum.at(passage_scores, self.inverse, scores)
        return passage_scores

    def ranking(self, passage_scores: np.ndarray) -> np.ndarray:
        eligible = np.flatnonzero(np.isfinite(passage_scores))
        order = np.lexsort((self.unique_ids[eligible], -passage_scores[eligible]))
        return eligible[order[: max(TOP_K)]]


def prepare(job: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    source = np.load(job["source_vectors"], mmap_mode="r")
    ids = np.load(job["ids"], mmap_mode="r")
    if source.dtype != np.float32 or source.ndim != 2 or ids.ndim != 1 or len(ids) != len(source):
        raise ValueError("Expected aligned float32 vectors and one passage ID per row")
    if source.shape != tuple(job["expected_shape"]):
        raise ValueError("Active metadata shape does not match the vector array")
    if not np.issubdtype(ids.dtype, np.integer) or np.any(ids < 1):
        raise ValueError("Passage IDs must be positive integers")
    _, first_rows = np.unique(ids, return_index=True)
    count = min(job["queries"], len(first_rows))
    if count < 1 or len(first_rows) < 2:
        raise ValueError("At least two distinct passages and one query are required")
    selected = np.sort(np.random.default_rng(job["seed"]).choice(first_rows, count, replace=False))
    queries = np.asarray(source[selected], dtype=np.float32)
    norms = np.linalg.norm(queries, axis=1)
    if not np.isfinite(queries).all() or np.any(norms == 0):
        raise ValueError("Sampled query contains a nonfinite or zero vector")
    queries /= norms[:, None]
    output = Path(job["output"])
    np.save(output / "queries.npy", queries, allow_pickle=False)
    np.save(output / "query_passage_ids.npy", np.asarray(ids[selected]), allow_pickle=False)
    np.save(output / "query_window_rows.npy", selected, allow_pickle=False)
    half = np.lib.format.open_memmap(
        job["formats"]["float16"]["vectors"], mode="w+", dtype=np.float16, shape=source.shape
    )
    integer = np.lib.format.open_memmap(
        job["formats"]["int8_row_symmetric"]["vectors"], mode="w+", dtype=np.int8, shape=source.shape
    )
    scales = np.lib.format.open_memmap(
        job["formats"]["int8_row_symmetric"]["scales"], mode="w+", dtype=np.float32, shape=(len(source),)
    )
    zero_rows = 0
    norm_error = 0.0
    for start in range(0, len(source), job["block_rows"]):
        block = np.asarray(source[start : start + job["block_rows"]])
        if not np.isfinite(block).all():
            raise ValueError("Source contains nonfinite vectors")
        row_norms = np.linalg.norm(block, axis=1)
        zero_rows += int(np.count_nonzero(row_norms == 0))
        norm_error = max(norm_error, float(np.max(np.abs(row_norms - 1))))
        half[start : start + len(block)] = block
        codes, row_scales = symmetric_codes(block)
        integer[start : start + len(block)] = codes
        scales[start : start + len(block)] = row_scales
    half.flush()
    integer.flush()
    scales.flush()
    return {
        "queries": count,
        "distinct_passages": len(first_rows),
        "source_zero_rows": zero_rows,
        "source_max_norm_deviation_from_one": norm_error,
        "seconds": time.perf_counter() - started,
        "peak_rss_bytes": peak_rss_bytes(),
    }


def performance(job: dict[str, Any], variant: str) -> dict[str, Any]:
    baseline_rss = peak_rss_bytes()
    started = time.perf_counter()
    store = Store(job, variant)
    queries = np.load(Path(job["output"]) / "queries.npy", mmap_mode="r")
    query_ids = np.load(Path(job["output"]) / "query_passage_ids.npy", mmap_mode="r")
    startup = time.perf_counter() - started
    # Initialization plus this full scan deliberately warm the mapped index.
    store.ranking(store.aggregate(store.scores(queries[0], int(query_ids[0]))))
    latencies = []
    for query, passage_id in zip(queries, query_ids, strict=True):
        began = time.perf_counter()
        ranking = store.ranking(store.aggregate(store.scores(query, int(passage_id))))
        if np.any(store.unique_ids[ranking] == passage_id):
            raise AssertionError("Self-passage leaked into candidates")
        latencies.append((time.perf_counter() - began) * 1000)
    peak = peak_rss_bytes()
    return {
        "queries": len(queries),
        "startup_seconds": startup,
        "latency_p50_ms": float(np.median(latencies)),
        "latency_p95_ms": float(np.percentile(latencies, 95)),
        "latency_mean_ms": float(np.mean(latencies)),
        "peak_rss_bytes": peak,
        "import_peak_rss_bytes": baseline_rss,
        "peak_minus_import_baseline_bytes": max(0, peak - baseline_rss),
        "zero_rows": int(store.zero.sum()),
    }


def accuracy(job: dict[str, Any], variant: str) -> dict[str, Any]:
    store = Store(job, variant)
    output = Path(job["output"])
    queries = np.load(output / "queries.npy", mmap_mode="r")
    query_ids = np.load(output / "query_passage_ids.npy", mmap_mode="r")
    is_reference = variant == "float32"
    reference = (
        np.lib.format.open_memmap(
            output / "reference_scores.npy", mode="w+", dtype=np.float32, shape=(len(queries), len(store.rows))
        )
        if is_reference
        else np.load(output / "reference_scores.npy", mmap_mode="r")
    )
    recalls: dict[int, list[float]] = {k: [] for k in TOP_K}
    tie_aware: dict[int, list[float]] = {k: [] for k in TOP_K}
    boundary_ties = dict.fromkeys(TOP_K, 0)
    maximum_error = absolute_error_sum = 0.0
    compared_pairs = 0
    fp64_sanity_max_error = 0.0
    for index, (query, passage_id) in enumerate(zip(queries, query_ids, strict=True)):
        scores = store.scores(query, int(passage_id))
        if is_reference:
            reference[index] = scores
            eligible = np.flatnonzero(np.isfinite(scores))
            row = int(eligible[index % len(eligible)])
            vector64 = np.asarray(store.rows[row], dtype=np.float64)
            query64 = np.asarray(query, dtype=np.float64)
            exact = np.dot(vector64, query64) / (np.linalg.norm(vector64) * np.linalg.norm(query64))
            fp64_sanity_max_error = max(fp64_sanity_max_error, abs(float(scores[row]) - float(exact)))
        actual_passages = store.aggregate(scores)
        reference_scores = np.asarray(reference[index])
        reference_passages = store.aggregate(reference_scores)
        actual_order = store.ranking(actual_passages)
        reference_order = store.ranking(reference_passages)
        valid = np.isfinite(reference_scores) & np.isfinite(scores)
        if np.any(np.isfinite(reference_scores) != np.isfinite(scores)):
            raise AssertionError("Encoding changed candidate eligibility")
        errors = np.abs(scores[valid] - reference_scores[valid])
        maximum_error = max(maximum_error, float(np.max(errors)))
        absolute_error_sum += float(errors.sum(dtype=np.float64))
        compared_pairs += len(errors)
        for k in TOP_K:
            expected = reference_order[:k]
            found = actual_order[:k]
            denominator = len(expected)
            recalls[k].append(len(np.intersect1d(found, expected)) / denominator)
            floor = float(reference_passages[expected[-1]])
            tie_aware[k].append(float(np.count_nonzero(reference_passages[found] >= floor - 1e-7)) / denominator)
            boundary_ties[k] += int(np.count_nonzero(reference_passages >= floor - 1e-7) > denominator)
    if is_reference:
        reference.flush()
        if fp64_sanity_max_error > 2e-6:
            raise AssertionError("Float32 cosine disagrees with independent float64 sanity pairs")
    return {
        "recall": {
            str(k): {
                "mean": float(np.mean(recalls[k])),
                "minimum": float(np.min(recalls[k])),
                "tie_aware_mean_tolerance_1e-7": float(np.mean(tie_aware[k])),
                "reference_boundary_tie_queries": boundary_ties[k],
            }
            for k in TOP_K
        },
        "max_abs_cosine_error": maximum_error,
        "mean_abs_cosine_error": absolute_error_sum / compared_pairs,
        "compared_query_window_pairs": compared_pairs,
        "fp64_sanity_max_error": fp64_sanity_max_error if is_reference else None,
        "accuracy_worker_peak_rss_bytes_not_query_memory": peak_rss_bytes(),
    }


def run_worker(job_path: Path, operation: str, variant: str = "float32") -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            operation,
            "--job",
            str(job_path),
            "--variant",
            variant,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def self_test() -> None:
    rows = np.array([[0, 0, 0], [0.1, -0.2, 0.4], [-0.1, 0.8, 0.02]], dtype=np.float32)
    codes, scales = symmetric_codes(rows)
    assert codes.dtype == np.int8 and scales.dtype == np.float32
    assert codes.min() >= -127 and codes.max() <= 127 and scales[0] == 1
    assert np.all(np.abs(codes.astype(np.float32) * scales[:, None] - rows) <= scales[:, None] / 2 + 1e-7)
    assert np.array_equal(codes[0], np.zeros(3, dtype=np.int8))
    with tempfile.TemporaryDirectory(prefix="book-atlas-quant-math-") as temporary:
        folder = Path(temporary)
        vectors = np.array([[1, 0, 0], [0.9, 0.1, 0], [0, 1, 0], [0, -1, 0], [0, 0, 0]], dtype=np.float32)
        np.save(folder / "rows.npy", vectors)
        np.save(folder / "ids.npy", np.array([1, 1, 2, 3, 4]))
        job = {
            "ids": str(folder / "ids.npy"),
            "block_rows": 2,
            "formats": {"float32": {"vectors": str(folder / "rows.npy")}},
        }
        store = Store(job, "float32")
        scores = store.scores(np.array([1, 0, 0], dtype=np.float32), 1)
        assert np.all(np.isneginf(scores[[0, 1, 4]])), "Exclude all overlapping/self-passage and zero rows"
        ranked = store.ranking(store.aggregate(scores))
        assert np.array_equal(store.unique_ids[ranked], [2, 3]), "Tie break uses passage ID"
        assert not store.rows.flags.writeable, "Source array must stay read-only"


def benchmark(args: argparse.Namespace) -> dict[str, Any]:
    source = args.source_vectors.resolve()
    active = source / "active.json"
    metadata = json.loads(active.read_text())
    generation = metadata["generation"]
    if Path(generation).name != generation:
        raise ValueError("Generation must be one direct child of the source vector directory")
    vectors = source / generation / "embeddings.npy"
    ids = source / generation / "ids.npy"
    output = args.output_dir.resolve()
    report_path = args.report.resolve() if args.report else output / "report.json"
    protected = source.parent
    if protected == output or protected in output.parents or output in protected.parents:
        raise ValueError("Benchmark output must be outside the source library/snapshot")
    if report_path == protected or protected in report_path.parents:
        raise ValueError("Report must be outside the source library/snapshot")
    if output.exists() or report_path.exists():
        raise FileExistsError("Use a new output directory/report; existing experiments are never overwritten")
    if not 1 <= args.queries <= 1000 or not 1 <= args.block_rows <= 16384:
        raise ValueError("Expected 1..1000 queries and 1..16384 block rows")
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output.parent).free < 2 * 1024**3:
        raise ValueError("Keep at least 2 GiB free before starting this bounded experiment")
    output.mkdir()
    before = {path.name: sha256(path) for path in (active, vectors, ids)}
    job = {
        "source_vectors": str(vectors),
        "ids": str(ids),
        "output": str(output),
        "queries": args.queries,
        "seed": args.seed,
        "block_rows": args.block_rows,
        "expected_shape": [metadata["vectors"], metadata["dimensions"]],
        "formats": {
            "float32": {"vectors": str(vectors)},
            "float16": {"vectors": str(output / "float16.npy")},
            "int8_row_symmetric": {"vectors": str(output / "int8.npy"), "scales": str(output / "scales.npy")},
        },
    }
    job_path = output / "job.json"
    write_json(job_path, job)
    print("Preparing bounded experimental arrays; source is read-only", file=sys.stderr, flush=True)
    preparation = run_worker(job_path, "prepare")
    results: dict[str, Any] = {}
    for variant in FORMATS:
        print(f"Measuring {variant}: isolated CPU latency/RSS, then approximation", file=sys.stderr, flush=True)
        timing = run_worker(job_path, "performance", variant)
        quality = run_worker(job_path, "accuracy", variant)
        files = {name: Path(path).stat().st_size for name, path in job["formats"][variant].items()}
        files["passage_ids"] = ids.stat().st_size
        results[variant] = {
            "storage_file_bytes": sum(files.values()),
            "storage_files": files,
            "performance": timing,
            "approximation": quality,
        }
    after = {path.name: sha256(path) for path in (active, vectors, ids)}
    if before != after:
        raise RuntimeError("Source changed during experiment; do not publish the results")
    baseline_bytes = results["float32"]["storage_file_bytes"]
    for result in results.values():
        result["storage_ratio_vs_float32"] = result["storage_file_bytes"] / baseline_bytes
        result["storage_reduction_pct"] = 100 * (1 - result["storage_ratio_vs_float32"])
    summary = {
        "experiment": "vector-storage-v1",
        "kind": "retrieval approximation, not relevance evaluation",
        "source": {
            "directory": str(args.source_vectors),
            "generation": generation,
            "model": metadata["model"],
            "model_revision": metadata.get("model_revision"),
            "corpus_fingerprint": metadata["corpus_fingerprint"],
            "strategy": metadata["strategy"],
            "vectors": metadata["vectors"],
            "dimensions": metadata["dimensions"],
            "file_sha256_before_and_after": before,
            "source_unchanged": True,
        },
        "method": {
            "query_count": preparation["queries"],
            "seed": args.seed,
            "block_rows": args.block_rows,
            "sampling": "uniform distinct passage IDs; first window per selected passage; sorted row order",
            "queries": "unquantized corpus-derived fp32 unit vectors; identical for every encoding",
            "candidate_exclusion": "all windows sharing the query passage ID, including overlapping windows",
            "ranking": "max cosine across windows per distinct passage; score descending then passage ID",
            "cosine": "fp32 decoded rows dot unit fp32 query, divided by decoded fp32 row norms",
            "int8": "symmetric row scale=max(abs(row))/127, zero scale=1, rint, clip[-127,127], fp32 scales",
            "latency": "warm-cache sequential full scan, decode, cosine, passage aggregation and top100 sort",
            "rss": "fresh performance subprocess per encoding; includes NumPy/startup/resident mmap pages",
            "accuracy_worker_memory_excluded": "reference-score mmap resides only in separate accuracy workers",
            "cold_io_measured": False,
            "ann_used": False,
            "production_changed": False,
            "query_rows_sha256": sha256(output / "query_window_rows.npy"),
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "numpy": np.__version__,
            "blas_thread_env": {key: os.environ[key] for key in THREAD_VARIABLES},
            "shared_host": True,
        },
        "preparation": preparation,
        "results": results,
        "limitations": [
            "Corpus-vector queries are not natural user questions or a relevance test.",
            "Nearby passages, translations and duplicate editions beyond the same passage may remain.",
            "No threshold, hybrid fusion, citation verification or end-to-end API latency is evaluated.",
            "Linear scanning cost remains proportional to window count; compression is not ANN.",
            "RSS and latency include this NumPy implementation and shared-host noise, not a backend guarantee.",
            "A 20-edition snapshot does not establish capacity or quality for 10,000–50,000 works.",
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(report_path, summary)
    if report_path != output / "report.json":
        write_json(output / "report.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-vectors", type=Path, default=Path("data/snapshots/curated20-v3/vectors"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/benchmarks/vector-storage-v1"))
    parser.add_argument("--report", type=Path, help="Optional separate small report; default OUTPUT_DIR/report.json")
    parser.add_argument("--queries", type=int, default=256)
    parser.add_argument("--seed", type=int, default=20261114)
    parser.add_argument("--block-rows", type=int, default=4096)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--worker", choices=("prepare", "performance", "accuracy"), help=argparse.SUPPRESS)
    parser.add_argument("--job", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--variant", choices=FORMATS, default="float32", help=argparse.SUPPRESS)
    args = parser.parse_args()
    self_test()
    if args.self_test:
        print(json.dumps({"self_test": "passed"}))
    elif args.worker:
        if args.job is None:
            parser.error("Internal worker requires --job")
        job = json.loads(args.job.read_text())
        result = (
            prepare(job)
            if args.worker == "prepare"
            else performance(job, args.variant)
            if args.worker == "performance"
            else accuracy(job, args.variant)
        )
        print(json.dumps(result))
    else:
        print(json.dumps(benchmark(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
