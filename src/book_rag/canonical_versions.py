"""Immutable canonical chapters for saved quotations, without reparsing originals.

Use a coherent SQLite snapshot to construct ParsedBook when the user first saves
a quotation. This module stores exact strings, not source transcriptions or notes.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import zlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypedDict

from book_rag.parsers import ParsedBook

SCHEMA_VERSION = 1
MAX_CANONICAL_BYTES = 100 * 1024 * 1024
MAX_SECTION_BYTES = 20 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SECTIONS = 100_000
MAX_HEADER_BYTES = 64 * 1024
_STORED_SECTION_LIMIT = MAX_SECTION_BYTES + 1024 * 1024
_BOOK_FIELDS = frozenset({"title", "author", "language", "edition", "source_url", "rights", "rights_url"})
_SECTION_FIELDS = frozenset({"section_author", "section_role", "section_translator", "attribution_basis"})
_SECTION_ROLES = frozenset({"", "main_text", "editorial", "unclassified"})


class CanonicalIntegrityError(ValueError):
    """Canonical version identity, structure or stored bytes failed validation."""


class PreservedCanonical(TypedDict):
    version_id: str
    path: Path
    section_count: int
    canonical_bytes: int
    stored_bytes: int


def _sha(value: str, field: str = "SHA256") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", value):
        raise ValueError(f"{field} must contain exactly 64 hexadecimal characters")
    return value.lower()


def _optional_sha(value: str | None, field: str) -> str | None:
    return None if value is None else _sha(value, field)


def _version_digest(version_id: str) -> str:
    if not isinstance(version_id, str) or not re.fullmatch(r"cv1-[a-f0-9]{64}", version_id):
        raise ValueError("Canonical version must be cv1- followed by 64 lowercase hexadecimal characters")
    return version_id[4:]


def _json_bytes(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (UnicodeError, TypeError, ValueError) as exc:
        raise ValueError("Canonical payload must contain exact valid UTF-8 strings") from exc


def _text(value: Any, field: str, maximum: int = MAX_HEADER_BYTES) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    try:
        if len(value.encode()) > maximum:
            raise ValueError(f"{field} exceeds the canonical byte limit")
    except UnicodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc
    return value


def _directory(path: Path, *, create: bool) -> Path:
    if path.is_symlink():
        raise CanonicalIntegrityError("Canonical storage directories must not be symlinks")
    if create:
        path.mkdir(exist_ok=True)
    if not path.is_dir():
        raise FileNotFoundError(path)
    return path


def _path(data_dir: Path, kind: str, digest: str, *, create: bool) -> Path:
    base = data_dir.expanduser().resolve()
    if create:
        base.mkdir(parents=True, exist_ok=True)
    root = _directory(base / "canonical", create=create)
    folder = _directory(root / kind, create=create)
    shard = _directory(folder / digest[:2], create=create)
    return shard / (digest + (".json.gz" if kind == "sections" else ".json"))


def _stamp(info: os.stat_result) -> tuple[int, int, int, int]:
    # Publishing/unlinking another hardlink changes ctime but not immutable bytes.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_regular(path: Path, maximum: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
        raise CanonicalIntegrityError("Canonical object must be a bounded regular nonsymlink file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise CanonicalIntegrityError("Canonical object changed before reading")
            payload = stream.read(maximum + 1)
            if len(payload) != before.st_size or _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise CanonicalIntegrityError("Canonical object changed while reading")
        if _stamp(path.lstat()) != _stamp(before):
            raise CanonicalIntegrityError("Canonical object path changed while reading")
        return payload
    except OSError as exc:
        raise CanonicalIntegrityError("Canonical object cannot be read safely") from exc


def _decode(payload: bytes, digest: str) -> dict[str, Any]:
    if hashlib.sha256(payload).hexdigest() != digest:
        raise CanonicalIntegrityError("Canonical decompressed payload SHA256 mismatch")
    try:
        result = json.loads(payload.decode("utf-8"))
        if not isinstance(result, dict) or _json_bytes(result) != payload:
            raise ValueError("Noncanonical JSON encoding")
    except (UnicodeError, ValueError) as exc:
        raise CanonicalIntegrityError("Invalid canonical UTF-8 JSON object") from exc
    return result


def _section(path: Path, digest: str) -> tuple[dict[str, Any], int]:
    compressed = _read_regular(path, _STORED_SECTION_LIMIT)
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as archive:
            payload = archive.read(MAX_SECTION_BYTES + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise CanonicalIntegrityError("Canonical section gzip is invalid or incomplete") from exc
    if len(payload) > MAX_SECTION_BYTES:
        raise CanonicalIntegrityError("Canonical section exceeds its decompressed byte limit")
    result = _decode(payload, digest)
    if set(result) != {"schema_version", "title", "text"} or type(result["schema_version"]) is not int:
        raise CanonicalIntegrityError("Invalid canonical section fields")
    if result["schema_version"] != SCHEMA_VERSION:
        raise CanonicalIntegrityError("Unsupported canonical section schema")
    try:
        _text(result["title"], "Section title", MAX_SECTION_BYTES)
        _text(result["text"], "Section text", MAX_SECTION_BYTES)
    except ValueError as exc:
        raise CanonicalIntegrityError(str(exc)) from exc
    return result, len(payload)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish(path: Path, payload: bytes, verify: Callable[[Path], Any]) -> int:
    if path.exists() or path.is_symlink():
        verify(path)
        return path.stat().st_size
    descriptor, name = tempfile.mkstemp(prefix=".canonical-", dir=path.parent)
    temporary = Path(name)
    published = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        verify(temporary)
        try:
            # Same-directory atomic no-replace publication, including concurrent writers.
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            verify(path)
        else:
            published = True
            verify(path)
            _sync_directory(path.parent)
        return path.stat().st_size
    except BaseException:
        if published:
            try:
                verify(path)
            except (OSError, ValueError):
                # A corrupt object created by this invocation is not old history.
                # Never unlink a concurrent publisher's different inode.
                if path.exists() and path.lstat().st_ino == temporary.lstat().st_ino:
                    path.unlink()
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _manifest(path: Path, digest: str) -> tuple[dict[str, Any], int]:
    payload = _read_regular(path, MAX_MANIFEST_BYTES)
    result = _decode(payload, digest)
    required = {
        "schema_version",
        "book",
        "artifact_sha256",
        "parser_fingerprint",
        "indexer_fingerprint",
        "section_count",
        "section_payload_bytes",
        "sections",
    }
    try:
        if set(result) != required or type(result["schema_version"]) is not int:
            raise ValueError("Invalid canonical manifest fields")
        if result["schema_version"] != SCHEMA_VERSION:
            raise ValueError("Unsupported canonical manifest schema")
        book = result["book"]
        if not isinstance(book, dict) or set(book) != _BOOK_FIELDS:
            raise ValueError("Invalid canonical book header")
        for key in _BOOK_FIELDS:
            _text(book[key], f"Book {key}")
        if _sha(result["artifact_sha256"]) != result["artifact_sha256"]:
            raise ValueError("Noncanonical source artifact SHA256")
        for key in ("parser_fingerprint", "indexer_fingerprint"):
            if _optional_sha(result[key], key) != result[key]:
                raise ValueError(f"Noncanonical {key}")
        sections = result["sections"]
        if not isinstance(sections, list) or not 0 < len(sections) <= MAX_SECTIONS:
            raise ValueError("Canonical manifest needs a bounded nonempty section list")
        if type(result["section_count"]) is not int or result["section_count"] != len(sections):
            raise ValueError("Canonical section count mismatch")
        total = 0
        for ordinal, section in enumerate(sections):
            if not isinstance(section, dict) or set(section) != {
                "ordinal",
                "title",
                "sha256",
                "bytes",
                "characters",
                "metadata",
            }:
                raise ValueError("Invalid canonical section descriptor")
            if type(section["ordinal"]) is not int or section["ordinal"] != ordinal:
                raise ValueError("Canonical section ordinals must be contiguous from zero")
            _text(section["title"], "Section title", MAX_SECTION_BYTES)
            _section_metadata(section["metadata"])
            if _sha(section["sha256"]) != section["sha256"]:
                raise ValueError("Noncanonical section SHA256")
            if type(section["bytes"]) is not int or not 0 < section["bytes"] <= MAX_SECTION_BYTES:
                raise ValueError("Canonical section byte count exceeds its limit")
            if type(section["characters"]) is not int or not 0 <= section["characters"] <= section["bytes"]:
                raise ValueError("Invalid canonical section character count")
            total += section["bytes"]
        if type(result["section_payload_bytes"]) is not int or result["section_payload_bytes"] != total:
            raise ValueError("Canonical total section byte count mismatch")
        if total + len(payload) > MAX_CANONICAL_BYTES:
            raise ValueError("Canonical book exceeds the aggregate byte limit")
    except ValueError as exc:
        raise CanonicalIntegrityError(str(exc)) from exc
    return result, len(payload)


def _section_metadata(metadata: Any) -> dict[str, str]:
    if not isinstance(metadata, Mapping) or set(metadata) - _SECTION_FIELDS:
        raise ValueError("Section metadata accepts only explicit contributor fields")
    result = {key: _text(value, key) for key, value in metadata.items()}
    if "section_role" in result and result["section_role"] not in _SECTION_ROLES:
        raise ValueError("Section role must be main_text, editorial, unclassified or empty")
    return result


def _metadata_list(
    metadata: Sequence[Mapping[str, str]] | Mapping[int, Mapping[str, str]] | None, count: int
) -> list[dict[str, str]]:
    if metadata is None:
        return [{} for _ in range(count)]
    if isinstance(metadata, Mapping):
        if any(type(ordinal) is not int or not 0 <= ordinal < count for ordinal in metadata):
            raise ValueError("Section metadata ordinal is out of range")
        return [_section_metadata(metadata.get(ordinal, {})) for ordinal in range(count)]
    if not isinstance(metadata, (list, tuple)) or len(metadata) != count:
        raise ValueError("Section metadata list must align exactly with section order")
    return [_section_metadata(entry) for entry in metadata]


def preserve_canonical(
    book: ParsedBook,
    data_dir: Path,
    *,
    artifact_sha256: str,
    parser_fingerprint: str | None = None,
    indexer_fingerprint: str | None = None,
    title: str | None = None,
    author: str | None = None,
    language: str | None = None,
    edition: str = "",
    source_url: str = "",
    rights: str = "",
    rights_url: str = "",
    section_metadata: Sequence[Mapping[str, str]] | Mapping[int, Mapping[str, str]] | None = None,
) -> PreservedCanonical:
    """Preserve exact snapshot strings; publish a version only after every section.

    Fingerprints are caller-provided historical provenance, never guessed from
    the parser currently installed. Unknown parser/indexer identities stay null.
    """
    artifact_sha256 = _sha(artifact_sha256, "Source artifact SHA256")
    parser_fingerprint = _optional_sha(parser_fingerprint, "Parser fingerprint")
    indexer_fingerprint = _optional_sha(indexer_fingerprint, "Indexer fingerprint")
    header = {
        "title": _text(book.title if title is None else title, "Book title"),
        "author": _text(book.author if author is None else author, "Book author"),
        "language": _text(book.language if language is None else language, "Book language"),
        "edition": _text(edition, "Book edition"),
        "source_url": _text(source_url, "Book source URL"),
        "rights": _text(rights, "Book rights"),
        "rights_url": _text(rights_url, "Book rights URL"),
    }
    if not isinstance(book.sections, list) or not 0 < len(book.sections) <= MAX_SECTIONS:
        raise ValueError("Canonical book needs a bounded nonempty section list")
    sections = []
    contributors = _metadata_list(section_metadata, len(book.sections))
    stored_digests: set[str] = set()
    total = stored = 0
    for ordinal, chapter in enumerate(book.sections):
        chapter_title = _text(chapter.title, "Section title", MAX_SECTION_BYTES)
        chapter_text = _text(chapter.text, "Section text", MAX_SECTION_BYTES)
        payload = _json_bytes({"schema_version": SCHEMA_VERSION, "title": chapter_title, "text": chapter_text})
        total += len(payload)
        if len(payload) > MAX_SECTION_BYTES or total > MAX_CANONICAL_BYTES:
            raise ValueError("Canonical section or book exceeds its byte limit")
        digest = hashlib.sha256(payload).hexdigest()
        path = _path(data_dir, "sections", digest, create=True)
        if path.exists() or path.is_symlink():
            _section(path, digest)
            stored_size = path.stat().st_size
        else:
            output = io.BytesIO()
            with gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=output, mtime=0) as compressed:
                compressed.write(payload)
            stored_size = _publish(
                path, output.getvalue(), lambda candidate, digest=digest: _section(candidate, digest)
            )
        if digest not in stored_digests:
            stored += stored_size
            stored_digests.add(digest)
        sections.append(
            {
                "ordinal": ordinal,
                "title": chapter_title,
                "sha256": digest,
                "bytes": len(payload),
                "characters": len(chapter_text),
                "metadata": contributors[ordinal],
            }
        )
    manifest = _json_bytes(
        {
            "schema_version": SCHEMA_VERSION,
            "book": header,
            "artifact_sha256": artifact_sha256,
            "parser_fingerprint": parser_fingerprint,
            "indexer_fingerprint": indexer_fingerprint,
            "section_count": len(sections),
            "section_payload_bytes": total,
            "sections": sections,
        }
    )
    if len(manifest) > MAX_MANIFEST_BYTES or total + len(manifest) > MAX_CANONICAL_BYTES:
        raise ValueError("Canonical manifest or book exceeds its byte limit")
    digest = hashlib.sha256(manifest).hexdigest()
    path = _path(data_dir, "versions", digest, create=True)
    stored += _publish(path, manifest, lambda candidate: _manifest(candidate, digest))
    return {
        "version_id": "cv1-" + digest,
        "path": path,
        "section_count": len(sections),
        "canonical_bytes": total + len(manifest),
        "stored_bytes": stored,
    }


def read_canonical_manifest(data_dir: Path, version_id: str) -> dict[str, Any]:
    """Verify a small header/TOC without decompressing any chapter."""
    digest = _version_digest(version_id)
    manifest, size = _manifest(_path(data_dir, "versions", digest, create=False), digest)
    return manifest | {"version_id": version_id, "canonical_bytes": manifest["section_payload_bytes"] + size}


def read_canonical_section(data_dir: Path, version_id: str, ordinal: int) -> dict[str, Any]:
    """Verify and read exactly one zero-based historical section and provenance."""
    if type(ordinal) is not int:
        raise ValueError("Canonical section ordinal must be an integer")
    manifest = read_canonical_manifest(data_dir, version_id)
    if not 0 <= ordinal < manifest["section_count"]:
        raise IndexError("Canonical section ordinal is out of range")
    entry = manifest["sections"][ordinal]
    section, size = _section(_path(data_dir, "sections", entry["sha256"], create=False), entry["sha256"])
    if section["title"] != entry["title"] or len(section["text"]) != entry["characters"] or size != entry["bytes"]:
        raise CanonicalIntegrityError("Canonical section differs from its manifest descriptor")
    return {
        key: manifest[key]
        for key in (
            "version_id",
            "book",
            "artifact_sha256",
            "parser_fingerprint",
            "indexer_fingerprint",
            "section_count",
        )
    } | {
        "section": {
            "ordinal": ordinal,
            "title": section["title"],
            "text": section["text"],
            "sha256": entry["sha256"],
            **entry["metadata"],
        }
    }
