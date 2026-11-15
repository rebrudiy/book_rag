"""Runtime epoch proof and immutable-digest cache regressions on owned CC0 data."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_semantic_batch_overlay import seed_batch

from book_rag import semantic_batch_overlay as overlay


def guard(state: Any, *, full: bool = False) -> dict[str, Any]:
    with overlay._snapshot(state.live) as coherent:
        return overlay._source_guard(coherent, state.manifest, full=full)


def test_runtime_proof_cannot_read_canonical_bodies_but_registration_remains_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = seed_batch(tmp_path)
    full = overlay.batch_source_state
    calls = []

    def capture(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(True)
        return full(*args, **kwargs)

    monkeypatch.setattr(overlay, "batch_source_state", capture)
    assert guard(state, full=True) == state.manifest["descriptor"]["source_state"]
    assert calls == [True]
    with state.live.connect() as conn:
        conn.execute("BEGIN")

        def authorizer(action: int, table: str | None, column: str | None, *args: Any) -> int:
            if action == sqlite3.SQLITE_READ and table in {"sections", "passages"} and column == "text":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        coherent = overlay._SnapshotLibrary(state.live, conn) if hasattr(overlay, "_SnapshotLibrary") else None
        # _snapshot's adapter is intentionally shared with the existing overlay.
        if coherent is None:
            from book_rag.semantic_overlay import _SnapshotLibrary

            coherent = _SnapshotLibrary(state.live, conn)
        assert overlay._source_guard(coherent, state.manifest) == state.manifest["descriptor"]["source_state"]
    assert calls == [True], "Runtime must not invoke the full corpus scanner"


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE passages SET text=text||'changed' WHERE book_id='cc0-fr'",
        "UPDATE passages SET sha256='changed' WHERE book_id='cc0-fr'",
        "UPDATE passages SET start=start+1 WHERE book_id='cc0-fr'",
        "UPDATE passages SET end=end-1 WHERE book_id='cc0-fr'",
        "UPDATE passages SET id=id+10000 WHERE book_id='cc0-fr'",
        "UPDATE passages SET book_id='cc0-outside' WHERE book_id='cc0-fr'",
        "UPDATE passages SET section_id=(SELECT min(id) FROM sections WHERE book_id='cc0-de') WHERE book_id='cc0-fr'",
        "DELETE FROM passages WHERE book_id='cc0-fr'",
        "INSERT OR REPLACE INTO passages SELECT id,book_id,section_id,start,end,text,sha256 "
        "FROM passages WHERE book_id='cc0-fr'",
        "UPDATE sections SET text=text||'changed' WHERE book_id='cc0-fr'",
        "UPDATE sections SET title='Changed chapter' WHERE book_id='cc0-fr'",
        "UPDATE sections SET ordinal=ordinal+100 WHERE book_id='cc0-fr'",
        "UPDATE sections SET id=id+10000 WHERE book_id='cc0-fr'",
        "UPDATE sections SET book_id='cc0-outside',ordinal=ordinal+100 WHERE book_id='cc0-fr'",
        "UPDATE books SET title='Changed title' WHERE id='cc0-fr'",
        "UPDATE books SET author='Changed author' WHERE id='cc0-fr'",
        "UPDATE books SET language='ru' WHERE id='cc0-fr'",
        "UPDATE books SET status='failed' WHERE id='cc0-fr'",
        "UPDATE books SET id='changed-id' WHERE id='cc0-fr'",
    ],
)
def test_every_canonical_input_mutation_invalidates_epoch_proof(tmp_path: Path, sql: str) -> None:
    state = seed_batch(tmp_path)
    guard(state)
    with state.live.connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")  # Deliberately exercise direct SQL corruption, not import repair.
        conn.execute(sql)
    with pytest.raises(ValueError, match="changed"):
        guard(state)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE books SET edition='Changed edition' WHERE id='cc0-fr'",
        "UPDATE books SET rights='Changed rights' WHERE id='cc0-fr'",
        "UPDATE books SET rights_url='https://example.invalid/changed' WHERE id='cc0-fr'",
        "UPDATE books SET source_url='https://example.invalid/changed' WHERE id='cc0-fr'",
        "UPDATE books SET artifact_sha256='changed' WHERE id='cc0-fr'",
        "UPDATE books SET content_sha256='changed' WHERE id='cc0-fr'",
        "UPDATE book_metadata SET metadata='{}' WHERE book_id='cc0-fr'",
        "UPDATE index_versions SET fingerprint='changed' WHERE book_id='cc0-fr'",
        "UPDATE section_roles SET section_author='Changed contributor'",
        "UPDATE section_roles SET section_role='editorial'",
        "UPDATE section_roles SET section_translator='Changed translator'",
        "DELETE FROM section_roles",
    ],
)
def test_untracked_provenance_and_contributors_still_use_actual_hashes(tmp_path: Path, sql: str) -> None:
    state = seed_batch(tmp_path)
    guard(state)
    with state.live.connect() as conn:
        conn.execute(sql)
    with pytest.raises(ValueError, match="provenance changed"):
        guard(state)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TRIGGER bookrag_epoch_passages_update",
        "UPDATE book_index_epoch_meta SET namespace='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
        "UPDATE book_index_epochs SET revision=revision+1 WHERE book_id='cc0-fr'",
    ],
)
def test_epoch_namespace_schema_and_revision_remain_fail_closed(tmp_path: Path, sql: str) -> None:
    state = seed_batch(tmp_path)
    with state.live.connect() as conn:
        conn.execute(sql)
    with pytest.raises(ValueError):
        guard(state)


def test_unrelated_updates_and_failed_refresh_do_not_invalidate_runtime(tmp_path: Path) -> None:
    state = seed_batch(tmp_path)
    before = guard(state)
    state.live.stage("cc0-fr", "failed", "Owned failed refresh")
    with state.live.connect() as conn:
        conn.execute("UPDATE books SET title='Unrelated title' WHERE id='cc0-outside'")
    assert guard(state) == before


def test_digest_cache_detects_content_replacement_missing_and_symlink(tmp_path: Path) -> None:
    file = tmp_path / "owned-artifact"
    file.write_bytes(b"original")
    original = overlay.file_sha256(file)
    before = file.stat()
    file.read_bytes()  # An access-time update alone must not invalidate a cache hit.
    assert overlay.file_sha256(file) == original
    file.write_bytes(b"replaced")
    os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert overlay.file_sha256(file) != original, "Preserving size/mtime must not bypass ctime"
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"original")
    replacement.replace(file)
    assert overlay.file_sha256(file) == original
    file.unlink()
    with pytest.raises(ValueError):
        overlay.file_sha256(file)
    file.symlink_to(replacement)
    with pytest.raises(ValueError):
        overlay.file_sha256(file)


def test_cache_hits_do_not_rehash_and_changed_expected_sha_cannot_reuse_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "owned"
    file.write_bytes(b"owned fixture")
    calls = []
    raw = overlay._file_sha256

    def counted(path: Path) -> str:
        calls.append(path)
        return raw(path)

    monkeypatch.setattr(overlay, "_file_sha256", counted)
    digest = overlay.file_sha256(file)
    assert overlay.file_sha256(file) == digest and calls == [file]
    assert overlay.file_sha256(file) != "f" * 64  # Expected checksums remain caller-owned, never cached as True.
    for number in range(260):
        path = tmp_path / str(number)
        path.write_bytes(b"x")
        overlay.file_sha256(path)
    assert len(overlay._HASH_CACHE) <= overlay._HASH_CACHE_LIMIT


def test_artifact_replaced_during_hash_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "changing"
    file.write_bytes(b"before")

    def changed(path: Path) -> str:
        result = hashlib.sha256(path.read_bytes()).hexdigest()
        replacement = path.with_name("replacement")
        replacement.write_bytes(b"after!")
        replacement.replace(path)
        return result

    monkeypatch.setattr(overlay, "_file_sha256", changed)
    with pytest.raises(ValueError, match="while hashing"):
        overlay.file_sha256(file)
