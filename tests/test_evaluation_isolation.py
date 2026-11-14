"""Frozen inputs retain byte identity while owned copies receive schema updates."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from typer.testing import CliRunner

from book_rag import evaluation, semantic
from book_rag.cli import app as cli_app
from book_rag.evaluation import evaluate, evaluation_library
from book_rag.library import Library


def tree_hashes(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


def seed(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "frozen"
    library = Library(source)
    raw = tmp_path / "original.txt"
    raw.write_text("Amber bookmarks preserve a quotation from an original CC0 test.")
    library.import_book(
        raw,
        {
            "id": "fixture",
            "title": "Original test",
            "author": "CC0 fixture author",
            "language": "en",
            "edition": "CC0 test edition",
        },
    )
    hit = library.search("Amber")["results"][0]
    generation = source / "vectors" / "gen-frozen-test"
    generation.mkdir(parents=True)
    np.save(generation / "embeddings.npy", np.array([[1.0, 0.0]], dtype="float32"))
    np.save(generation / "ids.npy", np.array([hit["id"]], dtype="int64"))
    (generation / "metadata.json").write_text('{"fixture":"immutable generation"}')
    (source / "vectors" / "active.json").write_text(
        json.dumps(
            {
                "generation": generation.name,
                "corpus_fingerprint": semantic.corpus_fingerprint(library),
                "model": semantic.DEFAULT_MODEL,
                "model_revision": semantic.DEFAULT_REVISION,
            }
        )
    )
    # An old schema has no pipeline table. Merely evaluating it must not migrate it.
    with library.connect() as connection:
        connection.execute("DROP TABLE source_pipeline")
    dataset = tmp_path / "owned-dev-fixture.json"
    dataset.write_text(
        json.dumps(
            {
                "version": 1,
                "split": "dev",
                "queries": [
                    {
                        "id": "owned-query",
                        "query": "Amber bookmarks",
                        "type": "quote",
                        "language": "en",
                        "answerable": True,
                        "relevant_book_ids": ["fixture"],
                        "evidence_contains": ["Amber bookmarks"],
                    }
                ],
            }
        )
    )
    return source, dataset


class QueryModel:
    def encode(self, texts: list[str], **kwargs: Any) -> np.ndarray:
        return np.array([[1.0, 0.0]] * len(texts), dtype="float32")


def test_owned_migration_semantic_mmap_and_model_metadata_leave_source_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, dataset = seed(tmp_path)
    cache = source / "models"
    cache.mkdir()
    (cache / "config.json").write_text('{"original":true}')
    (cache / "completed-weights").write_bytes(b"weights" * 200_000)
    snapshot = cache / "snapshots" / "pinned-revision"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").symlink_to(cache / "config.json")
    monkeypatch.delenv("BOOKRAG_MODEL_CACHE", raising=False)
    before = tree_hashes(source)
    source_db_sha = before["library.sqlite3"]
    with evaluation_library(source) as library:
        working = library.data_dir
        assert working != source and os.getenv("BOOKRAG_MODEL_CACHE") == str(working / "models")
        with library.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM source_pipeline").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("UPDATE books SET title='must not write'")
        generation = working / "vectors" / "gen-frozen-test"
        assert generation.is_symlink() and generation.resolve() == source / "vectors" / "gen-frozen-test"
        assert not (working / "vectors" / "active.json").is_symlink()
        matrix = np.load(generation / "embeddings.npy", mmap_mode="r")
        with pytest.raises(ValueError, match="read-only"):
            matrix[0, 0] = 2
        matrix._mmap.close()
        weights = working / "models" / "completed-weights"
        assert weights.is_symlink() and weights.resolve() == cache / "completed-weights"
        (working / "models" / "config.json").write_text('{"owned_metadata_change":true}')
        isolated_config = working / "models" / "snapshots" / "pinned-revision" / "config.json"
        assert not isolated_config.is_symlink()
        isolated_config.write_text('{"owned_snapshot_metadata_change":true}')

        def load(name: str, model_cache: str, revision: str | None = None) -> QueryModel:
            assert Path(model_cache) == working / "models"
            return QueryModel()

        monkeypatch.setattr(semantic, "_model", load)
        report = evaluate(library, dataset, mode="semantic")
        assert report["actual_search_modes"] == {"semantic": 1}
        assert report["metrics"]["hit_at_k"] == report["metrics"]["quote_exact_rate"] == 1
        assert report["evaluation_input"]["source_database_sha256"] == source_db_sha
        assert report["evaluation_input"]["model_cache_reuse"]["linked_bytes"] == 1_400_000
    assert not working.exists() and "BOOKRAG_MODEL_CACHE" not in os.environ
    assert tree_hashes(source) == before


def test_cli_evaluation_migrates_only_temporary_copy_and_protects_input_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, dataset = seed(tmp_path)
    before = tree_hashes(source)
    monkeypatch.setenv("BOOKRAG_DATA_DIR", str(source))
    output = tmp_path / "owned-report.json"
    result = CliRunner().invoke(
        cli_app, ["evaluate", "--dataset", str(dataset), "--mode", "lexical", "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text())
    assert report["metrics"]["quote_exact_rate"] == 1
    assert report["evaluation_input"]["source_database_sha256"] == before["library.sqlite3"]
    forbidden = CliRunner().invoke(
        cli_app, ["evaluate", "--dataset", str(dataset), "--output", str(source / "library.sqlite3")]
    )
    assert forbidden.exit_code != 0 and "outside" in forbidden.output
    assert tree_hashes(source) == before


def test_stable_wal_is_captured_without_touching_main_wal_or_shared_memory(tmp_path: Path) -> None:
    source, _ = seed(tmp_path)
    with sqlite3.connect(source / "library.sqlite3") as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE books SET title='Committed only in WAL' WHERE id='fixture'")
        writer.commit()
        assert (source / "library.sqlite3-wal").stat().st_size > 0
        before = tree_hashes(source)
        with evaluation_library(source) as library:
            book = library.get_book("fixture")
            assert book is not None and book["title"] == "Committed only in WAL"
            assert (
                library.input_provenance["source_sqlite_files_sha256"]["library.sqlite3-wal"]
                == before["library.sqlite3-wal"]
            )
        assert tree_hashes(source) == before


def test_cleanup_and_cache_environment_restored_after_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, _ = seed(tmp_path)
    shared = tmp_path / "shared-models"
    shared.mkdir()
    (shared / "config.json").write_text("original model configuration")
    monkeypatch.setenv("BOOKRAG_MODEL_CACHE", str(shared))
    before = tree_hashes(source)
    with pytest.raises(RuntimeError, match="owned test failure"), evaluation_library(source) as library:
        working = library.data_dir
        assert (working / "models" / "config.json").read_text() == "original model configuration"
        raise RuntimeError("owned test failure")
    assert not working.exists() and os.getenv("BOOKRAG_MODEL_CACHE") == str(shared)
    assert tree_hashes(source) == before


@pytest.mark.parametrize("index_state", ["missing", "stale_without_generation"])
def test_unavailable_semantic_index_keeps_explicit_fallback(tmp_path: Path, index_state: str) -> None:
    source, dataset = seed(tmp_path)
    active = source / "vectors" / "active.json"
    if index_state == "missing":
        active.unlink()
    else:
        active.write_text('{"corpus_fingerprint":"old-nonmatching-corpus"}')
    before = tree_hashes(source)
    report = evaluate(source, dataset, mode="semantic")
    assert report["actual_search_modes"] == {"lexical": 1}
    assert report["cases"][0]["warnings"]
    assert report["evaluation_input"]["vector_generation"] is None
    assert tree_hashes(source) == before


def test_source_changed_during_capture_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, _ = seed(tmp_path)
    original = evaluation._database_files
    calls = 0

    def changed(directory: Path) -> dict[str, tuple[int, int, int, int, int]]:
        nonlocal calls
        calls += 1
        observed = original(directory)
        if calls == 2:
            identity = observed["library.sqlite3"]
            observed["library.sqlite3"] = (*identity[:3], identity[3] + 1, identity[4])
        return observed

    before = tree_hashes(source)
    monkeypatch.setattr(evaluation, "_database_files", changed)
    with pytest.raises(RuntimeError, match="changed during evaluation capture"), evaluation_library(source):
        pytest.fail("A changing source must not be evaluated")
    assert tree_hashes(source) == before


def test_development_calibration_runner_uses_same_owned_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, dataset = seed(tmp_path)
    spec = importlib.util.spec_from_file_location("owned_calibration_runner", Path("scripts/calibrate_retrieval.py"))
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(semantic, "_model", lambda *args, **kwargs: QueryModel())
    before = tree_hashes(source)
    args = argparse.Namespace(
        dataset=dataset,
        data_dir=source,
        cache_dir=source / "models",
        kind="cosine",
        upstream_floor=0.3,
        candidate_depth=50,
        target_precision=0.9,
        min_accepted=1,
        objective="precision",
        false_accept_cost=5.0,
    )
    report = runner.collect(args)
    assert report["source_database_sha256"] == before["library.sqlite3"]
    assert report["examples"][0]["independent_source_checks"]["quote_exact"]
    assert report["evaluation_input"]["vector_generation"] == "gen-frozen-test"
    assert tree_hashes(source) == before
