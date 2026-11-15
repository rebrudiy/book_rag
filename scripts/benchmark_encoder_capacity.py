"""Offline MiniLM throughput, one Torch-only subprocess per CPU/MPS device.

Run only when other accelerator jobs are idle. No FAISS import, Library
construction, network/model download, final labels or production-index writes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import resource
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
RESERVE = int(4.5 * 1024**3)
MAX_RSS = 2 * 1024**3


def sha(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            checksum.update(block)
    return checksum.hexdigest()


def frozen_database(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_file() or path.is_symlink():
        raise ValueError("Expected a regular frozen SQLite file")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(resolved) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("Frozen throughput source must be checkpointed; benchmark never checkpoints it")
    return resolved


def model_files(cache: Path) -> tuple[Path, list[Path]]:
    cache = cache.resolve()
    snapshot = cache / ("models--" + MODEL.replace("/", "--")) / "snapshots" / REVISION
    if not snapshot.is_dir():
        raise ValueError("Pinned MiniLM cache is absent; no download is permitted by this benchmark")
    files = sorted({path.resolve() for path in snapshot.rglob("*") if path.is_file()})
    if not files or any(not path.is_relative_to(cache) for path in files):
        raise ValueError("Model cache resolves outside its owned root")
    if not any((snapshot / name).is_file() for name in ("model.safetensors", "pytorch_model.bin")):
        raise ValueError("Pinned model weights are absent")
    return snapshot, files


def footprint(paths: list[Path]) -> dict[str, Any]:
    result = {}
    for path in paths:
        before = path.stat()
        checksum = sha(path)
        after = path.stat()

        def identity(value: os.stat_result) -> list[int]:
            return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]

        if identity(before) != identity(after):
            raise ValueError("Frozen source changed during capture")
        result[str(path)] = {"sha256": checksum, "identity": identity(after)}
    return result


def source_windows(connection: sqlite3.Connection, model: Any, maximum: int) -> list[str]:
    # Importing the existing function constructs no Library and loads no model.
    from book_rag.semantic import _windows

    def book_windows(identity: str) -> Generator[str]:
        cursor = connection.execute(
            "SELECT p.text,b.title,b.author,s.title section_title FROM passages p "
            "JOIN books b ON b.id=p.book_id JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
            "WHERE b.status='indexed' AND b.id=? ORDER BY p.id",
            (identity,),
        )
        for row in cursor:
            yield from _windows(model, row)

    books = [row[0] for row in connection.execute("SELECT id FROM books WHERE status='indexed' ORDER BY id")]
    if not 1 <= len(books) <= 20:
        raise ValueError("Only a known small source of at most20 indexed editions is allowed")
    generators = [book_windows(identity) for identity in books]
    windows: list[str] = []
    while generators and len(windows) < maximum:
        active = []
        for generator in generators:
            value = next(generator, None)
            if value is not None:
                windows.append(value)
                active.append(generator)
            if len(windows) == maximum:
                break
        generators = active
    if len(windows) < maximum:
        raise ValueError("Known source has too few windows")
    return windows


def worker(args: argparse.Namespace) -> None:
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ[name] = "2"
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
        os.environ[name] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ.pop("BOOKRAG_IVFPQ_WORKER", None)
    source = frozen_database(args.source)
    snapshot, files = model_files(args.cache)
    protected = [source, *files]
    before = footprint(protected)
    started = time.perf_counter()
    import numpy as np

    torch = importlib.import_module("torch")
    SentenceTransformer = importlib.import_module("sentence_transformers").SentenceTransformer

    torch.set_num_threads(2)
    if args.device == "mps" and not torch.backends.mps.is_available():
        raise ValueError("MPS is unavailable; CPU results must not be labelled MPS")
    model = SentenceTransformer(str(snapshot), device=args.device, local_files_only=True, trust_remote_code=False)
    load_seconds = time.perf_counter() - started
    if "faiss" in sys.modules or model.max_seq_length != 128:
        raise ValueError("Unexpected FAISS import or MiniLM token-window budget")
    connection = sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    started = time.perf_counter()
    try:
        texts = source_windows(connection, model, args.windows)
    finally:
        connection.close()
    prepare_seconds = time.perf_counter() - started
    input_checksum = hashlib.sha256()
    lengths = []
    for text in texts:
        input_checksum.update(text.encode() + b"\x00")
        lengths.append(len(model.tokenizer.encode(text, add_special_tokens=True, truncation=False)))
    output_checksum = hashlib.sha256()
    times = []
    mps_memory = []
    first_batch_seconds = 0.0
    measured_rows = 0
    for start in range(0, len(texts), args.batch_size):
        batch = texts[start : start + args.batch_size]
        if args.device == "mps":
            torch.mps.synchronize()
        began = time.perf_counter()
        vectors = model.encode(
            batch, batch_size=args.batch_size, normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True
        )
        if args.device == "mps":
            torch.mps.synchronize()
            mps_memory.append(
                {
                    "current_allocated_bytes": torch.mps.current_allocated_memory(),
                    "driver_allocated_bytes": torch.mps.driver_allocated_memory(),
                }
            )
        elapsed = time.perf_counter() - began
        if vectors.shape != (len(batch), 384) or vectors.dtype != np.float32 or not np.isfinite(vectors).all():
            raise ValueError("Invalid encoder output")
        if np.max(np.abs(np.linalg.norm(vectors, axis=1) - 1)) > 0.001:
            raise ValueError("Encoder did not normalize vectors")
        output_checksum.update(vectors.tobytes())
        if start == 0:
            first_batch_seconds = elapsed
        else:
            times.append(elapsed)
            measured_rows += len(batch)
    after = footprint(protected)
    if before != after or "faiss" in sys.modules:
        raise ValueError("Protected source changed or FAISS entered Torch worker")
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss = int(rss if sys.platform == "darwin" else rss * 1024)
    if rss > MAX_RSS or shutil.disk_usage(args.output.parent).free < RESERVE:
        raise ValueError("Encoder benchmark exceeded RSS/free-space budget")
    rate = measured_rows / sum(times)
    report = {
        "schema_version": 1,
        "purpose": "Offline encoder capacity only; no relevance or RAG quality measurement",
        "device": args.device,
        "model": MODEL,
        "revision": REVISION,
        "model_max_tokens": model.max_seq_length,
        "torch_version": torch.__version__,
        "threads": 2,
        "batch_size": args.batch_size,
        "total_encoded_windows_including_warmup": len(texts),
        "warmup_windows": min(args.batch_size, len(texts)),
        "measured_windows": measured_rows,
        "model_load_seconds_including_imports": load_seconds,
        "window_preparation_seconds": prepare_seconds,
        "first_batch_seconds": first_batch_seconds,
        "steady_encoding_seconds": sum(times),
        "steady_vectors_per_second": rate,
        "batch_seconds_p50": float(np.median(times)),
        "batch_seconds_p95": float(np.percentile(times, 95)),
        "input_token_length_min": min(lengths),
        "input_token_length_p50": float(np.median(lengths)),
        "input_token_length_max": max(lengths),
        "windows_over_model_token_limit": sum(n > 128 for n in lengths),
        "input_windows_sha256": input_checksum.hexdigest(),
        "output_vectors_sha256": output_checksum.hexdigest(),
        "process_peak_rss_bytes": rss,
        "mps_memory_samples": mps_memory,
        "original_sources_before": before,
        "original_sources_after": after,
        "source_byte_identical": before == after,
        "faiss_imported": False,
        "network_allowed": False,
        "projections": [
            {"windows": n, "encoding_hours_at_measured_rate": n / rate / 3600} for n in (1_865_985, 15_692_534)
        ],
        "limitations": [
            "At most1024 prepared windows from the known20 source, not full corpus tokenization/indexing.",
            "Model load and window preparation are reported separately; first batch excluded from steady rate.",
            "Projected hours exclude IO, source proofs, compression, retries, variable lengths and contention.",
            "CPU and MPS output hashes can differ numerically; no bit-exact cross-device claim is made.",
            "RSS and MPS driver allocations overlap; do not add them as an exact physical-memory total.",
        ],
    }
    temporary = args.output.with_suffix(args.output.suffix + ".owned-tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    os.link(temporary, args.output)
    temporary.unlink()
    print(
        json.dumps(
            {"report": str(args.output), "device": args.device, "vectors_per_second": rate, "peak_rss_bytes": rss}
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=("cpu", "mps"), required=True)
    parser.add_argument("--source", type=Path, default=Path("data/snapshots/curated20-v3/library.sqlite3"))
    parser.add_argument("--cache", type=Path, default=Path("data/models"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--windows", type=int, default=1024)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 32 or not 64 <= args.windows <= 1024 or args.windows <= args.batch_size:
        parser.error("Need batch1..32 and64..1024 total windows, including warmup")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        parser.error("Output must be new; do not overwrite measurements")
    if args.worker:
        worker(args)
        return
    frozen_database(args.source)
    model_files(args.cache)
    if shutil.disk_usage(args.output.parent).free < RESERVE + 1024**2:
        parser.error("Need4.5GiB free reserve")
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
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
                if int(check.stdout.strip() or "0") * 1024 > MAX_RSS or time.monotonic() - started > 120:
                    raise RuntimeError("Torch-only worker exceeded2GiB RSS or120s deadline") from None
                if shutil.disk_usage(args.output.parent).free < RESERVE:
                    raise RuntimeError("Free space crossed4.5GiB reserve") from None
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
