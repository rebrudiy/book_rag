"""Content-addressed original files, published once and verified before reuse.

Only owned atomic-replacement download files may share an inode with an archive.
Arbitrary local files are copied so subsequent in-place edits cannot rewrite an
older edition. This preserves acquired bytes, not fidelity of a transcription.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import BinaryIO, TypedDict

MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".html", ".htm", ".xhtml", ".epub", ".fb2", ".pdf"})
_BLOCK = 1024 * 1024
_LINK_FALLBACK_ERRORS = {errno.EXDEV, errno.EPERM, errno.EACCES, errno.EMLINK, errno.ENOSYS, errno.ENOTSUP}


class ArtifactIntegrityError(ValueError):
    """An existing archive or an acquisition snapshot failed integrity checks."""


class SourceChangedError(ArtifactIntegrityError):
    """The mutable source changed while its original bytes were being archived."""


class ArchivedArtifact(TypedDict):
    path: Path
    sha256: str
    bytes: int
    method: str


def _sha256(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("SHA256 must contain exactly 64 hexadecimal characters")
    return value.lower()


def _size(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Artifact source must be a regular file")
    if not 0 < info.st_size <= MAX_ARTIFACT_BYTES:
        raise ValueError("Artifact source must contain 1 byte to 100 MiB")


def _stamp(info: os.stat_result, *, ctime: bool = True) -> tuple[int, ...]:
    values = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return (*values, info.st_ctime_ns) if ctime else values


def _digest(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(_BLOCK), b""):
        size += len(block)
        if size > MAX_ARTIFACT_BYTES:
            raise SourceChangedError("Artifact grew beyond the 100 MiB limit while reading")
        digest.update(block)
    return digest.hexdigest(), size


def _assert_source(source: Path, stream: BinaryIO, initial: os.stat_result, *, linked: bool = False) -> None:
    try:
        # Creating/removing our own hardlink changes ctime, but not source bytes,
        # inode identity, size or mtime. Payload hashes independently verify bytes.
        expected = _stamp(initial, ctime=not linked)
        if (
            _stamp(os.fstat(stream.fileno()), ctime=not linked) != expected
            or _stamp(source.stat(), ctime=not linked) != expected
        ):
            raise SourceChangedError("Source changed during artifact acquisition; retry from a stable source")
    except FileNotFoundError as exc:
        raise SourceChangedError("Source disappeared during artifact acquisition") from exc


def _archive_root(data_dir: Path, *, create: bool) -> Path | None:
    root = data_dir.expanduser().resolve() / "artifacts"
    if root.is_symlink():
        raise ArtifactIntegrityError("Artifact directory must not be a symlink")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.exists():
        return None
    if not root.is_dir():
        raise ArtifactIntegrityError("Artifact directory is not a directory")
    return root


def _shard(root: Path, sha256: str, *, create: bool) -> Path | None:
    shard = root / sha256[:2]
    if shard.is_symlink():
        raise ArtifactIntegrityError("Artifact shard must not be a symlink")
    if create:
        shard.mkdir(exist_ok=True)
    if not shard.exists():
        return None
    if not shard.is_dir():
        raise ArtifactIntegrityError("Artifact shard is not a directory")
    return shard


def _exists(path: Path) -> bool:
    # Broken symlinks must be treated as an existing unsafe archive, not replaced.
    return path.exists() or path.is_symlink()


def _verify(path: Path, sha256: str, expected_size: int | None = None) -> int:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ArtifactIntegrityError(f"Archive must be a regular nonsymlink file: {path}")
        try:
            _size(before)
        except ValueError as exc:
            raise ArtifactIntegrityError(f"Archive has invalid size: {path}") from exc
        with path.open("rb") as stream:
            if _stamp(os.fstat(stream.fileno()), ctime=False) != _stamp(before, ctime=False):
                raise ArtifactIntegrityError(f"Archive changed before verification: {path}")
            actual, size = _digest(stream)
            # Another concurrent publisher can add/remove hardlinks, changing
            # ctime while leaving the immutable payload unchanged.
            if _stamp(os.fstat(stream.fileno()), ctime=False) != _stamp(before, ctime=False):
                raise ArtifactIntegrityError(f"Archive changed during verification: {path}")
        if actual != sha256 or size != before.st_size or (expected_size is not None and size != expected_size):
            raise ArtifactIntegrityError(f"Archive digest/size mismatch; existing history was not overwritten: {path}")
        if _stamp(path.lstat(), ctime=False) != _stamp(before, ctime=False):
            raise ArtifactIntegrityError(f"Archive path changed during verification: {path}")
        return size
    except OSError as exc:
        raise ArtifactIntegrityError(f"Archive cannot be verified: {path}") from exc


def _controlled_download(source: Path, resolved: Path, data_dir: Path, info: os.stat_result) -> bool:
    downloads = data_dir.expanduser().resolve() / "downloads"
    # Refuse links for aliases, symlinked download roots and files already
    # aliased elsewhere. The normal downloader produces direct single-link files.
    return (
        os.name != "nt"
        and not downloads.is_symlink()
        and source.absolute() == resolved
        and resolved.is_relative_to(downloads)
        and info.st_nlink == 1
    )


def _copy_payload(stream: BinaryIO, destination: Path) -> None:
    stream.seek(0)
    copied = 0
    with destination.open("xb") as output:
        for block in iter(lambda: stream.read(_BLOCK), b""):
            copied += len(block)
            if copied > MAX_ARTIFACT_BYTES:
                raise SourceChangedError("Source grew beyond the artifact limit during copy")
            output.write(block)
        output.flush()
        os.fsync(output.fileno())


def _publish_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish complete bytes without a check-then-replace window."""
    if os.name == "nt":
        os.rename(source, destination)  # Windows rename fails if destination exists.
        return
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renamex_np" if sys.platform == "darwin" else "renameat2", None)
    if function is not None:
        if sys.platform == "darwin":
            function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            result = function(os.fsencode(source), os.fsencode(destination), 0x00000004)  # RENAME_EXCL
        else:
            function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            result = function(-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
        if result == 0:
            return
        error = ctypes.get_errno()
        if error not in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
            raise OSError(error, os.strerror(error), str(destination))
    # A same-directory link is also atomic and never overwrites an existing name.
    # Filesystems lacking both exclusive rename and hardlinks fail conservatively.
    os.link(source, destination, follow_symlinks=False)
    source.unlink()


def archive_artifact(source: Path, data_dir: Path, expected_sha256: str | None = None) -> ArchivedArtifact:
    """Acquire verified original bytes; callers should parse the returned path.

    Existing corrupt archives are rejected, never repaired/overwritten silently.
    Controlled download-cache files can be hardlinked because their downloader
    atomically replaces them. Every other source is copied into independent data.
    """
    source = source.expanduser()
    expected = _sha256(expected_sha256) if expected_sha256 is not None else None
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported book suffix for artifact archive: {source.suffix}")
    resolved = source.expanduser().resolve(strict=True)
    _size(resolved.stat())
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(source, flags), "rb") as stream:
        initial = os.fstat(stream.fileno())
        _size(initial)
        sha256, size = _digest(stream)
        _assert_source(source, stream, initial)
        if size != initial.st_size:
            raise SourceChangedError("Source size changed during hashing")
        if expected is not None and sha256 != expected:
            raise ArtifactIntegrityError("Source does not match the expected artifact SHA256")
        root = _archive_root(data_dir, create=True)
        assert root is not None
        shard = _shard(root, sha256, create=True)
        assert shard is not None
        target = shard / (sha256 + suffix)
        if _exists(target):
            _verify(target, sha256, size)
            _assert_source(source, stream, initial)
            return {"path": target, "sha256": sha256, "bytes": size, "method": "existing"}
        with tempfile.TemporaryDirectory(prefix=".acquire-", dir=shard) as scratch:
            payload = Path(scratch) / "payload"
            method = "copy"
            if _controlled_download(source, resolved, data_dir, initial):
                try:
                    os.link(source, payload, follow_symlinks=False)
                    method = "hardlink"
                except OSError as exc:
                    if exc.errno not in _LINK_FALLBACK_ERRORS:
                        raise
            if method == "copy":
                _copy_payload(stream, payload)
            else:
                with payload.open("rb") as linked_payload:
                    os.fsync(linked_payload.fileno())
            _assert_source(source, stream, initial, linked=method == "hardlink")
            try:
                _verify(payload, sha256, size)
            except ArtifactIntegrityError as exc:
                raise SourceChangedError("Source bytes changed between hashing and staging") from exc
            _assert_source(source, stream, initial, linked=method == "hardlink")
            payload_identity = (payload.stat().st_dev, payload.stat().st_ino)
            try:
                _publish_no_replace(payload, target)
            except FileExistsError:
                _verify(target, sha256, size)
                _assert_source(source, stream, initial, linked=method == "hardlink")
                return {"path": target, "sha256": sha256, "bytes": size, "method": "existing"}
            try:
                _verify(target, sha256, size)
                _assert_source(source, stream, initial, linked=method == "hardlink")
            except ArtifactIntegrityError:
                # A new alias can be invalidated by a violated downloader contract.
                # Remove only our own newly published corrupt inode, never history.
                try:
                    _verify(target, sha256, size)
                except ArtifactIntegrityError:
                    current = target.lstat() if _exists(target) else None
                    if current and (current.st_dev, current.st_ino) == payload_identity:
                        target.unlink()
                raise
            return {"path": target, "sha256": sha256, "bytes": size, "method": method}


def locate_artifact(data_dir: Path, sha256: str) -> Path | None:
    """Find verified original bytes by digest, rejecting corrupt/unsafe archives.

    Multiple suffixes for the same bytes are valid; lookup returns the first
    suffix in sorted order. Preserve recorded path/format when reparsing a book.
    """
    digest = _sha256(sha256)
    root = _archive_root(data_dir, create=False)
    if root is None:
        return None
    shard = _shard(root, digest, create=False)
    if shard is None:
        return None
    found = []
    for suffix in sorted(SUPPORTED_SUFFIXES):
        path = shard / (digest + suffix)
        if _exists(path):
            _verify(path, digest)
            found.append(path)
    return found[0] if found else None
