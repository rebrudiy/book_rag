"""Read-only numeric/latency proof for scoped scoring, never a relevance evaluation.

Uses a stored float32 query row, immutable SQLite metadata, read-only vector
memmaps and fresh single-BLAS-thread CPU workers. No Library constructor,
models, corpus text, live database checkpoint/migration or final-test data.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import platform
import re
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

THREAD_VARIABLES = ("OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
for variable in THREAD_VARIABLES:
    os.environ[variable] = "1"

import numpy as np  # noqa: E402

from book_rag.semantic import _candidate_passages, _score_vectors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DATABASE_SHA = "2d77675ba2a68f63e25f507355272e3206db8f9abbe959f326cea96f774939f0"


def sha(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            checksum.update(block)
    return checksum.hexdigest()


def identity(path: Path) -> list[int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected nonsymlink immutable input")
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def peak_rss() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    temporary.replace(path)


def worker(job_path: Path, method: str) -> None:
    job = json.loads(job_path.read_text())
    if hashlib.sha256(inspect.getsource(_score_vectors).encode()).hexdigest() != job["scorer_sha256"]:
        raise ValueError("Scorer changed after the experiment began")
    vectors = np.load(job["vectors"], mmap_mode="r", allow_pickle=False)
    ids = np.load(job["ids"], mmap_mode="r", allow_pickle=False)
    eligible = np.load(job["eligible"], mmap_mode="r", allow_pickle=False)
    query = np.load(job["query"], allow_pickle=False)
    before = peak_rss()
    started = time.perf_counter()
    cpu = time.process_time()
    if method == "full_dot_mask":
        scores = vectors @ query
        scores[~np.isin(ids, eligible)] = -np.inf
    elif method == "scoped":
        scores = _score_vectors(vectors, ids, query, eligible, ordered_ids=True)
    else:
        raise ValueError("Unknown measurement method")
    elapsed = time.perf_counter() - started
    cpu_elapsed = time.process_time() - cpu
    score_peak = peak_rss()
    if np.isnan(scores).any() or np.isposinf(scores).any():
        raise ValueError("Invalid score output")
    np.save(job[method + "_scores"], scores, allow_pickle=False)
    write(
        Path(job[method + "_result"]),
        {
            "method": method,
            "seconds": round(elapsed, 6),
            "cpu_seconds": round(cpu_elapsed, 6),
            "peak_rss_before_scoring_bytes": before,
            "peak_rss_through_scoring_bytes": score_peak,
            "peak_rss_after_saving_bytes": peak_rss(),
            "scores_bytes": int(scores.nbytes),
            "score_dtype": str(scores.dtype),
        },
    )


def run(args: Any) -> dict[str, Any]:
    source = args.source.resolve(strict=True)
    database = source / "library.sqlite3"
    wal = database.with_name(database.name + "-wal")
    if wal.is_symlink() or (wal.exists() and wal.stat().st_size):
        raise ValueError("Immutable source has a nonempty WAL")
    active = source / "vectors/active.json"
    metadata = json.loads(active.read_text())
    generation = metadata["generation"]
    if not re.fullmatch(r"gen-[A-Za-z0-9_.-]+", generation):
        raise ValueError("Expected a completed local generation")
    folder = active.parent / generation
    if folder.is_symlink():
        raise ValueError("Generation must be a nonsymlink directory")
    inputs = [database, active, folder / "embeddings.npy", folder / "ids.npy"]
    inputs += [
        folder / name
        for name in ("metadata.json", "checkpoint.json", "vector-reuse.json", "window-keys.sqlite3")
        if (folder / name).exists()
    ]
    before = {str(path.relative_to(source)): {"identity": identity(path), "sha256": sha(path)} for path in inputs}
    if before["library.sqlite3"]["sha256"] != args.expected_sha:
        raise ValueError("Frozen database SHA differs from the declared source")
    vectors = np.load(folder / "embeddings.npy", mmap_mode="r", allow_pickle=False)
    ids = np.load(folder / "ids.npy", mmap_mode="r", allow_pickle=False)
    if vectors.dtype != np.float32 or ids.dtype != np.int64 or vectors.ndim != 2 or ids.ndim != 1:
        raise ValueError("Expected float32 matrix/int64 ID vector")
    if vectors.shape != (metadata["vectors"], metadata["dimensions"]) or len(ids) != len(vectors):
        raise ValueError("Active dimensions/counts do not match aligned arrays")
    for array, path in ((vectors, folder / "embeddings.npy"), (ids, folder / "ids.npy")):
        if array.flags.writeable or path.stat().st_size != array.offset + array.nbytes:
            raise ValueError("Expected exact-length read-only NumPy arrays")
    for start in range(0, len(ids), 65536):
        block = ids[max(0, start - 1) : start + 65536]
        if np.any(block <= 0) or np.any(block[1:] < block[:-1]):
            raise ValueError("ordered_ids=True requires positive ascending passage IDs")
    if args.output.resolve().is_relative_to(source) or args.report.resolve().is_relative_to(source):
        raise ValueError("Outputs must stay outside the frozen source")
    args.output.mkdir(parents=True, exist_ok=False)
    if args.report.exists():
        raise FileExistsError("Use a new versioned public report path")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    deadline = time.monotonic() + 30
    scopes: list[dict[str, Any]] = []
    try:
        count = connection.execute("SELECT COUNT(*) FROM books WHERE status='indexed'").fetchone()[0]
        if count != args.expected_books:
            raise ValueError("Frozen edition count differs from declared fixture")
        for kind, value in [("book_id", book) for book in args.books] + [("language", args.language)]:
            rows = connection.execute(
                "SELECT p.id FROM passages p JOIN books b ON b.id=p.book_id "
                "JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
                f"WHERE b.status='indexed' AND b.{'id' if kind == 'book_id' else 'language'}=? ORDER BY p.id",
                (value,),
            )
            eligible = np.fromiter((row[0] for row in rows), dtype=np.int64)
            if not len(eligible):
                raise ValueError("Selected scope has no indexed passages")
            scopes.append({"kind": kind, "value": value, "eligible": eligible})
    finally:
        connection.close()
    query_row = int(np.searchsorted(ids, scopes[0]["eligible"][len(scopes[0]["eligible"]) // 2]))
    if query_row >= len(ids) or ids[query_row] != scopes[0]["eligible"][len(scopes[0]["eligible"]) // 2]:
        raise ValueError("Selected query passage is absent from source generation")
    query = np.array(vectors[query_row], copy=True)
    if not np.isfinite(query).all() or np.linalg.norm(query) == 0:
        raise ValueError("Query row is nonfinite or zero")
    query_path = args.output / "query.npy"
    np.save(query_path, query, allow_pickle=False)
    scorer_sha = hashlib.sha256(inspect.getsource(_score_vectors).encode()).hexdigest()
    report: dict[str, Any] = {
        "kind": "scoped_vector_numeric_and_latency_proof_not_relevance",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "source_name": source.name,
        "source_guards_passed": False,
        "generation": generation,
        "database_sha256": args.expected_sha,
        "script_sha256": sha(Path(__file__)),
        "scorer_sha256": scorer_sha,
        "source_inputs_before": before,
        "array_shape": list(vectors.shape),
        "array_bytes": int(vectors.nbytes),
        "indexed_editions": count,
        "ordered_positive_ids_checked": True,
        "query": {
            "kind": "stored_matrix_row",
            "window_row": query_row,
            "passage_id": int(ids[query_row]),
            "sha256": hashlib.sha256(query.tobytes()).hexdigest(),
            "self_not_excluded": True,
        },
        "cases": [],
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "platform": platform.platform(),
            "blas_thread_environment": {name: os.environ[name] for name in THREAD_VARIABLES},
        },
    }
    started = time.monotonic()
    for index, scope in enumerate(scopes):
        eligible = scope.pop("eligible")
        eligible_path = args.output / f"eligible-{index}.npy"
        np.save(eligible_path, eligible, allow_pickle=False)
        case = {**scope, "eligible_passages": len(eligible), "measurements": {}}
        job = {
            "vectors": str(folder / "embeddings.npy"),
            "ids": str(folder / "ids.npy"),
            "eligible": str(eligible_path.resolve()),
            "query": str(query_path.resolve()),
            "scorer_sha256": scorer_sha,
        }
        for method in ("full_dot_mask", "scoped"):
            job[method + "_scores"] = str((args.output / f"{index}-{method}-scores.npy").resolve())
            job[method + "_result"] = str((args.output / f"{index}-{method}.json").resolve())
        job_path = args.output / f"job-{index}.json"
        write(job_path, job)
        for method in ("full_dot_mask", "scoped"):
            print(json.dumps({"scope": scope, "method": method, "status": "starting"}), flush=True)
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(job_path.resolve()),
                    "--method",
                    method,
                ],
                check=True,
                timeout=60,
            )
            case["measurements"][method] = json.loads(Path(job[method + "_result"]).read_text())
        old = np.load(job["full_dot_mask_scores"], mmap_mode="r", allow_pickle=False)
        new = np.load(job["scoped_scores"], mmap_mode="r", allow_pickle=False)
        finite = np.isfinite(old)
        if not np.array_equal(finite, np.isfinite(new)) or np.isnan(old).any() or np.isnan(new).any():
            raise AssertionError("Finite/nonfinite score membership changed")
        difference = np.abs(old[finite].astype(np.float64) - new[finite])
        old_ids, _, _ = _candidate_passages(old, ids, 0.30)
        new_ids, _, _ = _candidate_passages(new, ids, 0.30)
        case.update(
            scored_windows=int(finite.sum()),
            bit_exact_scores=bool(np.array_equal(old, new)),
            allclose_scores=bool(np.allclose(old, new, rtol=1e-6, atol=1e-7)),
            max_absolute_score_error=float(difference.max(initial=0)),
            nonidentical_finite_scores=int(np.count_nonzero(difference)),
            outside_scope_negative_infinity_preserved=bool(np.isneginf(new[~finite]).all()),
            candidate_threshold=0.30,
            candidate_ids_equal=old_ids == new_ids,
            old_candidate_ids=old_ids,
            new_candidate_ids=new_ids,
        )
        report["cases"].append(case)
    after = {str(path.relative_to(source)): {"identity": identity(path), "sha256": sha(path)} for path in inputs}
    if after != before or (wal.exists() and wal.stat().st_size):
        raise RuntimeError("Original database/artifact identity or bytes changed")
    if scorer_sha != hashlib.sha256(inspect.getsource(_score_vectors).encode()).hexdigest():
        raise RuntimeError("Scorer changed during experiment")
    report.update(
        status="completed",
        source_guards_passed=True,
        source_inputs_after=after,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    report["limitations"] = [
        "One stored corpus vector, not a user query, relevance metric or heldout evaluation; "
        "its own window remains eligible.",
        "Single full-dot-then-scoped pair per scope in fresh sequential CPU workers; "
        "hashes and first pass warm file caches.",
        "One BLAS thread requested through process-local environment; concurrent ingestion/model work is not isolated.",
        "ru_maxrss is per-worker high-water RSS, includes mapped resident pages and imports; "
        "it is not allocation or total host memory.",
        "Scoring timings include mask/gather/score allocation, exclude startup, SQL, source hashing, "
        "ranking and output serialization.",
        "Tiny floating-point differences are reported separately; allclose does not prove identical order "
        "for every query or threshold boundary.",
        "No source text, Library constructor, source write, live database, model or sealed case was used.",
    ]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    write(args.report, report)
    return report


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="scoped-vector-cc0-") as temporary:
        directory = Path(temporary)
        source = directory / "source"
        generation = source / "vectors/gen-fixture"
        generation.mkdir(parents=True)
        with sqlite3.connect(source / "library.sqlite3") as connection:
            connection.executescript(
                "CREATE TABLE books(id TEXT,language TEXT,status TEXT);"
                "CREATE TABLE passages(id INTEGER,book_id TEXT,section_id INTEGER);"
                "CREATE TABLE sections(id INTEGER,book_id TEXT);"
            )
            connection.executemany("INSERT INTO books VALUES(?,?,'indexed')", [("one", "en"), ("two", "de")])
            connection.executemany(
                "INSERT INTO passages VALUES(?,?,?)", [(1, "one", 1), (2, "one", 1), (3, "two", 2), (4, "two", 2)]
            )
            connection.executemany("INSERT INTO sections VALUES(?,?)", [(1, "one"), (2, "two")])
        vectors = np.array(
            [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1], [1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float32
        )
        np.save(generation / "embeddings.npy", vectors)
        np.save(generation / "ids.npy", np.array([1, 1, 2, 3, 3, 4], dtype=np.int64))
        write(source / "vectors/active.json", {"generation": "gen-fixture", "vectors": 6, "dimensions": 4})
        report = run(
            SimpleNamespace(
                source=source,
                output=directory / "output",
                report=directory / "report.json",
                expected_sha=sha(source / "library.sqlite3"),
                expected_books=2,
                books=["one", "two"],
                language="de",
            )
        )
        assert report["source_guards_passed"] and all(
            case["bit_exact_scores"] and case["candidate_ids_equal"] for case in report["cases"]
        )
        print("Owned tiny numeric/guard/worker self-test passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/snapshots/library1308-v1")
    parser.add_argument("--expected-sha", default=EXPECTED_DATABASE_SHA)
    parser.add_argument("--expected-books", type=int, default=1308)
    parser.add_argument("--book", dest="books", action="append")
    parser.add_argument("--language", default="de")
    parser.add_argument("--output", type=Path, default=ROOT / "data/bench-scoped-vector-v1")
    parser.add_argument("--report", type=Path, default=ROOT / "experiments/scoped-vector-v1.json")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--method", choices=("full_dot_mask", "scoped"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.worker:
        if not args.method:
            parser.error("A worker method is required")
        worker(args.worker, args.method)
    elif args.self_test:
        self_test()
    else:
        args.books = args.books or ["pg2680-full", "pg1228-full"]
        if len(args.books) != 2 or len(set(args.books)) != 2:
            parser.error("Exactly two distinct book scopes are required")
        report = run(args)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "source_guards_passed": report["source_guards_passed"],
                    "cases": [
                        {
                            key: case[key]
                            for key in (
                                "kind",
                                "value",
                                "bit_exact_scores",
                                "allclose_scores",
                                "candidate_ids_equal",
                                "max_absolute_score_error",
                            )
                        }
                        for case in report["cases"]
                    ],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
