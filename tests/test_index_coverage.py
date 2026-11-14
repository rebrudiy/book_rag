"""Target epoch bindings fail closed without revalidating changed SHA claims."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from book_rag import index_coverage as coverage
from book_rag.index_coverage import bind_coverage, capture_coverage, ensure_epoch_schema, validate_coverage
from book_rag.library import Library

RECEIPT = "a" * 64


def seed(tmp_path: Path, *, tracked: bool = True) -> tuple[Library, dict[str, Any]]:
    library = Library(tmp_path / "library")
    for identity in ("covered", "outside"):
        source = tmp_path / f"{identity}.txt"
        source.write_text(f"Original exact {identity} chapter.\n\nAnother sentence about amber bookmarks.")
        library.import_book(source, {"id": identity, "title": identity, "author": "CC0 fixture", "language": "en"})
    with library.connect() as conn:
        # Keep legacy-mode tests independent of future Library startup integration.
        for name in coverage._TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute("DROP TABLE IF EXISTS book_index_epochs")
        conn.execute("DROP TABLE IF EXISTS book_index_epoch_meta")
        if tracked:
            ensure_epoch_schema(conn)
        proof = capture_coverage(conn, ["covered"])
    return library, proof


def revision(conn: sqlite3.Connection, identity: str = "covered") -> int:
    return conn.execute("SELECT revision FROM book_index_epochs WHERE book_id=?", (identity,)).fetchone()[0]


def test_schema_is_explicit_idempotent_and_preserves_external_transaction(tmp_path: Path) -> None:
    library, _ = seed(tmp_path, tracked=False)
    with library.connect() as conn:
        conn.execute("BEGIN")
        namespace = ensure_epoch_schema(conn)
        assert conn.in_transaction and ensure_epoch_schema(conn) == namespace
        assert revision(conn) == 0
        conn.rollback()
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='book_index_epochs'").fetchone()
        ensure_epoch_schema(conn)
        first = revision(conn)
        ensure_epoch_schema(conn)
        assert revision(conn) == first


def test_persisted_binding_survives_cache_clear_and_returns_independent_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library, proof = seed(tmp_path)
    binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)

    def no_scan(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("A bound query guard must never rescan or automatically rebind")

    monkeypatch.setattr(coverage, "capture_coverage", no_scan)
    coverage._CACHE.clear()
    first = validate_coverage(library, proof, binding)
    assert first["valid"] and not first["cache_hit"]
    first["reason"] = "Do not poison the cached response"
    second = validate_coverage(library, proof, binding)
    assert second["valid"] and second["cache_hit"] and second["reason"] != first["reason"]
    assert second["state_token"] == first["state_token"]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE passages SET text='tampered without changing stored SHA' WHERE book_id='covered'",
        "UPDATE passages SET sha256='" + "f" * 64 + "' WHERE book_id='covered'",
        "UPDATE passages SET start=start+1 WHERE book_id='covered'",
        "UPDATE sections SET text=text||' changed' WHERE book_id='covered'",
        "UPDATE sections SET title='Changed header' WHERE book_id='covered'",
        "UPDATE sections SET ordinal=ordinal+1 WHERE book_id='covered'",
        "UPDATE books SET title='Changed title' WHERE id='covered'",
        "UPDATE books SET author='Changed author' WHERE id='covered'",
        "UPDATE books SET language='ru' WHERE id='covered'",
        "UPDATE books SET status='failed' WHERE id='covered'",
        "DELETE FROM passages WHERE book_id='covered'",
    ],
)
def test_any_covered_change_invalidates_without_trusting_claimed_hash_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statement: str
) -> None:
    library, proof = seed(tmp_path)
    binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
    before = validate_coverage(library, proof, binding)
    with library.connect() as conn:
        conn.execute(statement)
    monkeypatch.setattr(coverage, "capture_coverage", lambda *a, **k: pytest.fail("No automatic reproving"))
    after = validate_coverage(library, proof, binding)
    assert not after["valid"] and after["state_token"] != before["state_token"]
    coverage._CACHE.clear()
    assert not validate_coverage(library, proof, binding)["valid"], "Restart must not forgive covered drift"


def test_outside_changes_leave_subset_valid_but_new_edition_invalidates_full_coverage(tmp_path: Path) -> None:
    library, subset = seed(tmp_path)
    subset_binding = bind_coverage(library, subset, receipt_sha256=RECEIPT)
    with library.connect() as conn:
        full = capture_coverage(conn, kind="full")
    full_binding = bind_coverage(library, full, receipt_sha256=RECEIPT)
    before = validate_coverage(library, subset, subset_binding)
    source = tmp_path / "new.txt"
    source.write_text("New edition outside semantic coverage.")
    library.import_book(source, {"id": "new", "title": "New edition", "author": "CC0", "language": "ru"})
    with library.connect() as conn:
        conn.execute("UPDATE books SET title='Outside changed title' WHERE id='outside'")
    after = validate_coverage(library, subset, subset_binding)
    assert after["valid"] and after["cache_hit"] and before["state_token"] == after["state_token"]
    assert not validate_coverage(library, full, full_binding)["valid"]


def test_noop_updates_notes_and_failed_refresh_preserve_current_inputs(tmp_path: Path) -> None:
    library, proof = seed(tmp_path)
    binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
    with library.connect() as conn:
        before = revision(conn)
        conn.execute("UPDATE books SET title=title WHERE id='covered'")
        conn.execute("UPDATE passages SET text=text,sha256=sha256 WHERE book_id='covered'")
        assert revision(conn) == before
    hit = library.search("bookmarks", book_id="covered")["results"][0]
    library.save({"kind": "passage", "passage_id": hit["id"], "title": "Owned note"})
    library.stage("covered", "failed", "Provider unavailable; successful edition remains")
    assert validate_coverage(library, proof, binding)["valid"]


def test_failed_atomic_reindex_rolls_back_epoch_updates_with_source_rows(tmp_path: Path) -> None:
    library, proof = seed(tmp_path)
    binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("Valid replacement input; attribution will fail inside the transaction.")
    with pytest.raises(ValueError):
        library.import_book(replacement, {"id": "covered", "section_attributions": [None]})
    assert validate_coverage(library, proof, binding)["valid"]


def test_epoch_overflow_aborts_source_write_instead_of_wrapping(tmp_path: Path) -> None:
    library, _ = seed(tmp_path)
    with library.connect() as conn:
        conn.execute("UPDATE book_index_epochs SET revision=? WHERE book_id='covered'", (2**63 - 1,))
    with library.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE books SET title='Must not publish' WHERE id='covered'")
    current = library.get_book("covered")
    assert current is not None and current["title"] == "covered"


def test_tombstone_revision_cannot_repeat_after_delete_reinsert_aba(tmp_path: Path) -> None:
    library, proof = seed(tmp_path)
    binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
    with library.connect() as conn:
        old_revision = revision(conn)
        book = dict(conn.execute("SELECT * FROM books WHERE id='covered'").fetchone())
        sections = [dict(row) for row in conn.execute("SELECT * FROM sections WHERE book_id='covered'")]
        passages = [dict(row) for row in conn.execute("SELECT * FROM passages WHERE book_id='covered'")]
        conn.execute("DELETE FROM books WHERE id='covered'")
        tombstone = revision(conn)
        for table, rows in (("books", [book]), ("sections", sections), ("passages", passages)):
            for row in rows:
                columns = ",".join(row)
                placeholders = ",".join("?" for _ in row)
                conn.execute(f"INSERT INTO {table}({columns}) VALUES({placeholders})", tuple(row.values()))
        assert revision(conn) > tombstone > old_revision
        assert capture_coverage(conn, ["covered"]) == proof, "The complete input can return while revision never does"
    assert not validate_coverage(library, proof, binding)["valid"]
    new_binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
    assert validate_coverage(library, proof, new_binding)["valid"], "Only an explicit strict rebind may accept it"


@pytest.mark.parametrize("table", ["passages", "sections"])
def test_moving_source_rows_bumps_both_old_and_new_book(tmp_path: Path, table: str) -> None:
    library, _ = seed(tmp_path)
    with library.connect() as conn:
        before = {identity: revision(conn, identity) for identity in ("covered", "outside")}
        extra = ",ordinal=ordinal+100" if table == "sections" else ""
        conn.execute(f"UPDATE {table} SET book_id='outside'{extra} WHERE book_id='covered'")
        assert all(revision(conn, identity) > before[identity] for identity in before)


@pytest.mark.parametrize("table", ["passages", "sections"])
def test_replace_detects_old_owner_even_when_recursive_delete_triggers_are_off(tmp_path: Path, table: str) -> None:
    library, proof = seed(tmp_path)
    binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
    with library.connect() as conn:
        conn.execute("PRAGMA recursive_triggers=OFF")
        before = revision(conn)
        row = dict(conn.execute(f"SELECT * FROM {table} WHERE book_id='covered'").fetchone())
        row["book_id"] = "outside"
        if table == "sections":
            row["ordinal"] = 100
        placeholders = ",".join("?" for _ in row)
        conn.execute(f"INSERT OR REPLACE INTO {table}({','.join(row)}) VALUES({placeholders})", tuple(row.values()))
        assert revision(conn) > before
    assert not validate_coverage(library, proof, binding)["valid"]


def test_missing_trigger_repair_rotates_namespace_and_cannot_hide_untracked_tamper(tmp_path: Path) -> None:
    library, proof = seed(tmp_path)
    binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
    with library.connect() as conn:
        conn.execute("DROP TRIGGER bookrag_epoch_passages_update")
        conn.execute("UPDATE passages SET text='No longer equals stored SHA' WHERE book_id='covered'")
    assert not validate_coverage(library, proof, binding)["valid"]
    with library.connect() as conn:
        namespace = ensure_epoch_schema(conn)
    assert namespace != binding["namespace"]
    assert not validate_coverage(library, proof, binding)["valid"]
    with pytest.raises(ValueError, match="stored SHA256"):
        bind_coverage(library, proof, receipt_sha256=RECEIPT)


def test_unrelated_ddl_requires_explicit_rebind_and_broken_owned_schema_is_rejected(tmp_path: Path) -> None:
    library, proof = seed(tmp_path)
    binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
    with library.connect() as conn:
        conn.execute("CREATE TABLE unrelated_owned_fixture(value TEXT)")
    assert not validate_coverage(library, proof, binding)["valid"]
    assert validate_coverage(library, proof, bind_coverage(library, proof, receipt_sha256=RECEIPT))["valid"]
    with library.connect() as conn:
        conn.execute("DROP TRIGGER bookrag_epoch_books_update")
        conn.execute("CREATE TRIGGER bookrag_epoch_books_update AFTER UPDATE ON books BEGIN SELECT 1; END")
        with pytest.raises(ValueError, match="Incompatible"):
            ensure_epoch_schema(conn)
        assert (
            "SELECT 1"
            in conn.execute("SELECT sql FROM sqlite_master WHERE name='bookrag_epoch_books_update'").fetchone()[0]
        )


def test_missing_epoch_row_and_malformed_book_type_fail_closed(tmp_path: Path) -> None:
    library, proof = seed(tmp_path)
    binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
    with library.connect() as conn:
        conn.execute("DELETE FROM book_index_epochs WHERE book_id='covered'")
    assert not validate_coverage(library, proof, binding)["valid"]
    with library.connect() as conn:
        ensure_epoch_schema(conn)
        conn.execute("UPDATE books SET title=? WHERE id='covered'", (sqlite3.Binary(b"wrong type"),))
    assert not validate_coverage(library, proof, binding)["valid"]


def test_explicit_rebind_checks_actual_canonical_section_not_only_passage_sha(tmp_path: Path) -> None:
    library, proof = seed(tmp_path)
    with library.connect() as conn:
        conn.execute("UPDATE sections SET text=text||' new canonical context' WHERE book_id='covered'")
    with pytest.raises(ValueError, match="differs"):
        bind_coverage(library, proof, receipt_sha256=RECEIPT)


@pytest.mark.parametrize(
    "mutation",
    [
        "start=-1",
        "start=start+1",
        "end=end+1",
        "end=start",
        "section_id=(SELECT id FROM sections WHERE book_id='outside' LIMIT 1),start=start+1",
        "section_id=9999999",
    ],
)
def test_strict_capture_and_bind_reject_bad_spans_or_foreign_sections_even_with_unchanged_sha(
    tmp_path: Path, mutation: str
) -> None:
    library, proof = seed(tmp_path)
    with library.connect() as conn:
        # Deliberately simulate an externally damaged owned DB, including a
        # dangling reference normally prevented by foreign_keys=ON.
        conn.execute("PRAGMA foreign_keys=OFF")
        original_sha = conn.execute("SELECT sha256 FROM passages WHERE book_id='covered'").fetchone()[0]
        conn.execute(f"UPDATE passages SET {mutation} WHERE book_id='covered'")
        assert conn.execute("SELECT sha256 FROM passages WHERE book_id='covered'").fetchone()[0] == original_sha
    with library.connect() as conn, pytest.raises(ValueError, match="canonical section"):
        capture_coverage(conn, ["covered"])
    with pytest.raises(ValueError, match="canonical section"):
        bind_coverage(library, proof, receipt_sha256=RECEIPT)


def test_strict_capture_reads_a_long_chapter_once_for_many_spans_but_query_guard_reads_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library, _ = seed(tmp_path)
    text = tmp_path / "many-passages.txt"
    text.write_text("\n\n".join(f"Paragraph {index}. " + "Exact original CC0 context. " * 50 for index in range(30)))
    library.import_book(text, {"id": "covered", "title": "Many passages", "author": "CC0", "language": "en"})
    monkeypatch.setattr(coverage, "SECTION_CACHE_BYTES", 1)
    with library.connect() as conn:
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        proof = capture_coverage(conn, ["covered"])
        assert proof["passages"] > 20
        chapter_reads = [sql for sql in statements if sql.startswith("SELECT book_id,text FROM sections WHERE id=")]
        assert len(chapter_reads) == 1, "Even an oversized chapter is retained until another chapter is needed"
    binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
    validate_coverage(library, proof, binding)
    original = library.connect
    statements = []

    @contextmanager
    def observe() -> Any:
        with original() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(library, "connect", observe)
    checked = validate_coverage(library, proof, binding)
    assert checked["valid"] and checked["cache_hit"]
    assert not any("FROM sections" in sql for sql in statements)


@pytest.mark.parametrize("changed_book", ["covered", "outside"])
def test_binding_detects_covered_race_after_snapshot_but_ignores_outside_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_book: str
) -> None:
    library, proof = seed(tmp_path)
    original = coverage.capture_coverage

    def race(conn: sqlite3.Connection, *args: Any, **kwargs: Any) -> dict[str, Any]:
        captured = original(conn, *args, **kwargs)
        with library.connect() as writer:
            writer.execute("UPDATE books SET title=title||' race' WHERE id=?", (changed_book,))
        return captured

    monkeypatch.setattr(coverage, "capture_coverage", race)
    if changed_book == "covered":
        with pytest.raises(ValueError, match="changed during"):
            bind_coverage(library, proof, receipt_sha256=RECEIPT)
    else:
        binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
        assert validate_coverage(library, proof, binding)["valid"]


def test_copied_database_requires_new_binding_even_with_identical_namespace(tmp_path: Path) -> None:
    library, proof = seed(tmp_path)
    binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
    destination = tmp_path / "copy"
    destination.mkdir()
    shutil.copyfile(library.db_path, destination / "library.sqlite3")
    copied = Library(destination)
    assert not validate_coverage(copied, proof, binding)["valid"]
    assert validate_coverage(copied, proof, bind_coverage(copied, proof, receipt_sha256=RECEIPT))["valid"]


def test_readonly_legacy_exact_scan_never_migrates_or_uses_claimed_sha_blindly(tmp_path: Path) -> None:
    library, proof = seed(tmp_path, tracked=False)
    before = hashlib.sha256(library.db_path.read_bytes()).hexdigest()

    class ReadOnly:
        db_path = library.db_path

        @contextmanager
        def connect(self) -> Any:
            connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                yield connection
            finally:
                connection.close()

    readonly = ReadOnly()
    assert validate_coverage(readonly, proof)["valid"]
    again = validate_coverage(readonly, proof)
    assert again["valid"] and not again["tracked"] and not again["cache_hit"]
    assert hashlib.sha256(library.db_path.read_bytes()).hexdigest() == before
    with library.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='book_index_epochs'").fetchone()
        conn.execute("UPDATE passages SET text='Tampered unchanged SHA' WHERE book_id='covered'")
    assert not validate_coverage(readonly, proof)["valid"]


def test_tracked_unbound_target_never_automatically_becomes_a_query_index(tmp_path: Path) -> None:
    library, proof = seed(tmp_path)
    result = validate_coverage(library, proof)
    assert not result["valid"] and "explicit" in result["reason"]


def test_cache_is_bounded_across_bindings(tmp_path: Path) -> None:
    library, proof = seed(tmp_path)
    coverage._CACHE.clear()
    for number in range(coverage.CACHE_SIZE + 3):
        binding = bind_coverage(library, proof, receipt_sha256=f"{number:064x}")
        assert validate_coverage(library, proof, binding)["valid"]
    assert len(coverage._CACHE) == coverage.CACHE_SIZE


@pytest.mark.parametrize("bad", [[], ["covered", "covered"], ["missing"], [True], [""]])
def test_invalid_or_unavailable_coverage_is_rejected(tmp_path: Path, bad: Any) -> None:
    library, _ = seed(tmp_path)
    with library.connect() as conn, pytest.raises(ValueError):
        capture_coverage(conn, bad)


def test_partial_cannot_be_labeled_full_and_proof_cannot_misreport_counts(tmp_path: Path) -> None:
    library, proof = seed(tmp_path)
    with library.connect() as conn, pytest.raises(ValueError, match="every indexed"):
        capture_coverage(conn, ["covered"], kind="full")
    with pytest.raises(ValueError):
        bind_coverage(library, proof | {"books": 999}, receipt_sha256=RECEIPT)
    binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
    with pytest.raises(ValueError):
        validate_coverage(library, proof, binding | {"schema_version": True})


def test_coverage_exceeds_old_sqlite_999_variable_limit_without_parameter_batches(tmp_path: Path) -> None:
    library, _ = seed(tmp_path)
    with library.connect() as conn:
        for number in range(1100):
            identity = f"extra-{number}"
            text = f"Owned original CC0 fixture {number}."
            conn.execute(
                "INSERT INTO books(id,title,author,language,work_id,edition,source_url,rights,status) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (identity, identity, "CC0 fixture", "en", identity, "Test", "", "CC0", "indexed"),
            )
            cursor = conn.execute(
                "INSERT INTO sections(book_id,ordinal,title,text) VALUES(?,?,?,?)", (identity, 0, "Test", text)
            )
            conn.execute(
                "INSERT INTO passages(book_id,section_id,start,end,text,sha256) VALUES(?,?,?,?,?,?)",
                (identity, cursor.lastrowid, 0, len(text), text, hashlib.sha256(text.encode()).hexdigest()),
            )
        proof = capture_coverage(conn, kind="full")
    assert proof["books"] == 1102
    binding = bind_coverage(library, proof, receipt_sha256=RECEIPT)
    assert validate_coverage(library, proof, binding)["valid"]
