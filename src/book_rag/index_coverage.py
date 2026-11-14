"""Explicit semantic coverage proofs and fail-closed per-book revision bindings.

Schema installation is an explicit writable migration. Query validation never
creates schema or silently rebinds a changed covered book. Ready-index receipt
and vector/ID integrity verification belong to the future index installer.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import threading
import uuid
from collections import OrderedDict
from collections.abc import Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Protocol, TypedDict

SCHEMA_VERSION = 1
MAX_COVERED_BOOKS = 50_000
CACHE_SIZE = 8
SECTION_CACHE_SIZE = 4
SECTION_CACHE_BYTES = 32 * 1024 * 1024
_LOCK = threading.RLock()

_EPOCH_SQL = """CREATE TABLE IF NOT EXISTS book_index_epochs (
 book_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>=0)
) STRICT"""
_META_SQL = """CREATE TABLE IF NOT EXISTS book_index_epoch_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL,
 namespace TEXT NOT NULL CHECK(length(namespace)=32)
) STRICT"""


def _bump(identity: str, *, when: str = "") -> str:
    return (
        "INSERT INTO book_index_epochs(book_id,revision) "
        f"SELECT {identity},1 {when} "
        "ON CONFLICT(book_id) DO UPDATE SET revision=book_index_epochs.revision+1;"
    )


def _trigger_sql() -> dict[str, str]:
    definitions = {
        "books": ("id", ("id", "title", "author", "language", "status")),
        "sections": ("book_id", ("id", "book_id", "ordinal", "title", "text")),
        "passages": ("book_id", ("id", "book_id", "section_id", "start", "end", "text", "sha256")),
    }
    result = {}
    for table, (identity, fields) in definitions.items():
        for operation in ("INSERT", "UPDATE", "DELETE"):
            name = f"bookrag_epoch_{table}_{operation.lower()}"
            event = operation
            condition = ""
            if operation == "UPDATE":
                event += " OF " + ",".join(fields)
                condition = " WHEN " + " OR ".join(f"OLD.{field} IS NOT NEW.{field}" for field in fields)
                body = _bump(f"OLD.{identity}") + _bump(
                    f"NEW.{identity}", when=f"WHERE NEW.{identity} IS NOT OLD.{identity}"
                )
            else:
                body = _bump(f"{'NEW' if operation == 'INSERT' else 'OLD'}.{identity}")
            result[name] = f"CREATE TRIGGER {name} AFTER {event} ON {table}{condition} BEGIN {body} END"
    # REPLACE can suppress DELETE triggers when recursive_triggers=OFF. Observe
    # conflicting old owners before SQLite removes them, including UNIQUE keys.
    for table, conflict in {
        "sections": "id=NEW.id OR (book_id=NEW.book_id AND ordinal=NEW.ordinal)",
        "passages": "id=NEW.id OR (section_id=NEW.section_id AND start=NEW.start AND end=NEW.end)",
    }.items():
        name = f"bookrag_epoch_{table}_insert_conflict"
        body = (
            "INSERT INTO book_index_epochs(book_id,revision) "
            f"SELECT DISTINCT book_id,1 FROM {table} WHERE {conflict} "
            "ON CONFLICT(book_id) DO UPDATE SET revision=book_index_epochs.revision+1;"
        )
        result[name] = f"CREATE TRIGGER {name} BEFORE INSERT ON {table} BEGIN {body} END"
    return result


_TRIGGERS = _trigger_sql()
_SCHEMA = {"book_index_epochs": _EPOCH_SQL, "book_index_epoch_meta": _META_SQL, **_TRIGGERS}


class CoverageLibrary(Protocol):
    db_path: Path

    def connect(self) -> AbstractContextManager[sqlite3.Connection]: ...


class CoverageValidation(TypedDict):
    valid: bool
    tracked: bool
    cache_hit: bool
    state_token: str
    reason: str
    covered_books: int
    covered_passages: int


_CACHE: OrderedDict[tuple[str, ...], CoverageValidation] = OrderedDict()


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("Coverage metadata must contain valid UTF-8 JSON values") from exc


def _text_bytes(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("Covered canonical text must be a UTF-8 string")
    try:
        return value.encode()
    except UnicodeError as exc:
        raise ValueError("Covered canonical text must be valid UTF-8") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Coverage SHA256 must contain 64 lowercase hexadecimal characters")
    return value


def _normalize_sql(value: str) -> str:
    return " ".join(re.sub(r"\bIF NOT EXISTS\s+", "", value, flags=re.I).strip().rstrip(";").split()).casefold()


def _installed_sql(conn: sqlite3.Connection) -> dict[str, str]:
    placeholders = ",".join("?" for _ in _SCHEMA)
    return dict(conn.execute(f"SELECT name,sql FROM sqlite_master WHERE name IN ({placeholders})", tuple(_SCHEMA)))


def ensure_epoch_schema(conn: sqlite3.Connection) -> str:
    """Explicitly install tracking atomically, preserving any caller transaction.

    Tombstones intentionally have no books FK: delete/reinsert must never reset a
    book's revision. Missing owned objects get repaired with a fresh namespace;
    incompatible definitions are rejected rather than silently overwritten.
    """
    conn.execute("SAVEPOINT bookrag_epoch_install")
    try:
        installed = _installed_sql(conn)
        for name, sql in installed.items():
            if _normalize_sql(sql) != _normalize_sql(_SCHEMA[name]):
                raise ValueError(f"Incompatible semantic epoch object: {name}")
        for table, fields in {
            "books": {"id", "title", "author", "language", "status"},
            "sections": {"id", "book_id", "ordinal", "title", "text"},
            "passages": {"id", "book_id", "section_id", "start", "end", "text", "sha256"},
        }.items():
            if not fields.issubset({row[1] for row in conn.execute(f"PRAGMA table_info({table})")}):
                raise ValueError(f"Missing required semantic source columns: {table}")
        conn.execute(_EPOCH_SQL)
        conn.execute(_META_SQL)
        rows = conn.execute("SELECT schema_version,namespace FROM book_index_epoch_meta").fetchall()
        if len(rows) > 1 or (rows and (rows[0][0] != SCHEMA_VERSION or not re.fullmatch(r"[a-f0-9]{32}", rows[0][1]))):
            raise ValueError("Invalid semantic epoch namespace")
        namespace = rows[0][1] if rows and len(installed) == len(_SCHEMA) else uuid.uuid4().hex
        conn.execute(
            "INSERT INTO book_index_epoch_meta VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE "
            "SET schema_version=excluded.schema_version,namespace=excluded.namespace",
            (SCHEMA_VERSION, namespace),
        )
        conn.execute("INSERT OR IGNORE INTO book_index_epochs SELECT id,0 FROM books")
        for name, sql in _TRIGGERS.items():
            if name not in installed:
                conn.execute(sql)
        conn.execute("RELEASE SAVEPOINT bookrag_epoch_install")
        return namespace
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT bookrag_epoch_install")
        conn.execute("RELEASE SAVEPOINT bookrag_epoch_install")
        raise


@contextmanager
def _snapshot(conn: sqlite3.Connection) -> Generator[None]:
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            conn.rollback()


def _book_ids(values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes)) or not 0 < len(values) <= MAX_COVERED_BOOKS:
        raise ValueError("Coverage needs a bounded nonempty edition-ID list")
    if any(not isinstance(value, str) or not value or len(value) > 512 for value in values):
        raise ValueError("Covered edition IDs must be nonempty bounded strings")
    ordered = sorted(values)
    if len(set(ordered)) != len(ordered):
        raise ValueError("Covered edition IDs must be unique")
    return ordered


def _indexed_ids(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT id FROM books WHERE status='indexed' ORDER BY id")]


def capture_coverage(
    conn: sqlite3.Connection, book_ids: Sequence[str] | None = None, *, kind: str = "subset"
) -> dict[str, Any]:
    """Capture actual source bytes/headers/IDs once in one coherent SQL snapshot.

    Stored passage SHA claims and exact canonical spans are independently checked.
    Chapter text is hashed once, then reread through a bounded LRU for span checks.
    """
    if kind not in {"subset", "full"}:
        raise ValueError("Coverage kind must be subset or full")
    with _snapshot(conn):
        ids = _book_ids(_indexed_ids(conn) if book_ids is None else book_ids)
        if kind == "full" and ids != _indexed_ids(conn):
            raise ValueError("Full coverage must include every indexed edition")
        parameter = _canonical(ids).decode()
        books = list(
            conn.execute(
                "SELECT b.id,b.title,b.author,b.language,b.status FROM books b "
                "JOIN json_each(?) c ON c.value=b.id ORDER BY b.id",
                (parameter,),
            )
        )
        if [row[0] for row in books] != ids or any(row[4] != "indexed" for row in books):
            raise ValueError("Covered edition is missing or not indexed")
        headers = {row[0]: hashlib.sha256(_canonical(tuple(row[:4]))) for row in books}
        per_book_passages = dict.fromkeys(ids, 0)
        per_book_sections = dict.fromkeys(ids, 0)
        section_hash = hashlib.sha256()
        section_count = 0
        for row in conn.execute(
            "SELECT s.book_id,s.id,s.ordinal,s.title,s.text FROM sections s "
            "JOIN json_each(?) c ON c.value=s.book_id ORDER BY s.book_id,s.ordinal,s.id",
            (parameter,),
        ):
            headers[row[0]].update(b"\n" + _canonical(tuple(row[1:4])))
            section_hash.update(_canonical((row[0], row[1], hashlib.sha256(_text_bytes(row[4])).hexdigest())) + b"\n")
            section_count += 1
            per_book_sections[row[0]] += 1
        passages = hashlib.sha256()
        mapping = hashlib.sha256()
        count = 0
        section_cache: OrderedDict[int, tuple[str, str]] = OrderedDict()
        cached_bytes = 0
        for row in conn.execute(
            "SELECT p.id,p.sha256,p.book_id,p.section_id,p.start,p.end,p.text FROM passages p "
            "JOIN json_each(?) c ON c.value=p.book_id ORDER BY p.id",
            (parameter,),
        ):
            if _sha(row[1]) != hashlib.sha256(_text_bytes(row[6])).hexdigest():
                raise ValueError("Covered passage text differs from its stored SHA256")
            section_id, start, end = row[3:6]
            if type(section_id) is not int:
                raise ValueError("Covered passage section identity must be an integer")
            if section_id not in section_cache:
                chapter = conn.execute("SELECT book_id,text FROM sections WHERE id=?", (section_id,)).fetchone()
                if chapter is None:
                    raise ValueError("Covered passage refers to a missing canonical section")
                if not isinstance(chapter[0], str) or not isinstance(chapter[1], str):
                    raise ValueError("Covered canonical section must have string book identity and text")
                section_cache[section_id] = (chapter[0], chapter[1])
                cached_bytes += sys.getsizeof(chapter[1])
                # Retain at most one oversized chapter so its neighboring
                # passages do not cause repeated whole-chapter SQL reads.
                while len(section_cache) > SECTION_CACHE_SIZE or (
                    len(section_cache) > 1 and cached_bytes > SECTION_CACHE_BYTES
                ):
                    _, evicted = section_cache.popitem(last=False)
                    cached_bytes -= sys.getsizeof(evicted[1])
            section_cache.move_to_end(section_id)
            chapter_book, chapter_text = section_cache[section_id]
            if (
                chapter_book != row[2]
                or type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(chapter_text)
                or chapter_text[start:end] != row[6]
            ):
                raise ValueError("Covered passage differs from its exact canonical section slice")
            passages.update(f"{row[0]}:{row[1]}\n".encode())
            mapping.update(_canonical((row[0], *row[2:6])) + b"\n")
            count += 1
            per_book_passages[row[2]] += 1
        if not all(per_book_passages.values()) or not all(per_book_sections.values()):
            raise ValueError("Every covered edition needs indexed passages and sections")
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": kind,
            "covered_book_ids": ids,
            "books": len(ids),
            "passages": count,
            "sections": section_count,
            "passage_fingerprint": passages.hexdigest(),
            "mapping_fingerprint": mapping.hexdigest(),
            "section_text_fingerprint": section_hash.hexdigest(),
            "book_headers": {identity: checksum.hexdigest() for identity, checksum in headers.items()},
        }


def _proof(proof: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "kind",
        "covered_book_ids",
        "books",
        "passages",
        "sections",
        "passage_fingerprint",
        "mapping_fingerprint",
        "section_text_fingerprint",
        "book_headers",
    }
    if not isinstance(proof, Mapping) or set(proof) != required or type(proof["schema_version"]) is not int:
        raise ValueError("Invalid coverage proof fields")
    if proof["schema_version"] != SCHEMA_VERSION or proof["kind"] not in {"subset", "full"}:
        raise ValueError("Unsupported coverage proof")
    if not isinstance(proof["covered_book_ids"], list):
        raise ValueError("Covered edition IDs must be a list")
    ids = _book_ids(proof["covered_book_ids"])
    if ids != proof["covered_book_ids"] or type(proof["books"]) is not int or proof["books"] != len(ids):
        raise ValueError("Coverage edition count/order mismatch")
    for key in ("passages", "sections"):
        if type(proof[key]) is not int or proof[key] < 1:
            raise ValueError("Coverage counts must be positive integers")
    for key in ("passage_fingerprint", "mapping_fingerprint", "section_text_fingerprint"):
        _sha(proof[key])
    if not isinstance(proof["book_headers"], dict) or set(proof["book_headers"]) != set(ids):
        raise ValueError("Coverage header identities mismatch")
    for value in proof["book_headers"].values():
        _sha(value)
    return dict(proof)


def _db_identity(library: CoverageLibrary) -> tuple[str, int, int]:
    path = library.db_path.expanduser().resolve()
    info = path.stat()
    return str(path), info.st_dev, info.st_ino


def _state(conn: sqlite3.Connection, proof: Mapping[str, Any]) -> dict[str, Any]:
    installed = _installed_sql(conn)
    tracked = len(installed) == len(_SCHEMA) and all(
        _normalize_sql(installed[name]) == _normalize_sql(sql) for name, sql in _SCHEMA.items()
    )
    namespace = None
    if tracked:
        metadata = conn.execute("SELECT schema_version,namespace FROM book_index_epoch_meta").fetchall()
        tracked = bool(
            len(metadata) == 1
            and metadata[0][0] == SCHEMA_VERSION
            and isinstance(metadata[0][1], str)
            and re.fullmatch(r"[a-f0-9]{32}", metadata[0][1])
        )
        namespace = metadata[0][1] if tracked else None
    fields = "e.revision" if tracked else "NULL"
    join = "LEFT JOIN book_index_epochs e ON e.book_id=b.id " if tracked else ""
    rows = list(
        conn.execute(
            f"SELECT b.id,b.status,b.title,b.author,b.language,{fields} FROM books b "
            + join
            + "JOIN json_each(?) c ON c.value=b.id ORDER BY b.id",
            (_canonical(proof["covered_book_ids"]).decode(),),
        )
    )
    if tracked and any(type(row[5]) is not int or row[5] < 0 for row in rows):
        tracked = False
    well_typed = all(all(isinstance(value, str) for value in row[:5]) for row in rows)
    available = (
        well_typed
        and [row[0] for row in rows] == proof["covered_book_ids"]
        and all(row[1] == "indexed" for row in rows)
    )
    if proof["kind"] == "full" and _indexed_ids(conn) != proof["covered_book_ids"]:
        available = False
    return {
        "tracked": tracked,
        "available": available,
        "namespace": namespace,
        "schema_cookie": conn.execute("PRAGMA schema_version").fetchone()[0],
        "book_state": _digest([tuple(row) for row in rows]) if well_typed else _digest("invalid book field types"),
    }


def bind_coverage(library: CoverageLibrary, proof: Mapping[str, Any], *, receipt_sha256: str) -> dict[str, Any]:
    """Explicitly bind a ready-index proof to current target epochs after a full scan.

    The caller must already have validated the ready generation, its receipt,
    vector array AND ids.npy. This function validates only SQL source coverage.
    """
    expected = _proof(proof)
    receipt = _sha(receipt_sha256)
    identity = _db_identity(library)
    with library.connect() as conn, _snapshot(conn):
        state = _state(conn, expected)
        if not state["tracked"] or not state["available"]:
            raise ValueError("Explicit epoch tracking and available covered editions are required to bind")
        actual = capture_coverage(conn, expected["covered_book_ids"], kind=expected["kind"])
        if actual != expected:
            raise ValueError("Target source differs from the ready index's coverage proof")
    with library.connect() as conn, _snapshot(conn):
        current = _state(conn, expected)
    if _db_identity(library) != identity or current != state:
        raise ValueError("Covered source changed during explicit binding; retry from a stable snapshot")
    return {
        "schema_version": SCHEMA_VERSION,
        "coverage_sha256": _digest(expected),
        "receipt_sha256": receipt,
        "db_identity": list(identity),
        "namespace": state["namespace"],
        "schema_cookie": state["schema_cookie"],
        "state_token": _digest((identity, state)),
    }


def validate_coverage(
    library: CoverageLibrary, proof: Mapping[str, Any], binding: Mapping[str, Any] | None = None
) -> CoverageValidation:
    """Cheap pre/post query guard; drift never triggers an automatic rebind.

    Legacy readonly databases without epochs may use an unbound strict scan.
    Bound targets with missing/changed tracking always fail closed instead.
    """
    expected = _proof(proof)
    identity = _db_identity(library)
    proof_sha = _digest(expected)
    with _LOCK, library.connect() as conn, _snapshot(conn):
        state = _state(conn, expected)
        token = _digest((identity, state))
        result: CoverageValidation = {
            "valid": False,
            "tracked": state["tracked"],
            "cache_hit": False,
            "state_token": token,
            "reason": "Covered source is missing or not indexed",
            "covered_books": expected["books"],
            "covered_passages": expected["passages"],
        }
        if not state["available"]:
            return result
        if binding is None:
            if state["tracked"]:
                result["reason"] = "Tracked targets require an explicit ready-index coverage binding"
                return result
            try:
                actual = capture_coverage(conn, expected["covered_book_ids"], kind=expected["kind"])
                result["valid"] = actual == expected
                result["state_token"] = _digest((identity, actual))
                result["reason"] = "Legacy exact source scan" if result["valid"] else "Legacy source coverage changed"
            except (ValueError, UnicodeError) as exc:
                result["reason"] = str(exc)
            if _db_identity(library) != identity:
                result["valid"] = False
                result["reason"] = "Database replaced during validation"
            return result
        required = {
            "schema_version",
            "coverage_sha256",
            "receipt_sha256",
            "db_identity",
            "namespace",
            "schema_cookie",
            "state_token",
        }
        if (
            not isinstance(binding, Mapping)
            or set(binding) != required
            or type(binding["schema_version"]) is not int
            or binding["schema_version"] != SCHEMA_VERSION
        ):
            raise ValueError("Invalid coverage binding")
        _sha(binding["receipt_sha256"])
        _sha(binding["coverage_sha256"])
        _sha(binding["state_token"])
        key = (str(identity), proof_sha, _digest(dict(binding)), token)
        if state["tracked"] and key in _CACHE:
            _CACHE.move_to_end(key)
            cached = _CACHE[key].copy()
            cached["cache_hit"] = True
            if _db_identity(library) != identity:
                cached["valid"] = False
                cached["reason"] = "Database replaced during validation"
            return cached
        result["valid"] = bool(
            state["tracked"]
            and binding["coverage_sha256"] == proof_sha
            and binding["db_identity"] == list(identity)
            and binding["namespace"] == state["namespace"]
            and binding["schema_cookie"] == state["schema_cookie"]
            and binding["state_token"] == token
        )
        result["reason"] = "Verified target binding" if result["valid"] else "Covered source or epoch tracking changed"
        if _db_identity(library) != identity:
            result["valid"] = False
            result["reason"] = "Database replaced during validation"
        if state["tracked"]:
            _CACHE[key] = result.copy()
            _CACHE.move_to_end(key)
            while len(_CACHE) > CACHE_SIZE:
                _CACHE.popitem(last=False)
        return result
