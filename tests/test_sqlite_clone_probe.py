"""The clone experiment touches only owned synthetic CC0 SQLite fixtures."""

from __future__ import annotations

import errno
import importlib.util
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

SPEC = importlib.util.spec_from_file_location(
    "sqlite_clone_probe", Path(__file__).resolve().parents[1] / "scripts/benchmark_sqlite_clone.py"
)
assert SPEC is not None and SPEC.loader is not None
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def _seed(path: Path, value: str = "Original CC0 invented text.") -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE synthetic_docs(id INTEGER PRIMARY KEY,text TEXT)")
        connection.execute("INSERT INTO synthetic_docs VALUES(1,?)", (value,))


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "owned-source.sqlite3"
    _seed(path)
    return path


def _copy_stub(source_descriptor: int, target_directory: Path, target_name: str) -> None:
    # The generic guard tests use an independent physical copy, not a claim that
    # the testing platform supplies APFS. A separate real native test follows.
    PROBE._budgeted_copy(source_descriptor, target_directory / target_name, PROBE.MIB)


def _unavailable(*args: Any) -> None:
    raise OSError(errno.ENOTSUP, "Synthetic unsupported filesystem")


def _assert_clean(parent: Path, target: Path) -> None:
    assert not target.exists()
    assert not list(parent.glob(".sqlite-capture-*"))


def test_source_bytes_mode_and_existing_shm_are_unchanged(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PROBE, "_native_clone", _copy_stub)
    shm = source.with_name(source.name + "-shm")
    shm.write_bytes(b"synthetic-existing-shm-sentinel")
    before = PROBE._record(source)
    shm_before = PROBE._record(shm)
    target = source.parent / "capture.sqlite3"
    result = PROBE.capture_sqlite(source, target)
    assert result["new_inode"] and result["source_unchanged"]
    assert result["sha256"] == before["sha256"] == PROBE._record(target)["sha256"]
    assert result["target_inode"] != before["inode"]
    assert PROBE._record(source) == before and PROBE._record(shm) == shm_before
    assert not list(source.parent.glob(".sqlite-capture-*"))


def test_linux_native_unavailable_is_fail_closed(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PROBE.sys, "platform", "linux")
    target = source.parent / "capture.sqlite3"
    before = PROBE._record(source)
    with pytest.raises(PROBE.CaptureRejected, match="no physical copy authorized"):
        PROBE.capture_sqlite(source, target)
    assert PROBE._record(source) == before
    _assert_clean(source.parent, target)


def test_explicit_budget_fallback_has_isolated_bytes(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PROBE, "_native_clone", _unavailable)
    target = source.parent / "capture.sqlite3"
    before = PROBE._record(source)
    result = PROBE.capture_sqlite(source, target, copy_budget_bytes=source.stat().st_size)
    assert result["method"] == "explicit-budgeted-copy"
    assert result["copy_budget_bytes"] == source.stat().st_size
    assert PROBE._record(source) == before
    PROBE._mutate_owned_database(target, "Changed target fixture.")
    assert PROBE._record(source) == before
    changed_target = PROBE._record(target)
    PROBE._mutate_owned_database(source, "Changed source fixture.")
    assert PROBE._record(target) == changed_target
    assert PROBE._record(source)["sha256"] != before["sha256"]


@pytest.mark.parametrize("budget", [1, 4096])
def test_copy_fallback_rejects_insufficient_budget(source: Path, monkeypatch: pytest.MonkeyPatch, budget: int) -> None:
    monkeypatch.setattr(PROBE, "_native_clone", _unavailable)
    target = source.parent / "capture.sqlite3"
    with pytest.raises(PROBE.CaptureRejected, match="byte budget"):
        PROBE.capture_sqlite(source, target, copy_budget_bytes=budget)
    _assert_clean(source.parent, target)


@pytest.mark.parametrize("error", [errno.EACCES, errno.EIO, errno.ENOSPC])
def test_other_native_errors_never_trigger_physical_copy(
    source: Path, monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    def native_failure(*args: Any) -> None:
        raise OSError(error, "Synthetic native failure")

    monkeypatch.setattr(PROBE, "_native_clone", native_failure)
    target = source.parent / "capture.sqlite3"
    with pytest.raises(PROBE.CaptureRejected, match="copy fallback refused"):
        PROBE.capture_sqlite(source, target, copy_budget_bytes=PROBE.MIB)
    _assert_clean(source.parent, target)


def test_pending_real_wal_is_rejected_before_native_call(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def should_not_clone(*args: Any) -> None:
        pytest.fail("Pending WAL must be rejected before native clone")

    monkeypatch.setattr(PROBE, "_native_clone", should_not_clone)
    target = source.parent / "capture.sqlite3"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE synthetic_docs SET text='Own pending WAL fixture' WHERE id=1")
        connection.commit()
        before = PROBE._record(source)
        wal = source.with_name(source.name + "-wal")
        wal_before = PROBE._record(wal)
        assert wal.stat().st_size > 0
        with pytest.raises(PROBE.CaptureRejected, match="Nonempty SQLite -wal"):
            PROBE.capture_sqlite(source, target)
        assert PROBE._record(source) == before and PROBE._record(wal) == wal_before
    _assert_clean(source.parent, target)


def test_nonempty_rollback_journal_is_rejected(source: Path) -> None:
    source.with_name(source.name + "-journal").write_bytes(b"owned-hot-journal-fixture")
    target = source.parent / "capture.sqlite3"
    with pytest.raises(PROBE.CaptureRejected, match="Nonempty SQLite -journal"):
        PROBE.capture_sqlite(source, target)
    _assert_clean(source.parent, target)


def test_main_drift_during_capture_rejects_and_cleans(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def mutate_after_clone(descriptor: int, directory: Path, name: str) -> None:
        _copy_stub(descriptor, directory, name)
        PROBE._mutate_owned_database(source, "Owned concurrent writer simulation.")

    monkeypatch.setattr(PROBE, "_native_clone", mutate_after_clone)
    target = source.parent / "capture.sqlite3"
    with pytest.raises(PROBE.CaptureRejected, match="changed during capture"):
        PROBE.capture_sqlite(source, target)
    _assert_clean(source.parent, target)


def test_replaced_source_inode_is_rejected(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replacement = source.parent / "owned-replacement.sqlite3"
    _seed(replacement, "Replacement fixture.")

    def replace_after_clone(descriptor: int, directory: Path, name: str) -> None:
        _copy_stub(descriptor, directory, name)
        os.replace(replacement, source)

    monkeypatch.setattr(PROBE, "_native_clone", replace_after_clone)
    target = source.parent / "capture.sqlite3"
    with pytest.raises(PROBE.CaptureRejected, match="identity changed"):
        PROBE.capture_sqlite(source, target)
    _assert_clean(source.parent, target)


def test_sidecar_appearing_after_clone_rejects_capture(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def add_wal_after_clone(descriptor: int, directory: Path, name: str) -> None:
        _copy_stub(descriptor, directory, name)
        source.with_name(source.name + "-wal").write_bytes(b"owned-new-wal-fixture")

    monkeypatch.setattr(PROBE, "_native_clone", add_wal_after_clone)
    target = source.parent / "capture.sqlite3"
    with pytest.raises(PROBE.CaptureRejected, match="Nonempty SQLite -wal"):
        PROBE.capture_sqlite(source, target)
    _assert_clean(source.parent, target)


def test_existing_destination_never_changes(source: Path) -> None:
    target = source.parent / "capture.sqlite3"
    target.write_bytes(b"owned-existing-destination-sentinel")
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        PROBE.capture_sqlite(source, target)
    assert target.read_bytes() == before


def test_publish_collision_does_not_overwrite_or_delete_other_file(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = source.parent / "capture.sqlite3"

    def collide_after_clone(descriptor: int, directory: Path, name: str) -> None:
        _copy_stub(descriptor, directory, name)
        target.write_bytes(b"owned-concurrent-destination")

    monkeypatch.setattr(PROBE, "_native_clone", collide_after_clone)
    with pytest.raises(FileExistsError):
        PROBE.capture_sqlite(source, target)
    assert target.read_bytes() == b"owned-concurrent-destination"
    assert not list(source.parent.glob(".sqlite-capture-*"))


def test_hardlinked_source_is_not_accepted_as_a_clone(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_hardlink(descriptor: int, directory: Path, name: str) -> None:
        os.link(source, directory / name)

    monkeypatch.setattr(PROBE, "_native_clone", fake_hardlink)
    target = source.parent / "capture.sqlite3"
    with pytest.raises(PROBE.CaptureRejected, match="own inode"):
        PROBE.capture_sqlite(source, target)
    _assert_clean(source.parent, target)


def test_partial_native_failure_is_cleaned(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def partial_failure(descriptor: int, directory: Path, name: str) -> None:
        (directory / name).write_bytes(b"owned-partial-native-result")
        raise OSError(errno.EIO, "Synthetic incomplete clone")

    monkeypatch.setattr(PROBE, "_native_clone", partial_failure)
    target = source.parent / "capture.sqlite3"
    with pytest.raises(PROBE.CaptureRejected):
        PROBE.capture_sqlite(source, target)
    _assert_clean(source.parent, target)


def test_symlink_source_is_rejected(source: Path) -> None:
    alias = source.parent / "owned-alias.sqlite3"
    alias.symlink_to(source)
    with pytest.raises(PROBE.CaptureRejected, match="symlink"):
        PROBE.capture_sqlite(alias, source.parent / "capture.sqlite3")


def test_corrupt_database_is_rejected_by_target_integrity(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with source.open("r+b") as stream:
        stream.seek(100)
        stream.write(b"\xff")
    monkeypatch.setattr(PROBE, "_native_clone", _copy_stub)
    target = source.parent / "capture.sqlite3"
    before = PROBE._record(source)
    with pytest.raises(PROBE.CaptureRejected, match="integrity"):
        PROBE.capture_sqlite(source, target)
    assert PROBE._record(source) == before
    _assert_clean(source.parent, target)


@pytest.mark.skipif(sys.platform != "darwin", reason="Native clonefile exists only on macOS")
def test_real_native_clone_has_bidirectional_cow_isolation(source: Path) -> None:
    target = source.parent / "capture.sqlite3"
    before = PROBE._record(source)
    try:
        result = PROBE.capture_sqlite(source, target)
    except PROBE.CaptureRejected as error:
        cause = error.__cause__
        if isinstance(cause, OSError) and cause.errno in PROBE.UNSUPPORTED:
            pytest.skip("Filesystem does not support clonefile")
        raise
    assert result["method"] == "macos-fclonefileat" and result["copy_budget_bytes"] == 0
    assert result["new_inode"] and result["sha256"] == before["sha256"]
    PROBE._mutate_owned_database(target, "Native target-only mutation.")
    assert PROBE._record(source) == before
    target_changed = PROBE._record(target)
    PROBE._mutate_owned_database(source, "Native source-only mutation.")
    assert PROBE._record(target) == target_changed
    assert PROBE._record(source)["sha256"] != before["sha256"]
