"""Ready-only subset activation, exact input reconciliation and failure isolation."""

from __future__ import annotations

import errno
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from test_semantic import FakeModel, FakeTokenizer

from book_rag import index_installation as installation
from book_rag import semantic
from book_rag.index_coverage import validate_coverage
from book_rag.index_installation import install_partial_index, read_installation_receipt
from book_rag.library import Library
from book_rag.vector_reuse import KEYMAP, RECEIPT, GenerationReuse, ModelSpace


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def folder(library: Library) -> Path:
    active = json.loads((library.data_dir / "vectors" / "active.json").read_text())
    return library.data_dir / "vectors" / active["generation"]


def seal_changed_checkpoint(source: Library, *, vectors_changed: bool = False) -> None:
    generation = folder(source)
    checkpoint = json.loads((generation / "checkpoint.json").read_text())
    if vectors_changed:
        values = np.load(generation / "embeddings.npy", mmap_mode="r")
        checkpoint["vector_checksum"] = hashlib.sha256(values.tobytes()).hexdigest()
        values._mmap.close()
    (generation / "checkpoint.json").write_text(json.dumps(checkpoint))
    receipt = json.loads((generation / RECEIPT).read_text())
    receipt["checkpoint_sha256"] = file_hash(generation / "checkpoint.json")
    if vectors_changed:
        receipt["vector_checksum"] = checkpoint["vector_checksum"]
    (generation / RECEIPT).write_text(json.dumps(receipt))


def seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, duplicates: bool = False, model_name: str | None = None
) -> tuple[Library, Library, FakeModel]:
    source = Library(tmp_path / "source")
    text = "echo " * 1000 if duplicates else " ".join(f"unique{index:04d}" for index in range(450))
    path = tmp_path / "source.txt"
    path.write_text(text)
    source.import_book(
        path, {"id": "covered", "title": "Source title", "author": "Original CC0 author", "language": "en"}
    )
    model = FakeModel()
    monkeypatch.setattr(semantic, "_model", lambda *args, **kwargs: model)
    semantic.build_index(source, model_name=model_name, batch_size=3)
    target_root = tmp_path / "target"
    target_root.mkdir()
    with source.connect() as origin, sqlite3.connect(target_root / "library.sqlite3") as destination:
        origin.backup(destination)
    target = Library(target_root)
    other = tmp_path / "outside.txt"
    other.write_text("Additional edition remains available through lexical search outside semantic coverage.")
    target.import_book(other, {"id": "outside", "title": "Outside title", "author": "CC0 fixture", "language": "en"})
    semantic.build_index(target, model_name=model_name, batch_size=3)
    monkeypatch.setattr(installation, "_load_tokenizer", lambda *args: FakeTokenizer())
    return source, target, model


def source_hashes(source: Library) -> dict[str, str]:
    paths = [source.db_path, source.data_dir / "vectors" / "active.json"]
    paths += [folder(source) / name for name in installation.GENERATION_FILES]
    return {str(path): file_hash(path) for path in paths}


def assert_old_active(target: Library, original: bytes) -> None:
    assert (target.data_dir / "vectors" / "active.json").read_bytes() == original
    assert target.search("outside", mode="semantic")["mode"] == "semantic"
    assert not list(target.data_dir.glob(".install-input-proof-*"))
    assert not list((target.data_dir / "vectors").glob(".install-generation-*"))


def test_all_unique_ready_generation_installs_hardlinks_as_explicit_subset_without_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, model = seed(tmp_path, monkeypatch)
    before, calls = source_hashes(source), model.calls
    installed = install_partial_index(source.data_dir, target.data_dir)
    assert installed["coverage_kind"] == "subset" and installed["coverage"]["covered_book_ids"] == ["covered"]
    assert installed["coverage"]["books"] == 1 and target.stats()["books"] == 2
    assert target.search("additional", mode="lexical")["results"][0]["book_id"] == "outside"
    receipt = read_installation_receipt(target.data_dir, installed)
    assert receipt["source_input_proof"]["kind"] == "recorded_unique_equals_window_stream"
    assert receipt["source_input_proof"]["windows"] == installed["vectors"]
    assert set(receipt["file_methods"].values()) == {"hardlink"}
    for name in installation.GENERATION_FILES:
        assert (folder(source) / name).stat().st_ino == (
            target.data_dir / "vectors" / installed["generation"] / name
        ).stat().st_ino
    assert source_hashes(source) == before and model.calls == calls
    assert validate_coverage(target, installed["coverage"], installed["coverage_binding"])["valid"]
    again = install_partial_index(source.data_dir, target.data_dir)
    assert again["generation"] == installed["generation"]
    assert set(read_installation_receipt(target.data_dir, again)["file_methods"].values()) == {"existing"}


def test_schema2_identical_duplicate_rows_are_reconciled_without_claiming_original_full_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _ = seed(tmp_path, monkeypatch, duplicates=True)
    before = source_hashes(source)
    installed = install_partial_index(source.data_dir, target.data_dir)
    receipt = read_installation_receipt(target.data_dir, installed)
    proof = receipt["source_input_proof"]
    assert proof["kind"] == "schema2_reconciled_exact" and proof["duplicate_rows"] > 0
    assert proof["different_duplicate_rows"] == 0 and not proof["requires_materialization"]
    assert installed["generation"] == folder(source).name and source_hashes(source) == before


def test_different_duplicate_payload_is_rebuilt_only_from_verified_first_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, model = seed(tmp_path, monkeypatch, duplicates=True)
    generation = folder(source)
    vectors = np.load(generation / "embeddings.npy", mmap_mode="r+")
    # A tiny floating difference is still not a bitwise proof; tolerance is forbidden.
    with sqlite3.connect(generation / KEYMAP) as conn:
        first_indices = {row[0] for row in conn.execute("SELECT row_index FROM window_keys")}
    duplicate = next(index for index in range(len(vectors)) if index not in first_indices)
    original = vectors[duplicate].copy()
    vectors[duplicate, 0] += np.float32(1e-7)
    vectors[duplicate] /= np.linalg.norm(vectors[duplicate])
    assert np.allclose(vectors[duplicate], original) and vectors[duplicate].tobytes() != original.tobytes()
    vectors.flush()
    vectors._mmap.close()
    seal_changed_checkpoint(source, vectors_changed=True)
    before, calls = source_hashes(source), model.calls
    installed = install_partial_index(source.data_dir, target.data_dir)
    receipt = read_installation_receipt(target.data_dir, installed)
    assert installed["generation"] != generation.name and installed["encoded_vectors"] == 0
    assert installed["reused_vectors"] == installed["vectors"] and model.calls == calls
    assert receipt["source_input_proof"]["kind"] == "schema2_reused_first_rows"
    assert receipt["file_methods"]["embeddings.npy"] == "materialized-exact-reuse"
    ready = target.data_dir / "vectors" / installed["generation"]
    rebuilt = np.load(ready / "embeddings.npy", mmap_mode="r")
    assert rebuilt[duplicate].tobytes() == original.tobytes()
    rebuilt._mmap.close()
    checkpoint = json.loads((ready / "checkpoint.json").read_text())
    assert checkpoint["window_input_checksum"] == receipt["source_input_proof"]["window_input_checksum"]
    with GenerationReuse.open(ready, ModelSpace.from_metadata(installed, normalized=True)):
        pass
    assert source_hashes(source) == before
    again = install_partial_index(source.data_dir, target.data_dir)
    assert again["generation"] == installed["generation"]
    assert set(read_installation_receipt(target.data_dir, again)["file_methods"].values()) == {"existing"}


@pytest.mark.parametrize("failure", ["append", "truncate", "dtype", "checksum", "forged-membership"])
def test_ids_are_independently_validated_even_when_reuse_vectors_are_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    original = (target.data_dir / "vectors" / "active.json").read_bytes()
    generation = folder(source)
    path = generation / "ids.npy"
    if failure == "append":
        with path.open("ab") as stream:
            stream.write(b"unclaimed trailing bytes")
    elif failure == "truncate":
        path.write_bytes(path.read_bytes()[:-8])
    else:
        ids = np.load(path)
        if failure == "dtype":
            np.save(path, ids.astype(np.int32))
        else:
            ids[0] = 999999
            np.save(path, ids)
            if failure == "forged-membership":
                checkpoint = json.loads((generation / "checkpoint.json").read_text())
                checkpoint["id_checksum"] = hashlib.sha256(ids.tobytes()).hexdigest()
                (generation / "checkpoint.json").write_text(json.dumps(checkpoint))
                seal_changed_checkpoint(source)
    with pytest.raises((ValueError, OSError)):
        install_partial_index(source.data_dir, target.data_dir)
    assert_old_active(target, original)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE passages SET text=text||' corrupted'",
        "UPDATE sections SET text=text||' changed'",
        "UPDATE books SET title='Header changed after embedding'",
        "UPDATE sections SET title='Chapter header changed after embedding'",
        "UPDATE passages SET start=start+1",
    ],
)
def test_source_actual_text_headers_and_slices_are_proved_not_just_claimed_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statement: str
) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    original = (target.data_dir / "vectors" / "active.json").read_bytes()
    with source.connect() as conn:
        conn.execute(statement)
    with pytest.raises(ValueError):
        install_partial_index(source.data_dir, target.data_dir)
    assert_old_active(target, original)


def test_target_actual_inputs_and_persisted_epoch_binding_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    installed = install_partial_index(source.data_dir, target.data_dir)
    original = (target.data_dir / "vectors" / "active.json").read_bytes()
    with target.connect() as conn:
        conn.execute("UPDATE passages SET text=text||' bad' WHERE book_id='covered'")
    assert not validate_coverage(target, installed["coverage"], installed["coverage_binding"])["valid"]
    with pytest.raises(ValueError, match="stored SHA256"):
        install_partial_index(source.data_dir, target.data_dir)
    assert (target.data_dir / "vectors" / "active.json").read_bytes() == original


@pytest.mark.parametrize("filename", [KEYMAP, RECEIPT])
def test_legacy_generation_missing_ready_proof_is_rejected_before_target_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    original = (target.data_dir / "vectors" / "active.json").read_bytes()
    before = set(target.data_dir.rglob("*"))
    (folder(source) / filename).unlink()
    with pytest.raises((OSError, ValueError)):
        install_partial_index(source.data_dir, target.data_dir)
    assert set(target.data_dir.rglob("*")) == before
    assert_old_active(target, original)


def test_staging_and_symlink_sources_are_never_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    original = (target.data_dir / "vectors" / "active.json").read_bytes()
    link = tmp_path / "source-alias"
    link.symlink_to(source.data_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        install_partial_index(link, target.data_dir)
    active = source.data_dir / "vectors" / "active.json"
    value = json.loads(active.read_text())
    value["generation"] = "staging-incomplete"
    active.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="staging"):
        install_partial_index(source.data_dir, target.data_dir)
    assert_old_active(target, original)


def test_copy_fallback_and_interruption_keep_old_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    original = (target.data_dir / "vectors" / "active.json").read_bytes()
    copier = installation._copy_or_link

    def interrupted(*args: Any, **kwargs: Any) -> str:
        raise KeyboardInterrupt("Owned installation interrupted")

    monkeypatch.setattr(installation, "_copy_or_link", interrupted)
    with pytest.raises(KeyboardInterrupt):
        install_partial_index(source.data_dir, target.data_dir)
    assert_old_active(target, original)
    monkeypatch.setattr(installation, "_copy_or_link", copier)
    original_link = installation.os.link

    def cross_device(source_path: Any, destination: Any, **kwargs: Any) -> None:
        if Path(destination).name in installation.GENERATION_FILES:
            raise OSError(errno.EXDEV, "Controlled cross-device fixture")
        original_link(source_path, destination, **kwargs)

    monkeypatch.setattr(installation.os, "link", cross_device)
    installed = install_partial_index(source.data_dir, target.data_dir)
    assert set(read_installation_receipt(target.data_dir, installed)["file_methods"].values()) == {"copy"}


def test_activation_fsync_failure_restores_original_pointer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    active = target.data_dir / "vectors" / "active.json"
    original = active.read_bytes()
    synchronizer = installation._fsync_directory
    raised = False

    def failed(path: Path) -> None:
        nonlocal raised
        if path == active.parent and b'"coverage_kind":"subset"' in active.read_bytes() and not raised:
            raised = True
            raise OSError("Controlled directory fsync failure")
        synchronizer(path)

    monkeypatch.setattr(installation, "_fsync_directory", failed)
    with pytest.raises(OSError, match="fsync"):
        install_partial_index(source.data_dir, target.data_dir)
    assert raised
    assert_old_active(target, original)
    assert not list(active.parent.glob(".install-active-*")) and not list(active.parent.glob(".install-previous-*"))


def test_persisted_receipt_rejects_changed_files_and_source_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    installed = install_partial_index(source.data_dir, target.data_dir)
    path = target.data_dir / "vectors" / installed["generation"] / "ids.npy"
    # Replace only the target name, leaving a hardlinked immutable source untouched.
    replacement = path.with_suffix(".replacement")
    shutil.copyfile(path, replacement)
    replacement.replace(path)
    with pytest.raises(ValueError, match="file changed"):
        read_installation_receipt(target.data_dir, installed)
    with source.connect() as conn:
        conn.execute("CREATE TABLE source_pending_wal_fixture(value TEXT)")
        conn.commit()
        with pytest.raises(ValueError, match="pending WAL"):
            install_partial_index(source.data_dir, target.data_dir)


def test_local_tokenizer_loading_never_requests_weights_network_or_remote_code(monkeypatch: pytest.MonkeyPatch) -> None:
    arguments: dict[str, Any] = {}

    class Tokenizers:
        @staticmethod
        def from_pretrained(*args: Any, **kwargs: Any) -> str:
            arguments.update(kwargs)
            return "tokenizer-only"

    class Package:
        AutoTokenizer = Tokenizers

    monkeypatch.setattr(installation.importlib, "import_module", lambda name: Package)
    space = ModelSpace("test/model", "a" * 40, 4, 64, '{"name":"fixture"}', True)
    assert installation._load_tokenizer(space, Path("owned-cache")) == "tokenizer-only"
    assert arguments["local_files_only"] is True and arguments["trust_remote_code"] is False
    assert arguments["revision"] == "a" * 40


def test_resealed_wrong_first_row_map_does_not_prove_actual_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    original = (target.data_dir / "vectors" / "active.json").read_bytes()
    generation = folder(source)
    with sqlite3.connect(generation / KEYMAP) as conn:
        rows = list(conn.execute("SELECT key,row_index FROM window_keys ORDER BY row_index"))
        swapped = [(rows[0][0], rows[1][1]), (rows[1][0], rows[0][1]), *rows[2:]]
        conn.execute("DELETE FROM window_keys")
        conn.executemany("INSERT INTO window_keys VALUES (?,?)", swapped)
        checksum = hashlib.sha256()
        for key, index in sorted(swapped, key=lambda item: item[1]):
            checksum.update(key + index.to_bytes(8, "big"))
        conn.execute("UPDATE build_state SET key_checksum=?", (checksum.hexdigest(),))
    checkpoint = json.loads((generation / "checkpoint.json").read_text())
    checkpoint["key_checksum"] = checksum.hexdigest()
    (generation / "checkpoint.json").write_text(json.dumps(checkpoint))
    receipt = json.loads((generation / RECEIPT).read_text())
    receipt.update(
        key_checksum=checksum.hexdigest(),
        keymap_sha256=file_hash(generation / KEYMAP),
        checkpoint_sha256=file_hash(generation / "checkpoint.json"),
    )
    (generation / RECEIPT).write_text(json.dumps(receipt))
    # GenerationReuse validates the internally consistent bytes. Installation
    # additionally proves that their first rows match the actual model inputs.
    with GenerationReuse.open(
        generation, ModelSpace.from_metadata(json.loads((generation / "metadata.json").read_text()), normalized=True)
    ):
        pass
    with pytest.raises(ValueError, match="input|first row"):
        install_partial_index(source.data_dir, target.data_dir)
    assert_old_active(target, original)


def test_final_epoch_guard_rejects_drift_after_strict_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    original = (target.data_dir / "vectors" / "active.json").read_bytes()
    binder = installation.bind_coverage

    def changed(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = binder(*args, **kwargs)
        with target.connect() as conn:
            conn.execute("UPDATE books SET title='Changed during install' WHERE id='covered'")
        return result

    monkeypatch.setattr(installation, "bind_coverage", changed)
    with pytest.raises(ValueError, match="before activation"):
        install_partial_index(source.data_dir, target.data_dir)
    assert_old_active(target, original)
    assert not list((target.data_dir / "vectors" / "installations").glob("*.json"))


def test_symlink_active_and_payload_are_rejected_without_following_owned_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    active = target.data_dir / "vectors" / "active.json"
    original = active.read_bytes()
    sentinel = tmp_path / "owned-sentinel.json"
    sentinel.write_bytes(b'{"owned":"do-not-touch"}')
    active.unlink()
    active.symlink_to(sentinel)
    with pytest.raises(ValueError, match="regular file"):
        install_partial_index(source.data_dir, target.data_dir)
    assert active.is_symlink() and sentinel.read_bytes() == b'{"owned":"do-not-touch"}'
    active.unlink()
    active.write_bytes(original)
    payload = folder(source) / "embeddings.npy"
    separate = tmp_path / "owned-vector-sentinel.npy"
    payload.rename(separate)
    payload.symlink_to(separate)
    with pytest.raises(ValueError, match="regular file"):
        install_partial_index(source.data_dir, target.data_dir)
    assert_old_active(target, original)


def test_shared_database_inode_and_model_space_drift_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    original = (target.data_dir / "vectors" / "active.json").read_bytes()
    generation = folder(source)
    active = source.data_dir / "vectors" / "active.json"
    metadata = json.loads(active.read_text())
    metadata["embedding_profile"]["query_prefix"] = "unverified-prefix: "
    active.write_text(json.dumps(metadata))
    (generation / "metadata.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError):
        install_partial_index(source.data_dir, target.data_dir)
    assert_old_active(target, original)
    target.db_path.unlink()
    installation.os.link(source.db_path, target.db_path)
    before = file_hash(source.db_path)
    with pytest.raises(ValueError, match="independent"):
        install_partial_index(source.data_dir, target.data_dir)
    assert file_hash(source.db_path) == before


def test_persisted_receipt_digest_and_locators_cannot_be_silently_rebound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    installed = install_partial_index(source.data_dir, target.data_dir)
    path = target.data_dir / "vectors" / installed["installation_receipt"]["path"]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="checksum"):
        read_installation_receipt(target.data_dir, installed)
    forged = installed | {"installation_receipt": {"path": "../../outside.json", "sha256": "a" * 64}}
    with pytest.raises(ValueError, match="locator"):
        read_installation_receipt(target.data_dir, forged)


_REAL_SOURCE = Path(__file__).resolve().parents[1] / "data/reuse-experiment/incremental-v2"
_MODEL_CACHE = Path(__file__).resolve().parents[1] / "data/models"


@pytest.mark.skipif(
    not (_REAL_SOURCE / "vectors/active.json").is_file() or not _MODEL_CACHE.is_dir(),
    reason="Optional completed local reuse experiment and pinned tokenizer cache are absent",
)
def test_available_real_ready_generation_only_installs_into_owned_tmp(tmp_path: Path) -> None:
    active = json.loads((_REAL_SOURCE / "vectors/active.json").read_text())
    generation = _REAL_SOURCE / "vectors" / active["generation"]
    files = [_REAL_SOURCE / "library.sqlite3", _REAL_SOURCE / "vectors/active.json"]
    files += [generation / name for name in installation.GENERATION_FILES]
    before = {str(path): file_hash(path) for path in files}
    source_entries = set(_REAL_SOURCE.rglob("*"))
    root = tmp_path / "owned-real-target"
    root.mkdir()
    shutil.copyfile(_REAL_SOURCE / "library.sqlite3", root / "library.sqlite3")
    target = Library(root)  # Only the owned target copy is migrated.
    extra = tmp_path / "extra.txt"
    extra.write_text("A separate CC0 edition outside the imported semantic subset.")
    target.import_book(extra, {"id": "outside-owned-fixture", "title": "Owned CC0 extra"})
    installed = install_partial_index(_REAL_SOURCE, root, model_cache=_MODEL_CACHE)
    receipt = read_installation_receipt(root, installed)
    assert receipt["source_input_proof"]["windows"] == 190 and installed["vectors"] == 190
    assert target.stats()["books"] > installed["coverage"]["books"]
    assert validate_coverage(target, installed["coverage"], installed["coverage_binding"])["valid"]
    assert {str(path): file_hash(path) for path in files} == before
    assert set(_REAL_SOURCE.rglob("*")) == source_entries


def test_source_wal_appearing_during_input_proof_prevents_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    original = (target.data_dir / "vectors" / "active.json").read_bytes()
    prover = installation._input_proof
    writers: list[sqlite3.Connection] = []

    def changed(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = prover(*args, **kwargs)
        writer = sqlite3.connect(source.db_path)
        writer.execute("CREATE TABLE source_wal_during_proof(value TEXT)")
        writer.commit()
        writers.append(writer)
        return result

    monkeypatch.setattr(installation, "_input_proof", changed)
    try:
        with pytest.raises(ValueError, match="pending WAL"):
            install_partial_index(source.data_dir, target.data_dir)
        assert_old_active(target, original)
    finally:
        for writer in writers:
            writer.close()


@pytest.mark.parametrize(
    "field,value",
    [("vectors", 999), ("passages", 999), ("strategy", "unverified-strategy"), ("corpus_fingerprint", "f" * 64)],
)
def test_fast_receipt_guard_checks_every_sealed_base_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    installed = install_partial_index(source.data_dir, target.data_dir)
    with pytest.raises(ValueError, match="sealed metadata"):
        read_installation_receipt(target.data_dir, installed | {field: value})


@pytest.mark.parametrize("copy_phase", [False, True])
def test_disk_reserve_prevents_scratch_or_copy_allocation_and_keeps_old_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, copy_phase: bool
) -> None:
    from types import SimpleNamespace

    source, target, _ = seed(tmp_path, monkeypatch)
    original = (target.data_dir / "vectors" / "active.json").read_bytes()
    count = json.loads((folder(source) / "metadata.json").read_text())["vectors"]
    free = installation.DISK_RESERVE_BYTES + count * 8 + 257 if copy_phase else 0
    monkeypatch.setattr(installation.shutil, "disk_usage", lambda path: SimpleNamespace(free=free))
    with pytest.raises(OSError) as error:
        install_partial_index(source.data_dir, target.data_dir, hardlink=not copy_phase)
    assert error.value.errno == errno.ENOSPC
    assert_old_active(target, original)


def test_frozen_e5_prefix_space_is_used_even_if_current_environment_selects_another_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from book_rag.embedding_profiles import E5_SMALL_MODEL, MINILM_MODEL

    source, target, _ = seed(tmp_path, monkeypatch, model_name=E5_SMALL_MODEL)
    monkeypatch.setenv("BOOKRAG_EMBEDDING_MODEL", MINILM_MODEL)
    monkeypatch.setenv("BOOKRAG_EMBEDDING_PROFILE", "minilm-v2")
    installed = install_partial_index(source.data_dir, target.data_dir)
    assert installed["model"] == E5_SMALL_MODEL
    assert installed["embedding_profile"]["document_prefix"] == "passage: "
    assert installed["embedding_profile"]["query_prefix"] == "query: "
    assert (
        read_installation_receipt(target.data_dir, installed)["model_space"]["embedding_profile"]
        == installed["embedding_profile"]
    )


def test_receipt_guard_rejects_symlink_metadata_before_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _ = seed(tmp_path, monkeypatch)
    installed = install_partial_index(source.data_dir, target.data_dir)
    metadata_path = target.data_dir / "vectors" / installed["generation"] / "metadata.json"
    sentinel = tmp_path / "owned-private-sentinel.json"
    sentinel.write_text('{"owned":"must-not-be-read"}')
    metadata_path.unlink()
    metadata_path.symlink_to(sentinel)
    reader = Path.read_text

    def guarded(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == metadata_path:
            pytest.fail("Symlink metadata must be rejected before any read")
        return reader(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    with pytest.raises(ValueError, match="regular file"):
        read_installation_receipt(target.data_dir, installed)
