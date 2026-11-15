"""Owned CC0 source/transport tests; parent process never imports native FAISS."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from book_rag import semantic_overlay as overlay
from book_rag.embedding_profiles import MINILM_MODEL, MINILM_PROFILE, MINILM_REVISION
from book_rag.experimental_ivfpq import file_sha256
from book_rag.incremental_ivfpq import _shard_name, digest
from book_rag.index_coverage import bind_coverage
from book_rag.library import Library
from book_rag.semantic_pilot import ReadOnlyLibrary, create_owned_subset, manifest_windows, prepare_windows


class Tokenizer:
    def encode(self, text: str, **kwargs: Any) -> list[int]:
        return [ord(char) for char in text]

    def decode(self, values: list[int], **kwargs: Any) -> str:
        return "".join(chr(value) for value in values)

    def __call__(self, text: str, **kwargs: Any) -> dict[str, Any]:
        return {"offset_mapping": [(j, j + 1) for j in range(len(text))]}


def write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False))


def seed_pilot(tmp_path: Path) -> SimpleNamespace:
    from book_rag.semantic import _windows, token_windows

    live = Library(tmp_path / "live")
    for number, text in enumerate(
        (
            "Серебряный ключ хранится в зелёной чашке. Точный исходный текст первого архива.",
            "Медное кольцо лежит на высокой полке. Совершенно другой текст второго архива.",
        )
    ):
        path = tmp_path / f"own-{number}.txt"
        path.write_text(text)
        live.import_book(
            path,
            {
                "id": f"cc0-{number}",
                "title": f"CC0 архив {number}",
                "author": "CC0 fixture",
                "language": "ru",
                "rights": "Own CC0 text",
            },
        )
    pilot = tmp_path / "pilot"
    pilot.mkdir()
    create_owned_subset(live.data_dir, pilot / "canonical", ["cc0-0", "cc0-1"])
    weight = tmp_path / "owned-fake-weight"
    weight.write_bytes(b"Owned fake encoder bytes; not a learned model")
    space = {
        "model": MINILM_MODEL,
        "revision": MINILM_REVISION,
        "dimensions": 384,
        "profile": MINILM_PROFILE.metadata(MINILM_REVISION),
        "strategy": "token-windows-v2",
        "assignment": "UNIT TEST ONLY: deterministic fake encoder, no semantic quality claim",
        "model_files_sha256": [file_sha256(weight)],
        "window_recipe_sha256": hashlib.sha256(
            (inspect.getsource(_windows) + inspect.getsource(token_windows)).encode()
        ).hexdigest(),
    }
    manifest = prepare_windows(
        pilot / "canonical",
        pilot / "windows.sqlite3",
        SimpleNamespace(tokenizer=Tokenizer(), max_seq_length=128),
        space,
    )
    rows = list(manifest_windows(pilot / "windows.sqlite3"))
    directory = pilot / "compressed"
    directory.mkdir()
    codec = directory / "codec.faiss"
    codec.write_bytes(b"Owned dummy codec; native tests replace with a real isolated codec")
    descriptor = {
        "schema_version": 1,
        "dimensions": 384,
        "nlist": 4,
        "shard_rows": len(rows),
        "rows": len(rows),
        "rows_by_book": manifest["rows_by_book"],
        "model_space": space,
        "model_space_sha256": digest(space),
        "covered_book_ids": ["cc0-0", "cc0-1"],
        "source_coverage": manifest["descriptor"]["source_coverage"],
        "codec_sha256": file_sha256(codec),
    }
    descriptor["identity"] = digest(descriptor)
    write(directory / "descriptor.json", descriptor)
    name = _shard_name(0, len(rows))
    (directory / (name + ".faiss")).write_bytes(b"Owned dummy shard; no native read in pure tests")
    np.savez(
        directory / (name + ".mapping.npz"),
        passages=np.array([r.passage_id for r in rows], dtype=np.int64),
        books=np.array([int(r.book_id[-1]) for r in rows], dtype=np.uint16),
    )
    write(directory / "chunk-00000000.json", {"kind": "owned test receipt"})
    receipt = {
        "identity": descriptor["identity"],
        "model_space_sha256": digest(space),
        "start": 0,
        "end": len(rows),
        "index_sha256": file_sha256(directory / (name + ".faiss")),
        "mapping_sha256": file_sha256(directory / (name + ".mapping.npz")),
        "chunks": [{"number": 0, "receipt_sha256": file_sha256(directory / "chunk-00000000.json")}],
        "coverage": descriptor["source_coverage"],
    }
    write(directory / (name + ".receipt.json"), receipt)
    receipt_sha = file_sha256(directory / (name + ".receipt.json"))
    binding = bind_coverage(ReadOnlyLibrary(pilot / "canonical"), receipt["coverage"], receipt_sha256=receipt_sha)
    write(directory / (name + ".binding.json"), binding)
    ready = {
        "schema_version": 1,
        "identity": descriptor["identity"],
        "rows": len(rows),
        "descriptor_sha256": file_sha256(directory / "descriptor.json"),
        "model_space_sha256": digest(space),
        "source_passage_coverage_verified": True,
        "source_passage_fingerprint": descriptor["source_coverage"]["passage_fingerprint"],
        "input_chain": manifest["input_chain"],
        "shards": [
            {
                "start": 0,
                "end": len(rows),
                "covered_book_ids": ["cc0-0", "cc0-1"],
                "receipt_sha256": receipt_sha,
                "binding_sha256": file_sha256(directory / (name + ".binding.json")),
            }
        ],
    }
    write(directory / "ready.json", ready)
    write(
        pilot / "report.json",
        {
            "resumed_phase": {
                "result": {"directory": str(directory), "ready_sha256": file_sha256(directory / "ready.json")}
            }
        },
    )
    with live.connect() as conn:
        passages = list(conn.execute("SELECT id,book_id FROM passages ORDER BY id"))
    candidates = [{"passage_id": row[0], "book_id": row[1], "score": 0.8 - n * 0.1} for n, row in enumerate(passages)]
    return SimpleNamespace(
        live=live,
        pilot=pilot,
        generation=directory,
        space=space,
        weight=weight,
        destination=tmp_path / "overlay",
        candidates=candidates,
        rows=rows,
    )


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = seed_pilot(tmp_path)

    def worker(request: dict[str, Any]) -> dict[str, Any]:
        if request["operation"] == "validate":
            return {"validated": True}
        hits = [r for r in state.candidates if not request.get("book_id") or r["book_id"] == request["book_id"]]
        return {"results": hits, "fallback_required": False, "source_changed": False}

    monkeypatch.setattr(overlay, "_run_worker", worker)
    monkeypatch.setattr(overlay, "_encode_query", lambda *args: np.eye(1, 384, dtype=np.float32))
    state.receipt = overlay.prepare_overlay(state.live, state.pilot, state.destination)
    monkeypatch.setenv("BOOKRAG_SEMANTIC_OVERLAY", str(state.destination / "installation.json"))
    return state


def test_explicit_install_preserves_original_bytes_and_binds_actual_live_inode(installed: SimpleNamespace) -> None:
    receipt = installed.receipt
    assert receipt["coverage_binding"]["db_identity"][0] == str(installed.live.db_path.resolve())
    assert receipt["coverage_binding"]["db_identity"][2] == installed.live.db_path.stat().st_ino
    assert receipt["coverage"]["books"] == 2
    assert not receipt["production_global_index_changed"]
    for path in installed.generation.glob("*.faiss"):
        linked = installed.destination / "generation" / path.name
        assert file_sha256(path) == file_sha256(linked)
    original_ready = file_sha256(installed.generation / "ready.json")
    assert original_ready != receipt["ready_sha256"]
    with pytest.raises(FileExistsError):
        overlay.prepare_overlay(installed.live, installed.pilot, installed.destination)


@pytest.mark.parametrize(
    "mode,book,language",
    [("semantic", None, None), ("hybrid", None, "en"), ("semantic", "outside", None), ("lexical", "cc0-0", "ru")],
)
def test_global_outside_and_other_modes_never_load_encoder(
    installed: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, mode: str, book: str | None, language: str | None
) -> None:
    monkeypatch.setattr(overlay, "_encode_query", lambda *args: pytest.fail("No model outside scoped coverage"))
    assert overlay.overlay_search(installed.live, "ключ", mode, book, language, 5) is None


def test_other_library_with_same_ids_is_untouched(installed: SimpleNamespace, tmp_path: Path) -> None:
    controlled = Library(tmp_path / "different-control")
    assert overlay.overlay_search(controlled, "ключ", "semantic", "cc0-0", "ru", 5) is None


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_source_verified_scoped_results_and_coverage(installed: SimpleNamespace, mode: str) -> None:
    result = overlay.overlay_search(installed.live, "ключ", mode, "cc0-0", "ru", 5)
    assert result and result["mode"] == mode
    assert result["results"] and all(r["verified"] and r["book_id"] == "cc0-0" for r in result["results"])
    assert result["semantic_coverage"] == {
        "indexed_books": 2,
        "searchable_books": 2,
        "active": True,
        "requested_book_covered": True,
        "scope": "ru21-scoped-overlay",
        "scope_language": "ru",
        "scope_searchable_books": 2,
    }


def test_russian_hybrid_includes_new_uncovered_word_hits_without_rebinding(
    installed: SimpleNamespace, tmp_path: Path
) -> None:
    text = tmp_path / "new.txt"
    text.write_text("Новый серебряный ключ появился в дополнительном архиве.")
    installed.live.import_book(text, {"id": "outside", "language": "ru"})
    result = overlay.overlay_search(installed.live, "серебряный ключ", "hybrid", None, "ru", 20)
    assert result and any(r["book_id"] == "outside" for r in result["results"])
    assert result["semantic_coverage"]["indexed_books"] == 2
    assert result["semantic_coverage"]["scope_searchable_books"] == 3


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE books SET title='Changed' WHERE id='cc0-1'",
        "UPDATE books SET edition='Changed edition' WHERE id='cc0-1'",
        "UPDATE books SET rights='Changed rights' WHERE id='cc0-1'",
        "UPDATE books SET source_url='https://example.invalid/new' WHERE id='cc0-1'",
        "UPDATE section_roles SET section_translator='Changed contributor'",
        "UPDATE book_metadata SET metadata='{}' WHERE book_id='cc0-1'",
        "UPDATE index_versions SET fingerprint='changed' WHERE book_id='cc0-1'",
        "UPDATE passages SET text=text||' corrupt' WHERE book_id='cc0-1'",
    ],
)
def test_any_covered_source_or_untracked_metadata_change_fails_entire_overlay(
    installed: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, statement: str
) -> None:
    with installed.live.connect() as conn:
        conn.execute(statement)
    monkeypatch.setattr(overlay, "_encode_query", lambda *args: pytest.fail("Stale source must fail before model"))
    result = overlay.overlay_search(installed.live, "ключ", "semantic", "cc0-0", "ru", 5)
    assert result and result["mode"] == "lexical" and result["warnings"]
    assert all(r["verified"] for r in result["results"])


def test_quoted_constraint_is_preserved(installed: SimpleNamespace) -> None:
    result = overlay.overlay_search(installed.live, '"медное кольцо"', "semantic", "cc0-0", "ru", 5)
    assert result and result["results"] == []


def test_changes_during_encoding_or_hydration_discard_stale_candidates(
    installed: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = overlay._SnapshotLibrary.passages

    def changed(self: Library, identities: list[int]) -> list[dict[str, Any]]:
        found = original(self, identities)
        with installed.live.connect() as conn:
            conn.execute("UPDATE books SET edition='New current edition' WHERE id='cc0-0'")
        return found

    monkeypatch.setattr(overlay._SnapshotLibrary, "passages", changed)
    result = overlay.overlay_search(installed.live, "ключ", "semantic", "cc0-0", "ru", 5)
    assert result and result["mode"] == "lexical"
    assert all(r["edition"] == "New current edition" for r in result["results"])


@pytest.mark.parametrize("corrupt", ["missing", "unverified", "wrong-owner", "nan"])
def test_invalid_native_evidence_cannot_escape_live_verification(
    installed: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, corrupt: str
) -> None:
    candidate = dict(installed.candidates[0])
    if corrupt == "missing":
        candidate["passage_id"] = 999999
    elif corrupt == "wrong-owner":
        candidate["book_id"] = "cc0-1"
    elif corrupt == "nan":
        candidate["score"] = float("nan")
    else:
        original = overlay._SnapshotLibrary.passages
        monkeypatch.setattr(
            overlay._SnapshotLibrary,
            "passages",
            lambda self, ids: [r | {"verified": False} for r in original(self, ids)],
        )
    monkeypatch.setattr(overlay, "_run_worker", lambda request: {"results": [candidate]})
    result = overlay.overlay_search(installed.live, "ключ", "semantic", "cc0-0", "ru", 5)
    assert result and result["mode"] == "lexical" and all(r["verified"] for r in result["results"])


def test_installation_change_midquery_is_rejected(installed: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    path = installed.destination / "installation.json"

    def changed(*args: Any) -> np.ndarray:
        value = json.loads(path.read_text())
        value["input_chain"] = "a" * 64
        write(path, value)
        return np.eye(1, 384, dtype=np.float32)

    monkeypatch.setattr(overlay, "_encode_query", changed)
    result = overlay.overlay_search(installed.live, "ключ", "semantic", "cc0-0", "ru", 5)
    assert result and result["mode"] == "lexical"


def test_failed_native_install_leaves_no_destination_or_owned_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = seed_pilot(tmp_path)
    before = {p: file_sha256(p) for p in state.generation.iterdir() if p.is_file()}

    def fail(request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("Controlled native failure")

    monkeypatch.setattr(overlay, "_run_worker", fail)
    with pytest.raises(RuntimeError, match="Controlled native"):
        overlay.prepare_overlay(state.live, state.pilot, state.destination)
    assert not state.destination.exists()
    assert not list(tmp_path.glob("overlay.overlay-staging-*"))
    assert before == {p: file_sha256(p) for p in state.generation.iterdir() if p.is_file()}


def test_source_change_after_native_validation_cannot_publish_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = seed_pilot(tmp_path)

    def changed(request: dict[str, Any]) -> dict[str, Any]:
        with state.live.connect() as conn:
            conn.execute("UPDATE books SET edition='Changed after native validation' WHERE id='cc0-0'")
        return {"validated": True}

    monkeypatch.setattr(overlay, "_run_worker", changed)
    with pytest.raises(ValueError, match="edition, attribution or provenance"):
        overlay.prepare_overlay(state.live, state.pilot, state.destination)
    assert not state.destination.exists()
    assert not list(tmp_path.glob("overlay.overlay-staging-*"))


def test_manifest_profile_and_model_recipe_must_match_ready_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = seed_pilot(tmp_path)
    path = state.pilot / "windows.sqlite3.receipt.json"
    value = json.loads(path.read_text())
    value["descriptor"]["window_recipe_sha256"] = "a" * 64
    write(path, value)
    monkeypatch.setattr(overlay, "_run_worker", lambda request: pytest.fail("Must fail before native load"))
    with pytest.raises(ValueError, match="Window producer"):
        overlay.prepare_overlay(state.live, state.pilot, state.destination)
    assert not state.destination.exists()


def test_copy_fallback_preserves_bytes_when_source_and_overlay_filesystems_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    state = seed_pilot(tmp_path)
    link = os.link

    def cross_device(source: Path, target: Path) -> None:
        if source.parent == state.generation:
            raise OSError(errno.EXDEV, "Owned cross-filesystem fixture")
        link(source, target)

    monkeypatch.setattr(os, "link", cross_device)
    monkeypatch.setattr(overlay, "_run_worker", lambda request: {"validated": True})
    overlay.prepare_overlay(state.live, state.pilot, state.destination)
    for source in state.generation.glob("*.faiss"):
        target = state.destination / "generation" / source.name
        assert file_sha256(source) == file_sha256(target)
        assert source.stat().st_ino != target.stat().st_ino


def test_changed_source_during_encoding_and_duplicate_worker_ids_fail_closed(
    installed: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    duplicate = [installed.candidates[0], installed.candidates[0]]
    monkeypatch.setattr(overlay, "_run_worker", lambda request: {"results": duplicate})
    result = overlay.overlay_search(installed.live, "ключ", "semantic", "cc0-0", "ru", 5)
    assert result and result["mode"] == "lexical"

    def changed(*args: Any) -> np.ndarray:
        with installed.live.connect() as conn:
            conn.execute("UPDATE books SET rights='Changed during encode' WHERE id='cc0-1'")
        return np.eye(1, 384, dtype=np.float32)

    monkeypatch.setattr(overlay, "_encode_query", changed)
    result = overlay.overlay_search(installed.live, "ключ", "semantic", "cc0-0", "ru", 5)
    assert result and result["mode"] == "lexical" and result["semantic_coverage"]["active"] is False


def test_output_limit_does_not_change_prefix_or_worker_candidate_pool(installed: SimpleNamespace) -> None:
    small = overlay.overlay_search(installed.live, "архив", "hybrid", None, "ru", 1)
    large = overlay.overlay_search(installed.live, "архив", "hybrid", None, "ru", 50)
    assert small and large
    assert small["results"] == large["results"][:1]
    assert small["candidate_pool"] == large["candidate_pool"]


def test_worker_timeout_kills_owned_child_and_releases_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        pid = 991234
        returncode = None
        killed = False

        def communicate(self, input: Any = None, timeout: float | None = None) -> tuple[bytes, bytes]:
            if self.killed:
                return b"", b""
            raise subprocess.TimeoutExpired("owned fake worker", 0.01)

        def poll(self) -> int | None:
            return -9 if self.killed else None

        def kill(self) -> None:
            self.killed = True

    process = Process()
    monkeypatch.setattr(overlay.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(overlay, "WORKER_TIMEOUT", -1)
    with pytest.raises(TimeoutError, match="exceeded15s"):
        overlay._run_worker({"operation": "owned timeout fixture"})
    assert process.killed and not overlay._WORKERS
    assert overlay._SLOT.acquire(timeout=0)
    overlay._SLOT.release()


def test_no_native_or_torch_import_from_ordinary_module_and_worker_failure_is_finite() -> None:
    assert "faiss" not in sys.modules
    response = subprocess.run(
        [sys.executable, "-m", "book_rag.semantic_overlay", "--worker"],
        input=json.dumps({"operation": "invalid", "installation": "/owned-nonexistent-fixture"}),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert json.loads(response.stdout)["ok"] is False


def test_installation_target_can_reserve_lock_but_denies_dml_ddl_and_write_pragmas(tmp_path: Path) -> None:
    state = seed_pilot(tmp_path)
    before = file_sha256(state.live.db_path)
    target = overlay.InstallationTarget(state.live.data_dir)
    with target.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert conn.execute("SELECT count(*) FROM books").fetchone()[0] == 2
        assert conn.execute("PRAGMA schema_version").fetchone()[0] > 0
        for sql in (
            "UPDATE books SET title='forbidden'",
            "CREATE TABLE forbidden(id)",
            "PRAGMA user_version=999",
            "DELETE FROM passages",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                conn.execute(sql)
    assert file_sha256(state.live.db_path) == before


def test_model_weight_bytes_and_postencode_stamps_are_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = seed_pilot(tmp_path)
    from book_rag import encoder_ipc, semantic

    monkeypatch.setattr(encoder_ipc, "model_snapshot", lambda cache: (tmp_path, [state.weight]))
    monkeypatch.setattr(semantic, "_model_cache", lambda library: tmp_path)
    fake = SimpleNamespace(max_seq_length=128, encode=lambda *args, **kwargs: np.eye(1, 384, dtype=np.float32))
    monkeypatch.setattr(semantic, "_model", lambda *args: fake)
    assert overlay._encode_query(state.live, "ключ", state.space).shape == (1, 384)
    state.weight.write_bytes(b"Changed cached weight bytes")
    with pytest.raises(ValueError, match="model bytes"):
        overlay._encode_query(state.live, "ключ", state.space)
    state.space["model_files_sha256"] = [file_sha256(state.weight)]

    def changed(*args: Any, **kwargs: Any) -> np.ndarray:
        state.weight.write_bytes(b"Changed during encode")
        return np.eye(1, 384, dtype=np.float32)

    fake.encode = changed
    with pytest.raises(ValueError, match="changed during encoding"):
        overlay._encode_query(state.live, "ключ", state.space)


@pytest.mark.skipif(importlib.util.find_spec("faiss") is None, reason="Optional FAISS wheel is absent")
def test_real_native_worker_isolated_query_matches_live_exact_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = seed_pilot(tmp_path)
    # Native training/build happen only in a fresh child. These are deliberately
    # fake CC0 embeddings; this test measures protocol correctness, not quality.
    child = r"""
import json,sys,os
from pathlib import Path
import numpy as np
from book_rag.experimental_ivfpq import PQConfig,faiss_backend,train_codec,file_sha256
from book_rag.incremental_ivfpq import build_incremental
from book_rag.semantic_pilot import ReadOnlyLibrary,manifest_windows
root=Path(sys.argv[1]);old=json.loads((root/'report.json').read_text());old_dir=Path(old['resumed_phase']['result']['directory']);space=json.loads((old_dir/'descriptor.json').read_text())['model_space']
backend=faiss_backend();backend.omp_set_num_threads(2)
rng=np.random.default_rng(19);training=rng.normal(size=(640,384)).astype('float32');training/=np.linalg.norm(training,axis=1)[:,None]
codec=train_codec(training,PQConfig(dimensions=384,nlist=4,subquantizers=24,bits=4,iterations=2))
codecpath=root/'real-codec.faiss';backend.write_index(codec,str(codecpath))
records=list(manifest_windows(root/'windows.sqlite3'));vectors=training[:len(records)]
source=ReadOnlyLibrary(root/'canonical');receipt=json.loads((root/'windows.sqlite3.receipt.json').read_text())
result=build_incremental(
 source,records,lambda rows:vectors[[r.window_id for r in rows]],root/'real-native',
 codecpath,file_sha256(codecpath),space,receipt['rows_by_book'],chunk_rows=2,shard_rows=4)
(root/'query.json').write_text(json.dumps(vectors[0].tolist()))
(root/'report.json').write_text(json.dumps({'resumed_phase':{'result':result}}))
"""
    env = os.environ.copy()
    env["BOOKRAG_IVFPQ_WORKER"] = "1"
    subprocess.run(
        [sys.executable, "-c", child, str(state.pilot)], env=env, capture_output=True, text=True, timeout=30, check=True
    )
    before = file_sha256(state.live.db_path)
    overlay.prepare_overlay(state.live, state.pilot, state.destination)
    assert file_sha256(state.live.db_path) == before
    monkeypatch.setenv("BOOKRAG_SEMANTIC_OVERLAY", str(state.destination / "installation.json"))
    vector = np.array([json.loads((state.pilot / "query.json").read_text())], dtype=np.float32)
    monkeypatch.setattr(overlay, "_encode_query", lambda *args: vector)
    result = overlay.overlay_search(state.live, "ключ", "semantic", "cc0-0", "ru", 5)
    assert result and result["mode"] == "semantic" and result["results"]
    assert result["results"][0]["book_id"] == "cc0-0"
    assert all(r["verified"] and r["semantic_score_approximate"] for r in result["results"])
    assert "faiss" not in sys.modules
    cli_destination = tmp_path / "actual-cli-installation"
    with state.live.connect() as conn:
        schema_before = conn.execute("PRAGMA schema_version").fetchone()[0]
    cli = subprocess.run(
        [
            sys.executable,
            "scripts/install_semantic_overlay.py",
            "--library",
            str(state.live.data_dir),
            "--pilot",
            str(state.pilot),
            "--destination",
            str(cli_destination),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(cli.stdout)["enabled_automatically"] is False
    assert (cli_destination / "installation.json").is_file()
    assert file_sha256(state.live.db_path) == before
    with state.live.connect() as conn:
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == schema_before
