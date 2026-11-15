"""Optional, CPU-only IVF-PQ experiment. Never used by production retrieval.

The caller supplies already encoded vectors and a source/model descriptor.
This module creates immutable independently resumable shards; it does not
establish a live SQLite coverage binding or make source attribution claims.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class PQConfig:
    dimensions: int = 384
    nlist: int = 128
    subquantizers: int = 64
    bits: int = 8
    iterations: int = 12
    seed: int = 20261115
    threads: int = 2

    def validate(self) -> None:
        for value in asdict(self).values():
            if type(value) is not int:
                raise ValueError("PQ configuration requires integers")
        if not 1 <= self.dimensions <= 4096 or self.subquantizers < 1 or self.dimensions % self.subquantizers:
            raise ValueError("Dimensions must be divisible by positive subquantizers")
        if not 1 <= self.subquantizers <= self.dimensions or self.bits not in (4, 8):
            raise ValueError("Unsupported PQ code shape")
        if not 1 <= self.nlist <= 4096 or not 1 <= self.iterations <= 100 or not 1 <= self.threads <= 2:
            raise ValueError("Unbounded experimental training configuration")
        if not 0 <= self.seed < 2**31:
            raise ValueError("Clustering seed must fit a nonnegative signed32bit integer")


def faiss_backend() -> Any:
    """Keep imports and ordinary project CI independent of the native extra."""
    if os.environ.get("BOOKRAG_IVFPQ_WORKER") != "1" or "torch" in sys.modules:
        raise RuntimeError("Native FAISS requires a dedicated CPU subprocess without Torch")
    try:
        return importlib.import_module("faiss")
    except ImportError as exc:
        raise RuntimeError("Optional experiment requires faiss-cpu; see docs/SCALABLE_SEMANTIC.md") from exc


def file_sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected an owned regular nonsymlink file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def array_sha256(array: NDArray[Any]) -> str:
    if not array.flags.c_contiguous:
        raise ValueError("Expected a contiguous bounded experiment array")
    return hashlib.sha256(memoryview(array).cast("B")).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    payload = canonical_bytes(value) + b"\n"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise ValueError("Immutable experiment metadata differs")
        return
    descriptor, name = tempfile.mkstemp(prefix=".pq-owned-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_index(backend: Any, index: Any, path: Path) -> str:
    descriptor, name = tempfile.mkstemp(prefix=".pq-owned-", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        backend.write_index(index, str(temporary))
        checksum = file_sha256(temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.link(temporary, path)
        return checksum
    finally:
        temporary.unlink(missing_ok=True)


def read_verified_index(path: Path, expected_sha256: str) -> Any:
    """Read only self-produced trusted indexes, verifying bytes before native IO."""
    before = path.stat()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or file_sha256(path) != expected_sha256:
        raise ValueError("Experimental FAISS index checksum mismatch")
    if path.stat() != before:
        raise ValueError("Experimental FAISS index changed during verification")
    index = faiss_backend().read_index(str(path))
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("Experimental FAISS index changed during read")
    return index


def train_codec(training: NDArray[np.float32], config: PQConfig) -> Any:
    config.validate()
    _check_vectors(training, config.dimensions)
    if len(training) < max(config.nlist, 1 << config.bits):
        raise ValueError("Too few training vectors for coarse/PQ centroids")
    backend = faiss_backend()
    backend.omp_set_num_threads(config.threads)
    quantizer = backend.IndexFlatIP(config.dimensions)
    index = backend.IndexIVFPQ(
        quantizer, config.dimensions, config.nlist, config.subquantizers, config.bits, backend.METRIC_INNER_PRODUCT
    )
    index.cp.niter = config.iterations
    index.cp.seed = config.seed
    index.cp.spherical = True
    index.pq.cp.niter = config.iterations
    index.pq.cp.seed = config.seed
    index.use_precomputed_table = -1
    index.train(training)
    if not index.is_trained or index.ntotal:
        raise ValueError("Expected an empty trained codec")
    return index


def _check_vectors(vectors: NDArray[Any], dimensions: int) -> None:
    if vectors.dtype != np.float32 or vectors.ndim != 2 or vectors.shape[1] != dimensions:
        raise ValueError("Expected a float32 matrix with declared dimensions")
    if not vectors.flags.c_contiguous or not np.isfinite(vectors).all():
        raise ValueError("Expected contiguous finite vectors")
    norms = np.linalg.norm(vectors, axis=1)
    if np.any(np.abs(norms - 1) > 0.001):
        raise ValueError("This cosine experiment requires existing unit-normalized vectors")


def build_shards(
    vectors: NDArray[np.float32],
    training: NDArray[np.float32],
    output: Path,
    source_descriptor: dict[str, Any],
    *,
    config: PQConfig = PQConfig(),
    shard_rows: int = 8192,
    stop_after_shards: int | None = None,
) -> dict[str, Any]:
    """Resume sealed shards with the same codec/input identity and zero re-encode.

    Input is a bounded sample in this experiment, not the complete live corpus.
    The optional stop is an explicit interrupt/resume demonstration and test hook.
    A private output directory permits one writer; concurrency is not implemented.
    """
    config.validate()
    _check_vectors(vectors, config.dimensions)
    _check_vectors(training, config.dimensions)
    if not len(vectors) or type(shard_rows) is not int or not 1 <= shard_rows <= 65536:
        raise ValueError("Invalid bounded shard size")
    if stop_after_shards is not None and (type(stop_after_shards) is not int or stop_after_shards < 1):
        raise ValueError("Invalid stop count")
    backend = faiss_backend()
    descriptor = {
        "schema_version": 1,
        "kind": "experimental-only-ivfpq-shards",
        "source": source_descriptor,
        "config": asdict(config),
        "rows": len(vectors),
        "shard_rows": shard_rows,
        "vectors_sha256": array_sha256(vectors),
        "training_sha256": array_sha256(training),
        "metric": "inner-product-of-existing-unit-float32-vectors",
        "codec_implementation": "faiss-cpu-" + backend.__version__,
    }
    identity = hashlib.sha256(canonical_bytes(descriptor)).hexdigest()
    directory = output / ("ivfpq-" + identity)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Experiment output must not be a symlink")
    _write_json(directory / "descriptor.json", descriptor)
    backend.omp_set_num_threads(config.threads)
    codec_path = directory / "codec.faiss"
    codec_receipt = directory / "codec.json"
    if codec_receipt.exists():
        sealed_codec = json.loads(codec_receipt.read_text())
        if sealed_codec.get("identity") != identity:
            raise ValueError("Codec receipt belongs to different input/model space")
        codec_sha = sealed_codec["sha256"]
        codec = read_verified_index(codec_path, codec_sha)
    else:
        if codec_path.exists():
            if codec_path.is_symlink():
                raise ValueError("Unsealed codec must not be a symlink")
            codec_path.unlink()  # Owned incomplete publication; no receipt/active pointer exists.
        codec = train_codec(training, config)
        codec_sha = _write_index(backend, codec, codec_path)
        _write_json(codec_receipt, {"sha256": codec_sha, "identity": identity})
    if not codec.is_trained or codec.ntotal or codec.d != config.dimensions:
        raise ValueError("Invalid trained experimental codec")
    shards: list[dict[str, Any]] = []
    resumed = 0
    created = 0
    for start in range(0, len(vectors), shard_rows):
        end = min(start + shard_rows, len(vectors))
        filename = f"shard-{start:08d}-{end:08d}.faiss"
        path = directory / filename
        receipt_path = directory / (filename + ".json")
        expected = {"identity": identity, "start": start, "end": end, "codec_sha256": codec_sha, "file": filename}
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if any(receipt.get(key) != value for key, value in expected.items()):
                raise ValueError("Shard receipt belongs to different input/codec")
            index = read_verified_index(path, receipt["sha256"])
            if index.ntotal != end - start or index.d != config.dimensions:
                raise ValueError("Shard row count/dimension mismatch")
            resumed += 1
        else:
            if path.exists():
                if path.is_symlink():
                    raise ValueError("Unsealed shard must not be a symlink")
                path.unlink()
            index = backend.clone_index(codec)
            index.add_with_ids(vectors[start:end], np.arange(start, end, dtype=np.int64))
            receipt = {**expected, "sha256": _write_index(backend, index, path)}
            _write_json(receipt_path, receipt)
            created += 1
        shards.append(receipt)
        del index
        if stop_after_shards is not None and created >= stop_after_shards:
            break
    complete = len(shards) == (len(vectors) + shard_rows - 1) // shard_rows
    result = {
        "identity": identity,
        "directory": str(directory),
        "complete": complete,
        "rows": len(vectors),
        "code_bytes_per_vector": (config.subquantizers * config.bits + 7) // 8,
        "faiss_id_bytes_per_vector": 8,
        "resumed_shards": resumed,
        "created_shards": created,
        "shards": shards,
    }
    if complete:
        # Completion manifest has no process-specific resumed/created counters.
        _write_json(
            directory / "complete.json",
            {k: v for k, v in result.items() if k not in {"directory", "resumed_shards", "created_shards"}},
        )
    return result


def search_shards(
    build: dict[str, Any], queries: NDArray[np.float32], *, nprobe: int, candidate_windows: int = 4096
) -> tuple[NDArray[np.float32], NDArray[np.int64]]:
    """Read one immutable shard at a time; stable merge by score then window ID."""
    if not build.get("complete") or type(nprobe) is not int or nprobe < 1:
        raise ValueError("Only complete experimental builds can be searched")
    if type(candidate_windows) is not int or not 1 <= candidate_windows <= 8192:
        raise ValueError("Candidate pool must be bounded")
    backend = faiss_backend()
    descriptor = json.loads((Path(build["directory"]) / "descriptor.json").read_text())
    _check_vectors(queries, descriptor["config"]["dimensions"])
    backend.omp_set_num_threads(descriptor["config"]["threads"])
    if nprobe > descriptor["config"]["nlist"]:
        raise ValueError("nprobe exceeds coarse centroid count")
    scores = np.empty((len(queries), 0), dtype=np.float32)
    rows = np.empty((len(queries), 0), dtype=np.int64)
    for receipt in build["shards"]:
        index = read_verified_index(Path(build["directory"]) / receipt["file"], receipt["sha256"])
        index.nprobe = nprobe
        block_scores, block_rows = index.search(queries, min(candidate_windows, index.ntotal))
        combined_scores = np.concatenate((scores, block_scores), axis=1)
        combined_rows = np.concatenate((rows, block_rows), axis=1)
        combined_scores[combined_rows < 0] = -np.inf
        order = np.lexsort((combined_rows, -combined_scores), axis=1)[:, :candidate_windows]
        scores = np.take_along_axis(combined_scores, order, axis=1)
        rows = np.take_along_axis(combined_rows, order, axis=1)
        del index
    return scores, rows


def unique_passages(
    window_rows: NDArray[np.int64], passage_ids: NDArray[np.int64], excluded_passage: int, limit: int
) -> list[int]:
    """Consume ranked window IDs, excluding every window of the query passage."""
    if limit < 1:
        raise ValueError("Positive unique-passage limit required")
    seen: set[int] = {excluded_passage}
    result: list[int] = []
    for row in window_rows:
        if row < 0:
            continue
        if row >= len(passage_ids):
            raise ValueError("Window ID out of sample bounds")
        passage = int(passage_ids[row])
        if passage not in seen:
            seen.add(passage)
            result.append(passage)
            if len(result) == limit:
                break
    return result


def projected_capacity(vectors: int, dimensions: int = 384, code_bytes: int = 64) -> dict[str, int]:
    if any(type(value) is not int or value < 1 for value in (vectors, dimensions, code_bytes)):
        raise ValueError("Positive integer capacity parameters required")
    return {
        "vectors": vectors,
        "float32_matrix_bytes": vectors * dimensions * 4,
        "ivfpq_codes_plus_window_ids_bytes": vectors * (code_bytes + 8),
        "window_to_passage_mapping_bytes": vectors * 8,
        "ivfpq_plus_mapping_bytes": vectors * (code_bytes + 16),
        "float16_plus_mapping_bytes": vectors * (dimensions * 2 + 8),
        "scalar_int8_plus_scale_and_mapping_bytes": vectors * (dimensions + 4 + 8),
    }
