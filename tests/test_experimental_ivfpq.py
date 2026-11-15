"""Synthetic numeric/durability tests; optional native FAISS is never required."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from book_rag import experimental_ivfpq as pq


class FakeIndex:
    def __init__(self, dimensions: int, nlist: int = 4):
        self.d = dimensions
        self.nlist = nlist
        self.ntotal = 0
        self.is_trained = False
        self.cp = SimpleNamespace()
        self.pq = SimpleNamespace(cp=SimpleNamespace())
        self.values = np.empty((0, dimensions), dtype=np.float32)
        self.row_ids = np.empty(0, dtype=np.int64)

    def train(self, training: np.ndarray) -> None:
        self.is_trained = True

    def add_with_ids(self, values: np.ndarray, row_ids: np.ndarray) -> None:
        self.values = values.copy()
        self.row_ids = row_ids.copy()
        self.ntotal = len(values)

    def search(self, queries: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
        scores = queries @ self.values.T
        order = np.lexsort((np.broadcast_to(self.row_ids, scores.shape), -scores), axis=1)[:, :k]
        return np.take_along_axis(scores, order, axis=1), self.row_ids[order]


class FakeBackend:
    METRIC_INNER_PRODUCT = 0
    __version__ = "synthetic-v1"

    def __init__(self):
        self.training_calls = 0
        self.writes = 0
        self.fail_next_write = False

    def omp_set_num_threads(self, threads: int) -> None:
        assert 1 <= threads <= 2

    def IndexFlatIP(self, dimensions: int) -> int:
        return dimensions

    def IndexIVFPQ(self, quantizer: Any, dimensions: int, nlist: int, *args: Any) -> FakeIndex:
        self.training_calls += 1
        return FakeIndex(dimensions, nlist)

    def clone_index(self, index: FakeIndex) -> FakeIndex:
        return copy.deepcopy(index)

    def write_index(self, index: FakeIndex, filename: str) -> None:
        self.writes += 1
        Path(filename).write_text(
            json.dumps(
                {
                    "dimensions": index.d,
                    "nlist": index.nlist,
                    "trained": index.is_trained,
                    "values": index.values.tolist(),
                    "ids": index.row_ids.tolist(),
                }
            )
        )
        if self.fail_next_write:
            self.fail_next_write = False
            raise OSError("Synthetic interrupted write")

    def read_index(self, filename: str) -> FakeIndex:
        value = json.loads(Path(filename).read_text())
        index = FakeIndex(value["dimensions"], value["nlist"])
        index.is_trained = value["trained"]
        index.values = np.array(value["values"], dtype=np.float32).reshape(-1, index.d)
        index.row_ids = np.array(value["ids"], dtype=np.int64)
        index.ntotal = len(index.values)
        return index


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    value = FakeBackend()
    monkeypatch.setattr(pq, "faiss_backend", lambda: value)
    return value


@pytest.fixture
def vectors() -> np.ndarray:
    value = np.random.default_rng(33).standard_normal((512, 8), dtype=np.float32)
    value /= np.linalg.norm(value, axis=1, keepdims=True)
    return value


def config() -> pq.PQConfig:
    return pq.PQConfig(dimensions=8, nlist=4, subquantizers=2, bits=4, iterations=2)


def build(vectors: np.ndarray, output: Path, **kwargs: Any) -> dict[str, Any]:
    return pq.build_shards(
        vectors,
        vectors[:256],
        output,
        {"model": "synthetic", "source_version": "a"},
        config=config(),
        shard_rows=128,
        **kwargs,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"dimensions": 0},
        {"subquantizers": 0},
        {"subquantizers": -1},
        {"dimensions": 7},
        {"bits": 9},
        {"nlist": 0},
        {"iterations": 0},
        {"threads": 8},
        {"nlist": True},
    ],
)
def test_configuration_is_bounded(change: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        replace(config(), **change).validate()


def test_capacity_includes_ids_and_external_passage_mapping() -> None:
    value = pq.projected_capacity(1_000_000)
    assert value["float32_matrix_bytes"] == 1_536_000_000
    assert value["ivfpq_codes_plus_window_ids_bytes"] == 72_000_000
    assert value["ivfpq_plus_mapping_bytes"] == 80_000_000
    assert value["window_to_passage_mapping_bytes"] == 8_000_000


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_capacity_rejects_invalid_count(count: Any) -> None:
    with pytest.raises(ValueError):
        pq.projected_capacity(count)


def test_incomplete_build_is_not_searchable_and_resume_skips_sealed_shard(
    tmp_path: Path, vectors: np.ndarray, backend: FakeBackend
) -> None:
    first = build(vectors, tmp_path, stop_after_shards=1)
    assert not first["complete"]
    with pytest.raises(ValueError, match="complete"):
        pq.search_shards(first, vectors[:1], nprobe=1)
    first_shard = Path(first["directory"]) / first["shards"][0]["file"]
    before = first_shard.stat()
    checksum = pq.file_sha256(first_shard)
    training_calls = backend.training_calls
    writes = backend.writes
    second = build(vectors, tmp_path)
    assert second["complete"] and second["resumed_shards"] == 1 and second["created_shards"] == 3
    assert backend.training_calls == training_calls
    assert backend.writes == writes + 3
    assert first_shard.stat() == before
    assert pq.file_sha256(first_shard) == checksum
    third = build(vectors, tmp_path)
    assert third["resumed_shards"] == 4 and third["created_shards"] == 0
    assert backend.training_calls == training_calls


def test_changed_input_and_model_space_create_distinct_generations(
    tmp_path: Path, vectors: np.ndarray, backend: FakeBackend
) -> None:
    original = build(vectors, tmp_path)
    modified = vectors.copy()
    modified[[0, 1]] = modified[[1, 0]]
    different = build(modified, tmp_path)
    different_model = pq.build_shards(
        vectors,
        vectors[:256],
        tmp_path,
        {"model": "another-model", "source_version": "a"},
        config=config(),
        shard_rows=128,
    )
    assert len({original["identity"], different["identity"], different_model["identity"]}) == 3
    assert (Path(original["directory"]) / "complete.json").exists()


@pytest.mark.parametrize("filename", ["codec.faiss", "shard-00000000-00000128.faiss"])
def test_corrupted_sealed_index_rejected_before_native_load(
    filename: str, tmp_path: Path, vectors: np.ndarray, backend: FakeBackend
) -> None:
    first = build(vectors, tmp_path)
    path = Path(first["directory"]) / filename
    path.write_text("corruption")
    with pytest.raises(ValueError, match="checksum"):
        build(vectors, tmp_path)
    assert path.read_text() == "corruption"


def test_codec_receipt_cannot_be_rebound_to_another_input(
    tmp_path: Path, vectors: np.ndarray, backend: FakeBackend
) -> None:
    first = build(vectors, tmp_path, stop_after_shards=1)
    directory = Path(first["directory"])
    checksum = pq.file_sha256(directory / "codec.faiss")
    receipt_path = directory / "codec.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["identity"] = "d" * 64
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="different input/model space"):
        build(vectors, tmp_path)
    assert pq.file_sha256(directory / "codec.faiss") == checksum


def test_failed_native_publish_cleans_only_owned_temporary_files(
    tmp_path: Path, vectors: np.ndarray, backend: FakeBackend
) -> None:
    backend.fail_next_write = True
    with pytest.raises(OSError, match="interrupted"):
        build(vectors, tmp_path)
    assert not list(tmp_path.rglob(".pq-owned-*"))
    assert not list(tmp_path.rglob("*.faiss"))
    assert not list(tmp_path.rglob("complete.json"))
    assert build(vectors, tmp_path)["complete"]


def test_unsealed_owned_shard_after_crash_rebuilt(tmp_path: Path, vectors: np.ndarray, backend: FakeBackend) -> None:
    first = build(vectors, tmp_path, stop_after_shards=1)
    directory = Path(first["directory"])
    (directory / "shard-00000128-00000256.faiss").write_text("incomplete-unsealed")
    resumed = build(vectors, tmp_path)
    assert resumed["complete"]
    assert pq.file_sha256(directory / resumed["shards"][1]["file"]) == resumed["shards"][1]["sha256"]


def test_shard_search_merge_matches_exact_with_stable_ids(
    tmp_path: Path, vectors: np.ndarray, backend: FakeBackend
) -> None:
    value = build(vectors, tmp_path)
    queries = vectors[:3].copy()
    scores, rows = pq.search_shards(value, queries, nprobe=4, candidate_windows=20)
    expected_scores = queries @ vectors.T
    expected = np.argsort(-expected_scores, axis=1)[:, :20]
    np.testing.assert_array_equal(rows, expected)
    np.testing.assert_allclose(scores, np.take_along_axis(expected_scores, expected, axis=1), atol=0.000001)


def test_unique_passages_excludes_all_query_windows_duplicates_and_padding() -> None:
    passage_ids = np.array([7, 7, 8, 8, 9, 10, 7], dtype=np.int64)
    rows = np.array([0, 6, 2, 1, 3, -1, 4, 5], dtype=np.int64)
    assert pq.unique_passages(rows, passage_ids, 7, 2) == [8, 9]
    assert pq.unique_passages(rows, passage_ids, 7, 10) == [8, 9, 10]
    with pytest.raises(ValueError, match="bounds"):
        pq.unique_passages(np.array([42], dtype=np.int64), passage_ids, 7, 10)


def test_non_unit_or_nonfinite_vectors_rejected_without_training(
    tmp_path: Path, vectors: np.ndarray, backend: FakeBackend
) -> None:
    for value in (vectors * 2, vectors.astype(np.float64)):
        with pytest.raises(ValueError):
            build(value, tmp_path)
    value = vectors.copy()
    value[0, 0] = np.nan
    with pytest.raises(ValueError, match="finite"):
        build(value, tmp_path)
    assert backend.training_calls == 0


def test_optional_backend_missing_fails_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> Any:
        raise ImportError("optional native package absent")

    monkeypatch.setattr(pq.importlib, "import_module", missing)
    monkeypatch.setenv("BOOKRAG_IVFPQ_WORKER", "1")
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    assert pq.projected_capacity(1)["ivfpq_plus_mapping_bytes"] == 80
    with pytest.raises(RuntimeError, match="Optional experiment"):
        pq.faiss_backend()


def test_native_backend_requires_explicit_separate_worker_without_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(name: str) -> Any:
        raise AssertionError("Must reject before importing any native package")

    monkeypatch.setattr(pq.importlib, "import_module", forbidden)
    monkeypatch.delenv("BOOKRAG_IVFPQ_WORKER", raising=False)
    with pytest.raises(RuntimeError, match="dedicated CPU subprocess"):
        pq.faiss_backend()
    monkeypatch.setenv("BOOKRAG_IVFPQ_WORKER", "1")
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    with pytest.raises(RuntimeError, match="without Torch"):
        pq.faiss_backend()


def test_probe_exact_reference_and_query_sampling_are_synthetic() -> None:
    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_ivfpq.py"
    spec = importlib.util.spec_from_file_location("ivfpq_probe_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    vectors = np.array([[1, 0], [1, 0], [0.9, 0.1], [0, 1]], dtype=np.float32)
    ids = np.array([10, 10, 11, 12], dtype=np.int64)
    assert module.exact_candidates(vectors, ids, vectors[0], 10, 10) == [11, 12]
    selected = module.choose_query_rows(ids, 3)
    assert len(set(ids[selected].tolist())) == 3
    evenly_spread = module.choose_query_rows(np.arange(10000, dtype=np.int64), 64)
    assert evenly_spread[0] == 0 and evenly_spread[-1] == 9999
    assert np.max(np.diff(evenly_spread)) <= 159


def test_native_tiny_index_only_in_isolated_subprocess(tmp_path: Path) -> None:
    if importlib.util.find_spec("faiss") is None:
        pytest.skip("Optional FAISS wheel absent")
    script = r"""
import sys
from pathlib import Path
import numpy as np
from book_rag.experimental_ivfpq import PQConfig, build_shards, search_shards
vectors = np.random.default_rng(33).standard_normal((512, 8), dtype=np.float32)
vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
value = build_shards(vectors, vectors[:256], Path(sys.argv[1]), {"model": "synthetic"},
    config=PQConfig(dimensions=8, nlist=4, subquantizers=2, bits=4, iterations=2), shard_rows=128)
scores, rows = search_shards(value, vectors[:1].copy(), nprobe=4, candidate_windows=16)
assert scores.shape == rows.shape == (1, 16)
assert np.isfinite(scores).all() and ((rows >= 0) & (rows < len(vectors))).all()
assert "torch" not in sys.modules
print("isolated-native-ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2", "BOOKRAG_IVFPQ_WORKER": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert "isolated-native-ok" in result.stdout
    assert "faiss._swigfaiss" not in sys.modules and "faiss" not in sys.modules
