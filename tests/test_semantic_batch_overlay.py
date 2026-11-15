"""Independent direct-live CC0 registry checks; native work is subprocess-only."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from book_rag import semantic_batch_overlay as overlay
from book_rag.embedding_profiles import MINILM_MODEL, MINILM_PROFILE, MINILM_REVISION
from book_rag.experimental_ivfpq import file_sha256
from book_rag.incremental_ivfpq import _shard_name, digest
from book_rag.index_coverage import bind_coverage
from book_rag.library import Library
from book_rag.semantic_batches import prepare_batch_windows
from book_rag.semantic_overlay import InstallationTarget, _SnapshotLibrary
from book_rag.semantic_pilot import ReadOnlyLibrary, manifest_windows


class Tokenizer:
    def encode(self, text: str, **kwargs: Any) -> list[int]:
        return [ord(char) for char in text]

    def decode(self, values: list[int], **kwargs: Any) -> str:
        return "".join(chr(value) for value in values)

    def __call__(self, text: str, **kwargs: Any) -> dict[str, Any]:
        return {"offset_mapping": [(index, index + 1) for index in range(len(text))]}


def write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False))


def seed_batch(root: Path) -> SimpleNamespace:
    from book_rag.semantic import _windows, token_windows

    live = Library(root / "live")
    for identity, language, text in (
        ("cc0-fr", "fr", "La clef bleue repose dans la tasse verte. Le gardien vérifie le registre du phare."),
        (
            "cc0-de",
            "de",
            "Der silberne Schlüssel liegt unter der Brücke. Die Wächterin notiert den ruhigen Wasserstand.",
        ),
        ("cc0-outside", "en", "An uncovered original archive contains an amber key. This is an independent CC0 story."),
    ):
        source = root / (identity + ".txt")
        source.write_text(text + "\n\n" + text)
        live.import_book(
            source,
            {
                "id": identity,
                "title": identity,
                "author": "CC0 test author",
                "language": language,
                "edition": "Owned CC0 edition",
                "coverage": "full",
                "rights": "Own CC0-1.0 text",
                "source_url": "https://example.invalid/" + identity,
            },
        )
    weight = root / "fake-weight"
    weight.write_bytes(b"Owned deterministic fake encoder, no neural weights")
    space = {
        "model": MINILM_MODEL,
        "revision": MINILM_REVISION,
        "dimensions": 384,
        "profile": MINILM_PROFILE.metadata(MINILM_REVISION),
        "strategy": "token-windows-v2",
        "assignment": "UNIT TEST ONLY: fake CC0 vectors; no retrieval-quality claim",
        "model_files_sha256": [file_sha256(weight)],
        "window_recipe_sha256": hashlib.sha256(
            (inspect.getsource(_windows) + inspect.getsource(token_windows)).encode()
        ).hexdigest(),
    }
    batch = root / "batch"
    batch.mkdir()
    manifest = prepare_batch_windows(
        live.data_dir,
        batch / "windows.sqlite3",
        ["cc0-fr", "cc0-de"],
        SimpleNamespace(tokenizer=Tokenizer(), max_seq_length=128),
        space,
        reserve_bytes=0,
    )
    records = list(manifest_windows(batch / "windows.sqlite3"))
    generation = batch / "fixture-generation"
    generation.mkdir()
    (generation / "codec.faiss").write_bytes(b"Owned fake codec, real native test replaces this artifact")
    descriptor = {
        "schema_version": 1,
        "kind": "experimental-incremental-IVFPQ",
        "dimensions": 384,
        "nlist": 4,
        "codec_implementation": "faiss-cpu-1.16.0",
        "standalone_code_bytes": 13,
        "chunk_rows": len(records),
        "shard_rows": len(records),
        "rows": len(records),
        "rows_by_book": manifest["rows_by_book"],
        "model_space": space,
        "model_space_sha256": digest(space),
        "covered_book_ids": ["cc0-de", "cc0-fr"],
        "build_source_token": manifest["descriptor"]["source_state"]["state_token"],
        "source_coverage": manifest["descriptor"]["source_state"]["coverage"],
        "input_order": "ascending-passage-id-with-contiguous-windows",
        "codec_sha256": file_sha256(generation / "codec.faiss"),
    }
    descriptor["identity"] = digest(descriptor)
    write(generation / "descriptor.json", descriptor)
    name = _shard_name(0, len(records))
    (generation / (name + ".faiss")).write_bytes(b"Owned native placeholder for pure tests only")
    books = descriptor["covered_book_ids"]
    np.savez(
        generation / (name + ".mapping.npz"),
        passages=np.array([r.passage_id for r in records], dtype=np.int64),
        books=np.array([books.index(r.book_id) for r in records], dtype=np.uint16),
    )
    write(generation / "chunk-00000000.json", {"kind": "Owned fake chunk receipt"})
    receipt = {
        "schema_version": 1,
        "identity": descriptor["identity"],
        "start": 0,
        "end": len(records),
        "model_space_sha256": digest(space),
        "coverage": descriptor["source_coverage"],
        "chunks": [{"number": 0, "receipt_sha256": file_sha256(generation / "chunk-00000000.json")}],
        "index_sha256": file_sha256(generation / (name + ".faiss")),
        "mapping_sha256": file_sha256(generation / (name + ".mapping.npz")),
    }
    write(generation / (name + ".receipt.json"), receipt)
    receipt_sha = file_sha256(generation / (name + ".receipt.json"))
    original_binding = bind_coverage(ReadOnlyLibrary(live.data_dir), receipt["coverage"], receipt_sha256=receipt_sha)
    write(generation / (name + ".binding.json"), original_binding)
    ready = {
        "schema_version": 1,
        "identity": descriptor["identity"],
        "rows": len(records),
        "coverage_kind": "explicit-subset",
        "descriptor_sha256": file_sha256(generation / "descriptor.json"),
        "model_space_sha256": digest(space),
        "source_passage_coverage_verified": True,
        "source_passage_fingerprint": descriptor["source_coverage"]["passage_fingerprint"],
        "input_chain": manifest["input_chain"],
        "chunk_chain": "0" * 64,
        "shards": [
            {
                "start": 0,
                "end": len(records),
                "covered_book_ids": books,
                "receipt_sha256": receipt_sha,
                "binding_sha256": file_sha256(generation / (name + ".binding.json")),
            }
        ],
    }
    write(generation / "ready.json", ready)
    final_generation = batch / "compressed" / ("incremental-" + descriptor["identity"])
    final_generation.parent.mkdir()
    generation.rename(final_generation)
    generation = final_generation
    result = {
        "complete": True,
        "directory": str(generation),
        "identity": descriptor["identity"],
        "durable_rows": len(records),
        "ready_sha256": file_sha256(generation / "ready.json"),
        "created_chunks": 1,
        "resumed_chunks": 0,
    }
    report = {
        "kind": "actual-complete-edition-semantic-batch",
        "status": "complete",
        "source": str(live.data_dir),
        "source_unchanged": True,
        "activated": False,
        "closed_final_used": False,
        "codec_sha256": descriptor["codec_sha256"],
        "codec_report_sha256": "a" * 64,
        "manifest": {key: manifest[key] for key in ("rows", "rows_by_book", "input_chain", "database_sha256")},
        "encoder": {
            "model_space": space,
            "model_files_unchanged": True,
            "faiss_imported": False,
            "rows_encoded": len(records),
            "encode_requests": 1,
        },
        "native": {"ok": True, "result": result, "torch_imported": False, "peak_rss_bytes": 1},
        "encoded_rows_this_run": len(records),
        "durable_rows_before": 0,
        "resources": {"owned_output_bytes": 1, "free_disk_bytes": 10 * 1024**3},
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
    }
    write(batch / "report.json", report)
    with live.connect() as conn:
        candidates = [
            {"passage_id": row[0], "book_id": row[1], "score": 0.8 - n * 0.05, "window_id": n}
            for n, row in enumerate(
                conn.execute("SELECT id,book_id FROM passages WHERE book_id IN ('cc0-fr','cc0-de') ORDER BY id")
            )
        ]
    return SimpleNamespace(
        live=live,
        batch=batch,
        generation=generation,
        manifest=manifest,
        report=report,
        destination=root / "registry",
        space=space,
        candidates=candidates,
    )


def fake_worker(state: SimpleNamespace, request: dict[str, Any]) -> dict[str, Any]:
    if request.get("operation") != "query":
        return {"validated": True, "torch_imported": False}
    candidates = [hit for hit in state.candidates if not request.get("book_id") or hit["book_id"] == request["book_id"]]
    return {"results": candidates, "fallback_required": False, "source_changed": False}


@pytest.fixture
def registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = seed_batch(tmp_path)
    monkeypatch.setattr(overlay, "_run_worker", lambda request: fake_worker(state, request))
    monkeypatch.setattr(overlay, "_encode_query", lambda *args: np.eye(1, 384, dtype=np.float32))
    state.registry = overlay.install_batch_registry(state.live, [state.batch], state.destination)
    monkeypatch.setenv("BOOKRAG_SEMANTIC_BATCH_REGISTRY", str(state.destination / "registry.json"))
    return state


def test_registry_preserves_original_direct_live_binding_and_artifact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = seed_batch(tmp_path)
    source_before = file_sha256(state.live.db_path)
    artifacts_before = {p: file_sha256(p) for p in state.generation.iterdir() if p.is_file()}
    monkeypatch.setattr(overlay, "_run_worker", lambda request: fake_worker(state, request))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Already live-bound source must never be rebound")

    import book_rag.index_coverage as coverage

    monkeypatch.setattr(coverage, "bind_coverage", forbidden)
    if hasattr(overlay, "bind_coverage"):
        monkeypatch.setattr(overlay, "bind_coverage", forbidden)
    overlay.install_batch_registry(InstallationTarget(state.live.data_dir), [state.batch], state.destination)
    assert source_before == file_sha256(state.live.db_path)
    assert artifacts_before == {p: file_sha256(p) for p in state.generation.iterdir() if p.is_file()}
    assert (state.destination / "registry.json").is_file()
    assert not list(state.destination.rglob("*.faiss")) and not list(state.destination.rglob("*.sqlite3"))
    with pytest.raises(FileExistsError):
        overlay.install_batch_registry(state.live, [state.batch], state.destination)


@pytest.mark.parametrize(
    "book,language", [(None, None), (None, "fr"), (None, "de"), (None, "ru"), ("cc0-outside", None)]
)
def test_global_language_and_uncovered_queries_leave_existing_search_untouched(
    registered: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, book: str | None, language: str | None
) -> None:
    monkeypatch.setattr(overlay, "_encode_query", lambda *args: pytest.fail("No batch model outside explicit coverage"))
    assert overlay.batch_overlay_search(registered.live, "clef", "semantic", book, language, 5) is None


def test_registry_does_not_redirect_controlled_copy_with_identical_passage_ids(
    registered: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = tmp_path / "control"
    control.mkdir()
    shutil.copyfile(registered.live.db_path, control / "library.sqlite3")
    library = ReadOnlyLibrary(control)
    monkeypatch.setattr(overlay, "_encode_query", lambda *args: pytest.fail("Do not load overlay for another DB"))
    assert overlay.batch_overlay_search(library, "clef", "semantic", "cc0-fr", "fr", 5) is None


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_explicit_book_uses_exact_live_evidence_and_honest_partial_coverage(
    registered: SimpleNamespace, mode: str
) -> None:
    result = overlay.batch_overlay_search(registered.live, "clef", mode, "cc0-fr", "fr", 5)
    assert result and result["mode"] == mode and result["results"]
    assert all(hit["verified"] and hit["book_id"] == "cc0-fr" and hit["language"] == "fr" for hit in result["results"])
    assert result["semantic_coverage"]["indexed_books"] == 2
    assert result["semantic_coverage"]["searchable_books"] == 3
    assert result["semantic_coverage"]["requested_book_covered"] is True
    assert result["warnings"]


def test_conflicting_language_filter_never_returns_wrong_scope(registered: SimpleNamespace) -> None:
    result = overlay.batch_overlay_search(registered.live, "clef", "semantic", "cc0-fr", "de", 5)
    assert result is None or not result["results"]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE books SET title='Changed' WHERE id='cc0-de'",
        "UPDATE books SET edition='Different acquired edition' WHERE id='cc0-de'",
        "UPDATE books SET rights='Changed terms' WHERE id='cc0-de'",
        "UPDATE books SET artifact_sha256='changed' WHERE id='cc0-de'",
        "UPDATE section_roles SET section_translator='Changed translator'",
        "UPDATE book_metadata SET metadata='{}' WHERE book_id='cc0-de'",
        "UPDATE index_versions SET fingerprint='changed' WHERE book_id='cc0-de'",
        "UPDATE passages SET text=text||' tampered' WHERE book_id='cc0-de'",
        "UPDATE book_index_epoch_meta SET namespace='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
    ],
)
def test_any_covered_body_or_untracked_provenance_change_fails_entire_batch(
    registered: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, statement: str
) -> None:
    with registered.live.connect() as conn:
        conn.execute(statement)
    monkeypatch.setattr(overlay, "_encode_query", lambda *args: pytest.fail("Changed source must fail before model"))
    result = overlay.batch_overlay_search(registered.live, "clef", "semantic", "cc0-fr", "fr", 5)
    assert result and result["mode"] == "lexical" and result["warnings"]
    assert all(hit["verified"] and hit["book_id"] == "cc0-fr" for hit in result["results"])


def test_unrelated_book_and_failed_refresh_do_not_invalidate_successful_source(registered: SimpleNamespace) -> None:
    with registered.live.connect() as conn:
        conn.execute("UPDATE books SET title='Unrelated new title' WHERE id='cc0-outside'")
    registered.live.stage("cc0-fr", "failed", "Owned failed refresh; old edition remains exact")
    result = overlay.batch_overlay_search(registered.live, "clef", "semantic", "cc0-fr", "fr", 5)
    assert result and result["mode"] == "semantic" and result["results"]


@pytest.mark.parametrize(
    "change",
    ["status", "missing_source_check", "weights", "native_torch", "close_error", "encoder_space", "encoder_faiss"],
)
def test_ready_artifact_without_successful_final_report_is_not_registerable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    state = seed_batch(tmp_path)
    report = state.report
    if change == "status":
        report["status"] = "failed"
    elif change == "missing_source_check":
        report["source_unchanged"] = False
    elif change == "weights":
        report["encoder"]["model_files_unchanged"] = False
    elif change == "native_torch":
        report["native"]["torch_imported"] = True
    elif change == "encoder_space":
        report["encoder"]["model_space"] = {"model": "different-space"}
    elif change == "encoder_faiss":
        report["encoder"]["faiss_imported"] = True
    else:
        report["encoder_close_error"] = "Owned final verification failure"
    write(state.batch / "report.json", report)
    monkeypatch.setattr(overlay, "_run_worker", lambda request: pytest.fail("Bad finalization must fail before native"))
    with pytest.raises((ValueError, RuntimeError)):
        overlay.install_batch_registry(state.live, [state.batch], state.destination)
    assert not (state.destination / "registry.json").exists()


def test_staging_or_overlapping_batches_cannot_be_registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = seed_batch(tmp_path)
    monkeypatch.setattr(overlay, "_run_worker", lambda request: pytest.fail("Bad input fails before native"))
    with pytest.raises(ValueError, match="Duplicate"):
        overlay.install_batch_registry(state.live, [state.batch, state.batch], state.destination)
    (state.batch / "report.json").unlink()
    with pytest.raises((ValueError, OSError)):
        overlay.install_batch_registry(state.live, [state.batch], state.destination)
    assert not state.destination.exists()


CONTAINER_CASES = [
    ("report", ("native",)),
    ("report", ("native", "result")),
    ("report", ("encoder",)),
    ("manifest", ("descriptor",)),
    ("manifest", ("descriptor", "source_state")),
    ("manifest", ("descriptor", "model_space")),
    ("descriptor", ("model_space",)),
]


def corrupt_container(state: SimpleNamespace, file_kind: str, fields: tuple[str, ...], replacement: Any) -> None:
    path = (
        state.batch / "report.json"
        if file_kind == "report"
        else state.batch / "windows.sqlite3.receipt.json"
        if file_kind == "manifest"
        else state.generation / "descriptor.json"
    )
    value = json.loads(path.read_text())
    parent = value
    for field in fields[:-1]:
        parent = parent[field]
    parent[fields[-1]] = replacement
    write(path, value)


@pytest.mark.parametrize("file_kind,fields", CONTAINER_CASES)
@pytest.mark.parametrize("replacement", [None, []])
def test_corrupt_registered_container_falls_back_before_encoding(
    registered: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    file_kind: str,
    fields: tuple[str, ...],
    replacement: Any,
) -> None:
    corrupt_container(registered, file_kind, fields, replacement)
    monkeypatch.setattr(
        overlay, "_encode_query", lambda *args: pytest.fail("Corrupt containers must fail before encoding")
    )
    result = overlay.batch_overlay_search(registered.live, "clef", "semantic", "cc0-fr", "fr", 5)
    assert result and result["mode"] == "lexical" and result["warnings"]
    assert all(hit["verified"] and hit["book_id"] == "cc0-fr" for hit in result["results"])


@pytest.mark.parametrize("file_kind,fields", CONTAINER_CASES)
@pytest.mark.parametrize("replacement", [None, []])
def test_bad_container_install_is_rejected_without_publishing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_kind: str, fields: tuple[str, ...], replacement: Any
) -> None:
    state = seed_batch(tmp_path)
    corrupt_container(state, file_kind, fields, replacement)
    monkeypatch.setattr(
        overlay, "_run_worker", lambda request: pytest.fail("Corrupt containers fail before native work")
    )
    with pytest.raises((ValueError, RuntimeError)):
        overlay.install_batch_registry(state.live, [state.batch], state.destination)
    assert not state.destination.exists()


@pytest.mark.parametrize("filename", ["ready.json", "descriptor.json", "codec.faiss"])
def test_corrupt_generation_is_rejected_before_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    state = seed_batch(tmp_path)
    (state.generation / filename).write_bytes(b"Owned corrupted artifact")
    monkeypatch.setattr(overlay, "_run_worker", lambda request: pytest.fail("Corruption must fail before native"))
    with pytest.raises((ValueError, RuntimeError, KeyError)):
        overlay.install_batch_registry(state.live, [state.batch], state.destination)


def test_quoted_constraints_and_output_limit_prefixes_are_preserved(registered: SimpleNamespace) -> None:
    absent = overlay.batch_overlay_search(registered.live, '"silberne Schlüssel"', "semantic", "cc0-fr", "fr", 5)
    assert absent and not absent["results"]
    small = overlay.batch_overlay_search(registered.live, "clef", "hybrid", "cc0-fr", "fr", 1)
    large = overlay.batch_overlay_search(registered.live, "clef", "hybrid", "cc0-fr", "fr", 50)
    assert small and large and small["results"] == large["results"][:1]


@pytest.mark.parametrize("bad", ["missing", "owner", "nan", "duplicate"])
def test_invalid_native_hits_fail_closed_against_live_evidence(
    registered: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    candidate = dict(next(hit for hit in registered.candidates if hit["book_id"] == "cc0-fr"))
    if bad == "missing":
        candidate["passage_id"] = 999999
    elif bad == "owner":
        candidate["book_id"] = "cc0-de"
    elif bad == "nan":
        candidate["score"] = float("nan")
    values = [candidate, candidate] if bad == "duplicate" else [candidate]
    monkeypatch.setattr(overlay, "_run_worker", lambda request: {"results": values, "fallback_required": False})
    result = overlay.batch_overlay_search(registered.live, "clef", "semantic", "cc0-fr", "fr", 5)
    assert result and result["mode"] == "lexical" and all(hit["verified"] for hit in result["results"])


def test_provenance_change_during_encoding_is_observed_after_worker(
    registered: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def changed(*args: Any) -> np.ndarray:
        with registered.live.connect() as conn:
            conn.execute("UPDATE books SET edition='New during encoding' WHERE id='cc0-fr'")
        return np.eye(1, 384, dtype=np.float32)

    monkeypatch.setattr(overlay, "_encode_query", changed)
    result = overlay.batch_overlay_search(registered.live, "clef", "semantic", "cc0-fr", "fr", 5)
    assert result and result["mode"] == "lexical"
    assert all(hit["edition"] == "New during encoding" for hit in result["results"])


def test_fresh_guard_catches_untracked_metadata_written_during_hydration(
    registered: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    passages = _SnapshotLibrary.passages

    def changed(self: Library, identities: list[int]) -> list[dict[str, Any]]:
        results = passages(self, identities)
        with registered.live.connect() as conn:
            conn.execute("UPDATE books SET edition='Changed after source read' WHERE id='cc0-fr'")
        return results

    monkeypatch.setattr(_SnapshotLibrary, "passages", changed)
    result = overlay.batch_overlay_search(registered.live, "clef", "semantic", "cc0-fr", "fr", 5)
    assert result and result["mode"] == "lexical"
    assert all(hit["edition"] == "Changed after source read" for hit in result["results"])


def test_registry_swap_during_encoding_discards_candidates(
    registered: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = registered.destination / "registry.json"

    def changed(*args: Any) -> np.ndarray:
        value = json.loads(path.read_text())
        value["owned_test_swap"] = True
        write(path, value)
        return np.eye(1, 384, dtype=np.float32)

    monkeypatch.setattr(overlay, "_encode_query", changed)
    result = overlay.batch_overlay_search(registered.live, "clef", "semantic", "cc0-fr", "fr", 5)
    assert result and result["mode"] == "lexical"


def test_worker_monitor_subprocess_timeout_is_an_explicit_word_fallback(
    registered: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(request: dict[str, Any]) -> dict[str, Any]:
        raise subprocess.TimeoutExpired("owned monitoring command", 2)

    monkeypatch.setattr(overlay, "_run_worker", failed)
    result = overlay.batch_overlay_search(registered.live, "clef", "semantic", "cc0-fr", "fr", 5)
    assert result and result["mode"] == "lexical" and result["warnings"]


@pytest.mark.skipif(importlib.util.find_spec("faiss") is None, reason="Optional native FAISS is unavailable")
def test_real_direct_live_native_registry_and_cli_preserve_source_and_existing_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = seed_batch(tmp_path)
    child = r"""
import json,sys
from pathlib import Path
import numpy as np
from book_rag.experimental_ivfpq import PQConfig,faiss_backend,train_codec,file_sha256
from book_rag.incremental_ivfpq import build_incremental
from book_rag.semantic_pilot import ReadOnlyLibrary,manifest_windows
root=Path(sys.argv[1]);live=Path(sys.argv[2]);report=json.loads((root/'report.json').read_text())
manifest=json.loads((root/'windows.sqlite3.receipt.json').read_text())
space=manifest['descriptor']['model_space'];backend=faiss_backend();backend.omp_set_num_threads(2)
rng=np.random.default_rng(27);training=rng.normal(size=(640,384)).astype('float32')
training/=np.linalg.norm(training,axis=1)[:,None]
codec=train_codec(training,PQConfig(dimensions=384,nlist=4,subquantizers=24,bits=4,iterations=2))
codecpath=root/'native-codec.faiss';backend.write_index(codec,str(codecpath))
records=list(manifest_windows(root/'windows.sqlite3'));vectors=training[:len(records)]
result=build_incremental(ReadOnlyLibrary(live),records,lambda rows:vectors[[row.window_id for row in rows]],
 root/'compressed',codecpath,file_sha256(codecpath),space,manifest['rows_by_book'],chunk_rows=2,shard_rows=4)
report['native']['result']=result;report['codec_sha256']=file_sha256(codecpath)
(root/'report.json').write_text(json.dumps(report));(root/'query.json').write_text(json.dumps(vectors[0].tolist()))
"""
    env = os.environ.copy()
    env["BOOKRAG_IVFPQ_WORKER"] = "1"
    subprocess.run(
        [sys.executable, "-c", child, str(state.batch), str(state.live.data_dir)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    generation = Path(json.loads((state.batch / "report.json").read_text())["native"]["result"]["directory"])
    before = {path: file_sha256(path) for path in generation.iterdir() if path.is_file()}
    database_sha = file_sha256(state.live.db_path)
    with state.live.connect() as conn:
        schema = conn.execute("PRAGMA schema_version").fetchone()[0]
    cli = subprocess.run(
        [
            sys.executable,
            "scripts/install_semantic_batch_registry.py",
            "--library",
            str(state.live.data_dir),
            "--batch",
            str(state.batch),
            "--destination",
            str(state.destination),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(cli.stdout)["enabled_automatically"] is False
    monkeypatch.setenv("BOOKRAG_SEMANTIC_BATCH_REGISTRY", str(state.destination / "registry.json"))
    vector = np.array([json.loads((state.batch / "query.json").read_text())], dtype=np.float32)
    monkeypatch.setattr(overlay, "_encode_query", lambda *args: vector)
    result = overlay.batch_overlay_search(state.live, "clef", "semantic", "cc0-fr", "fr", 5)
    assert result and result["mode"] == "semantic" and result["results"]
    assert all(hit["verified"] and hit["book_id"] == "cc0-fr" for hit in result["results"])
    assert database_sha == file_sha256(state.live.db_path)
    with state.live.connect() as conn:
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == schema
    assert before == {path: file_sha256(path) for path in generation.iterdir() if path.is_file()}
    assert "faiss" not in sys.modules
