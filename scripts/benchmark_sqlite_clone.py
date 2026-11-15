"""Probe guarded copy-on-write SQLite capture on owned synthetic CC0 files only.

This script never opens a production database. The reusable capture helper does
not initialize Library, open SQLite on the source, checkpoint it, or chmod it.
Callers must operationally quiesce writers; observed main/sidecar drift is rejected.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import platform
import random
import sqlite3
import stat
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

MIB = 1024 * 1024
SIDECARS = ("-wal", "-shm", "-journal")
UNSUPPORTED = {errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV}


class CaptureRejected(RuntimeError):
    """A stable, standalone SQLite capture could not be proved."""


def _fd_stamp(descriptor: int) -> dict[str, int]:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise CaptureRejected("Only regular source and target files are supported")
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "mode": stat.S_IMODE(info.st_mode),
    }


def _fd_sha256(descriptor: int, expected_bytes: int) -> str:
    checksum = hashlib.sha256()
    offset = 0
    while offset < expected_bytes:
        block = os.pread(descriptor, min(MIB, expected_bytes - offset), offset)
        if not block:
            raise CaptureRejected("File truncated while hashing")
        checksum.update(block)
        offset += len(block)
    if os.pread(descriptor, 1, expected_bytes):
        raise CaptureRejected("File grew while hashing")
    return checksum.hexdigest()


def _record(path: Path, descriptor: int | None = None) -> dict[str, Any]:
    owned_descriptor = descriptor is None
    if descriptor is None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = _fd_stamp(descriptor)
        checksum = _fd_sha256(descriptor, before["bytes"])
        after = _fd_stamp(descriptor)
        named = path.stat(follow_symlinks=False)
        if before != after or (named.st_dev, named.st_ino) != (after["device"], after["inode"]):
            raise CaptureRejected("Source or target identity changed while hashing")
        return after | {"sha256": checksum}
    finally:
        if owned_descriptor:
            os.close(descriptor)


def _source_state(source: Path, descriptor: int) -> dict[str, Any]:
    # Check pending transactions before spending time hashing the main file.
    for suffix in ("-wal", "-journal"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.exists() and sidecar.stat(follow_symlinks=False).st_size:
            raise CaptureRejected(f"Nonempty SQLite {suffix} requires quiescing/checkpointing by its owner")
    result: dict[str, Any] = {"main": _record(source, descriptor)}
    for suffix in SIDECARS:
        sidecar = source.with_name(source.name + suffix)
        result[suffix] = _record(sidecar) if sidecar.exists() else None
    return result


def _native_clone(source_descriptor: int, target_directory: Path, target_name: str) -> None:
    """Use the descriptor form of macOS clonefile to avoid source-path races."""
    if sys.platform != "darwin":
        raise OSError(errno.ENOTSUP, "macOS clonefile is unavailable on this platform")
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "fclonefileat", None)
    if function is None:
        raise OSError(errno.ENOSYS, "fclonefileat is unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    function.restype = ctypes.c_int
    directory_descriptor = os.open(target_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        if function(source_descriptor, directory_descriptor, os.fsencode(target_name), 0) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    finally:
        os.close(directory_descriptor)


def _budgeted_copy(source_descriptor: int, destination: Path, byte_budget: int) -> None:
    initial = _fd_stamp(source_descriptor)
    if byte_budget <= 0 or initial["bytes"] > byte_budget:
        raise CaptureRejected("Copy fallback exceeds the explicitly authorized byte budget")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while block := os.pread(source_descriptor, min(MIB, byte_budget - offset + 1), offset):
            if offset + len(block) > byte_budget:
                raise CaptureRejected("Source grew beyond the copy fallback byte budget")
            view = memoryview(block)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "Short write during budgeted copy")
                view = view[written:]
            offset += len(block)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def capture_sqlite(source: Path, destination: Path, *, copy_budget_bytes: int = 0) -> dict[str, Any]:
    """Publish a byte-identical standalone capture, failing closed on observed drift.

    No source SQLite connection or write is made. A quiesced source with no pending
    WAL/rollback journal is required. Unsupported native cloning only permits a
    physical copy when the caller explicitly authorizes enough bytes. Existing
    destinations are never replaced. The destination receives a fresh inode.
    """
    started = time.perf_counter()
    if type(copy_budget_bytes) is not int or copy_budget_bytes < 0:
        raise ValueError("copy_budget_bytes must be a nonnegative integer")
    if source.is_symlink():
        raise CaptureRejected("A symlink source is not supported")
    source = source.resolve(strict=True)
    if not stat.S_ISREG(source.stat(follow_symlinks=False).st_mode):
        raise CaptureRejected("Only a regular source file is supported")
    destination = destination.parent.resolve(strict=True) / destination.name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    source_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    temporary_directory: Path | None = None
    try:
        if os.pread(source_descriptor, 16, 0) != b"SQLite format 3\x00":
            raise CaptureRejected("Source is not a SQLite 3 database")
        before = _source_state(source, source_descriptor)
        temporary_directory = Path(tempfile.mkdtemp(prefix=".sqlite-capture-", dir=destination.parent))
        staged = temporary_directory / "capture.sqlite3"
        operation_started = time.perf_counter()
        method = "macos-fclonefileat"
        try:
            _native_clone(source_descriptor, temporary_directory, staged.name)
        except OSError as error:
            if error.errno not in UNSUPPORTED:
                raise CaptureRejected(f"Native clone failed with errno {error.errno}; copy fallback refused") from error
            if not copy_budget_bytes:
                raise CaptureRejected("Native cloning unavailable; no physical copy authorized") from error
            # A native implementation is expected to be atomic. Clean any owned
            # failed target before the explicit fallback, including mocked tests.
            staged.unlink(missing_ok=True)
            method = "explicit-budgeted-copy"
            _budgeted_copy(source_descriptor, staged, copy_budget_bytes)
        operation_ms = (time.perf_counter() - operation_started) * 1000
        cloned = _record(staged)
        original = before["main"]
        if (cloned["device"], cloned["inode"]) == (original["device"], original["inode"]):
            raise CaptureRejected("A snapshot must have its own inode; hardlinks are rejected")
        if (cloned["bytes"], cloned["sha256"]) != (original["bytes"], original["sha256"]):
            raise CaptureRejected("Captured bytes do not match the guarded source")
        try:
            with closing(sqlite3.connect(f"{staged.as_uri()}?mode=ro&immutable=1", uri=True)) as connection:
                connection.execute("PRAGMA query_only=ON")
                if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise CaptureRejected("Captured SQLite integrity check failed")
        except sqlite3.Error as error:
            raise CaptureRejected("Captured SQLite integrity check could not complete") from error
        after = _source_state(source, source_descriptor)
        if before != after:
            raise CaptureRejected("Source main/sidecars changed during capture")
        # Directory and file are ours. Changing the capture mode enables future
        # owned migrations without changing the source's permissions.
        staged.chmod(0o600)
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        # Hardlink only from our verified staging file, never from the source.
        # link() publishes without replacing a destination created concurrently.
        os.link(staged, destination)
        staged.unlink()
        return {
            "method": method,
            "bytes": cloned["bytes"],
            "sha256": cloned["sha256"],
            "source_before": before,
            "source_after": after,
            "source_unchanged": before == after,
            "target_device": cloned["device"],
            "target_inode": cloned["inode"],
            "new_inode": True,
            "sqlite_integrity": "ok",
            "copy_budget_bytes": copy_budget_bytes,
            "native_or_copy_operation_ms": operation_ms,
            "guarded_capture_ms": (time.perf_counter() - started) * 1000,
        }
    finally:
        os.close(source_descriptor)
        if temporary_directory is not None:
            for path in temporary_directory.iterdir():
                path.unlink()
            temporary_directory.rmdir()


def make_synthetic_database(destination: Path, size_mib: int) -> None:
    """Create only invented CC0 data with a deterministic pseudorandom payload."""
    if destination.exists():
        raise FileExistsError(destination)
    generator = random.Random(20261115)
    with closing(sqlite3.connect(destination)) as connection, connection:
        connection.execute("CREATE TABLE license(name TEXT, declaration TEXT)")
        connection.execute("INSERT INTO license VALUES('CC0-1.0','All records are invented benchmark data.')")
        connection.execute("CREATE TABLE synthetic_docs(id INTEGER PRIMARY KEY,title TEXT,text TEXT,padding BLOB)")
        rows = size_mib * MIB // 4096
        connection.executemany(
            "INSERT INTO synthetic_docs VALUES(?,?,?,?)",
            (
                (i + 1, f"Synthetic CC0 document {i + 1}", "Invented storage-probe text.", generator.randbytes(3500))
                for i in range(rows)
            ),
        )


def _mutate_owned_database(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE synthetic_docs SET text=? WHERE id=1", (value,))


def run_experiment(output: Path, size_mib: int, copy_budget_bytes: int = 0) -> dict[str, Any]:
    """Create a new owned directory, capture it, and test both COW directions."""
    if not 10 <= size_mib <= 50:
        raise ValueError("Synthetic experiment size must be between 10 and 50 MiB")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / "synthetic-cc0-source.sqlite3"
    target = output / "synthetic-cc0-capture.sqlite3"
    make_synthetic_database(source, size_mib)
    receipt = capture_sqlite(source, target, copy_budget_bytes=copy_budget_bytes)
    initial_source = _record(source)
    _mutate_owned_database(target, "Target-only mutation of invented data.")
    target_changed = _record(target)
    source_after_target = _record(source)
    if initial_source != source_after_target or target_changed["sha256"] == initial_source["sha256"]:
        raise CaptureRejected("Target mutation was not isolated from the source")
    _mutate_owned_database(source, "Source-only mutation of invented data.")
    source_changed = _record(source)
    target_after_source = _record(target)
    if target_changed != target_after_source or source_changed["sha256"] == initial_source["sha256"]:
        raise CaptureRejected("Source mutation was not isolated from the capture")
    report = {
        "experiment": "sqlite-clone-v1",
        "synthetic_only": True,
        "license": "CC0-1.0",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "requested_payload_mib": size_mib,
        "capture": receipt,
        "isolation": {
            "target_mutation_preserves_source": True,
            "source_mutation_preserves_target": True,
            "initial_source_sha256": initial_source["sha256"],
            "source_after_target_mutation_sha256": source_after_target["sha256"],
            "mutated_target_sha256": target_changed["sha256"],
            "target_after_source_mutation_sha256": target_after_source["sha256"],
            "mutated_source_sha256": source_changed["sha256"],
        },
        "limitations": [
            "Only owned synthetic data was measured; no production/frozen corpus was read or cloned.",
            "Callers must quiesce source writers; hash/sidecar guards are not a SQLite online-backup replacement.",
            "Native clonefile success plus isolation verifies COW behavior; "
            "physical shared-block accounting was not measured.",
            "Source SHA scans and target integrity checks are included in guarded time; "
            "only the clone/copy syscall stage is timed separately.",
            "Later COW writes can need disk space; successful cloning does not reserve future write capacity.",
        ],
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/bench/sqlite-clone-v1"))
    parser.add_argument("--size-mib", type=int, default=20)
    parser.add_argument("--copy-budget-mib", type=int, default=0)
    arguments = parser.parse_args()
    if arguments.copy_budget_mib < 0:
        parser.error("Copy budget must be nonnegative")
    report = run_experiment(arguments.output, arguments.size_mib, arguments.copy_budget_mib * MIB)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
