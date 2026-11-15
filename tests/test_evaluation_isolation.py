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
from test_semantic import FakeModel, FakeTokenizer
from typer.testing import CliRunner

from book_rag import evaluation, index_installation, semantic
from book_rag.cli import app as cli_app
from book_rag.evaluation import evaluate, evaluation_library
from book_rag.library import Library


def installed_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Library, Path, FakeModel]:
    """Build/install only tiny actual indexes; every text/model is an owned fake."""
    monkeypatch.delenv("BOOKRAG_MODEL_CACHE", raising=False)
    original = Library(tmp_path / "index-origin")
    raw = tmp_path / "covered.txt"
    raw.write_text("Amber bookmarks preserve an exact quotation from this original CC0 fixture.")
    original.import_book(
        raw,
        {
            "id": "covered",
            "title": "Covered fixture",
            "author": "CC0 fixture author",
            "language": "en",
            "edition": "Covered CC0 edition",
        },
    )
    model = FakeModel()
    monkeypatch.setattr(semantic, "_model", lambda *args, **kwargs: model)
    semantic.build_index(original, batch_size=2)
    directory = tmp_path / "installed-library"
    directory.mkdir()
    with original.connect() as source, sqlite3.connect(directory / "library.sqlite3") as copied:
        source.backup(copied)
    target = Library(directory)
    outside = tmp_path / "outside.txt"
    outside.write_text("Zirconium discoveries belong to the newer edition outside vector coverage.")
    target.import_book(
        outside,
        {
            "id": "outside",
            "title": "Outside fixture",
            "author": "CC0 fixture author",
            "language": "en",
            "edition": "Outside CC0 edition",
        },
    )
    monkeypatch.setattr(index_installation, "_load_tokenizer", lambda *args: FakeTokenizer())
    index_installation.install_partial_index(original.data_dir, target.data_dir)
    models = directory / "models"
    models.mkdir()
    (models / "config.json").write_text('{"fixture":"owned cache metadata"}')
    (models / "weights.bin").write_bytes(b"original fake weight bytes" * 60_000)
    dataset = tmp_path / "owned-partial-dev.json"
    dataset.write_text(
        json.dumps(
            {
                "version": 1,
                "split": "dev",
                "queries": [
                    {
                        "id": "covered",
                        "query": "Amber bookmarks",
                        "type": "quote",
                        "language": "en",
                        "answerable": True,
                        "relevant_book_ids": ["covered"],
                    },
                    {
                        "id": "outside",
                        "query": "Zirconium discoveries",
                        "type": "quote",
                        "language": "en",
                        "answerable": True,
                        "relevant_book_ids": ["outside"],
                    },
                ],
            }
        )
    )
    return target, dataset, model


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
        assert not hasattr(library, "semantic_reference")
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


def test_owned_cache_retains_real_hf_relative_snapshot_links(tmp_path: Path) -> None:
    source = tmp_path / "shared-models"
    repository = source / "models--fixture--owned"
    blobs = repository / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "config-object").write_text('{"model_type":"owned-fixture"}')
    (blobs / "weight-object").write_bytes(b"w" * (evaluation._CACHE_COPY_LIMIT + 10))
    snapshot = repository / "snapshots" / "pinned-revision"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").symlink_to("../../blobs/config-object")
    (snapshot / "model.safetensors").symlink_to("../../blobs/weight-object")
    (snapshot / "missing.json").symlink_to("../../blobs/not-acquired")
    before = tree_hashes(source)
    owned = tmp_path / "owned-models"
    evaluation._copy_model_cache(source, owned)
    copied_snapshot = owned / snapshot.relative_to(source)
    assert (copied_snapshot / "config.json").read_text() == (snapshot / "config.json").read_text()
    assert not (copied_snapshot / "config.json").is_symlink()
    assert (copied_snapshot / "model.safetensors").is_symlink()
    assert (copied_snapshot / "model.safetensors").resolve() == (blobs / "weight-object").resolve()
    assert not (copied_snapshot / "missing.json").is_symlink()
    (copied_snapshot / "config.json").write_text('{"model_type":"owned-update"}')
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


def test_partial_evaluation_validates_original_identity_but_reads_owned_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, dataset, model = installed_partial(tmp_path, monkeypatch)
    original_library = evaluation.Library

    class OwnedMigration(original_library):
        def __init__(self, directory: Path) -> None:
            assert directory != target.data_dir
            super().__init__(directory)
            with self.connect() as conn:
                conn.execute("CREATE TABLE owned_evaluation_schema_fixture(value TEXT)")

    monkeypatch.setattr(evaluation, "Library", OwnedMigration)
    # Even pre-existing sidecar bytes must stay untouched. Immutable source
    # reads ignore these empty-WAL/shared-memory files rather than opening them.
    target.db_path.with_name("library.sqlite3-wal").write_bytes(b"")
    target.db_path.with_name("library.sqlite3-shm").write_bytes(b"owned shared memory marker" * 200)
    before = tree_hashes(tmp_path / "index-origin"), tree_hashes(target.data_dir)
    encoded_before = model.calls
    with evaluation_library(target.data_dir) as library:
        reference = library.semantic_reference
        assert reference.data_dir == target.data_dir and reference.db_path == target.db_path
        assert reference.db_path.stat().st_ino != library.db_path.stat().st_ino
        with reference.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM books WHERE status='indexed'").fetchone()[0] == 2
            assert not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='owned_evaluation_schema_fixture'"
            ).fetchone()
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("CREATE TABLE cannot_write_original(value TEXT)")
        report = evaluate(library, dataset, mode="hybrid")
        assert report["actual_search_modes"] == {"hybrid": 2}
        assert model.calls == encoded_before + 2
        assert report["metrics"]["hit_at_k"] == report["metrics"]["quote_exact_rate"] == 1
        assert "partial_index_reference" in report["evaluation_input"]
        assert all("fallback" not in warning.casefold() for case in report["cases"] for warning in case["warnings"])
    assert (tree_hashes(tmp_path / "index-origin"), tree_hashes(target.data_dir)) == before


def test_evaluate_path_uses_actual_partial_hybrid_without_original_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, dataset, _ = installed_partial(tmp_path, monkeypatch)
    before = tree_hashes(target.data_dir)
    report = evaluate(target.data_dir, dataset, mode="hybrid")
    assert report["actual_search_modes"] == {"hybrid": 2}
    assert report["corpus"]["books"] == 2
    assert tree_hashes(target.data_dir) == before


def test_partial_evaluation_tolerates_actual_external_readers_coordination_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, dataset, model = installed_partial(tmp_path, monkeypatch)
    before = hashlib.sha256(target.db_path.read_bytes()).hexdigest()
    calls = model.calls
    reader = None
    try:
        with evaluation_library(target.data_dir) as library:
            reader = sqlite3.connect(target.db_path.resolve().as_uri() + "?mode=ro", uri=True)
            reader.execute("BEGIN")
            assert reader.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 2
            assert target.db_path.with_name("library.sqlite3-shm").exists()
            wal = target.db_path.with_name("library.sqlite3-wal")
            assert not wal.exists() or wal.stat().st_size == 0
            result = evaluate(library, dataset, mode="hybrid")
            assert result["actual_search_modes"] == {"hybrid": 2}
            assert model.calls == calls + 2
            assert result["metrics"]["quote_exact_rate"] == 1
            # Leave the coordination files present for the context-exit guard.
            # The external reader owns them; the evaluator never opens them.
    finally:
        if reader is not None:
            reader.close()
    assert hashlib.sha256(target.db_path.read_bytes()).hexdigest() == before


def test_empty_wal_is_not_canonical_input_but_nonempty_wal_is(tmp_path: Path) -> None:
    (tmp_path / "library.sqlite3").write_bytes(b"owned test bytes")
    before = evaluation._database_files(tmp_path)
    wal = tmp_path / "library.sqlite3-wal"
    wal.touch()
    assert evaluation._database_files(tmp_path) == before
    wal.write_bytes(b"a WAL header must fail closed even without a verified committed frame")
    assert evaluation._database_files(tmp_path) != before
    assert "library.sqlite3-wal" in evaluation._database_files(tmp_path)


def test_partial_pending_wal_is_rejected_without_touching_main_wal_or_shm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, dataset, _ = installed_partial(tmp_path, monkeypatch)
    with sqlite3.connect(target.db_path) as writer:
        writer.execute("INSERT INTO saved(kind,title,query) VALUES ('query','Owned WAL fixture','Amber')")
        writer.commit()
        assert target.db_path.with_name("library.sqlite3-wal").stat().st_size > 0
        before = tree_hashes(target.data_dir)
        with pytest.raises(ValueError, match="checkpointed original.*reinstall its index binding"):
            evaluate(target.data_dir, dataset, mode="hybrid")
        assert tree_hashes(target.data_dir) == before


def test_stale_partial_binding_falls_back_without_rebinding_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, dataset, model = installed_partial(tmp_path, monkeypatch)
    with target.connect() as conn:
        conn.execute("UPDATE books SET title='Updated covered metadata' WHERE id='covered'")
    before = tree_hashes(target.data_dir)
    calls = model.calls
    report = evaluate(target.data_dir, dataset, mode="hybrid")
    assert report["actual_search_modes"] == {"lexical": 2}
    assert all(case["warnings"] for case in report["cases"])
    assert model.calls == calls and tree_hashes(target.data_dir) == before


def test_original_change_during_partial_evaluation_discards_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, dataset, model = installed_partial(tmp_path, monkeypatch)
    encode = model.encode
    changed = False

    def concurrent_write(texts: list[str], **kwargs: object) -> np.ndarray:
        nonlocal changed
        if not changed:
            changed = True
            with target.connect() as conn:
                # Outside coverage stays valid for normal live hybrid search,
                # but a frozen evaluation must still discard a changed corpus.
                conn.execute("UPDATE books SET title='Concurrent outside change' WHERE id='outside'")
        return encode(texts, **kwargs)

    monkeypatch.setattr(model, "encode", concurrent_write)
    with pytest.raises(RuntimeError, match="partial-index SQLite input changed|Source SQLite changed"):
        evaluate(target.data_dir, dataset, mode="hybrid")
    assert changed


def test_installation_receipt_change_is_part_of_partial_evaluation_footprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, _, _ = installed_partial(tmp_path, monkeypatch)
    active = json.loads((target.data_dir / "vectors" / "active.json").read_text())
    receipt = target.data_dir / "vectors" / active["installation_receipt"]["path"]
    with pytest.raises(RuntimeError, match="installation receipt changed"), evaluation_library(target.data_dir):
        receipt.write_bytes(receipt.read_bytes() + b" ")
