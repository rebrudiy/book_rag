"""Whole-library audit must independently detect corrupt evidence and originals."""

from pathlib import Path

from book_rag.integrity import audit_library
from book_rag.library import Library


def prepare(tmp_path: Path) -> tuple[Library, Path]:
    library = Library(tmp_path / "data")
    source = tmp_path / "book.txt"
    source.write_text("Exact evidence 🌍.\n\nAnother paragraph.")
    library.import_book(source, {"id": "book", "title": "Title", "author": "Author"})
    book = library.get_book("book")
    assert book is not None
    return library, Path(book["local_path"])


def test_complete_audit_and_missing_original_are_distinct(tmp_path: Path) -> None:
    library, source = prepare(tmp_path)
    report = audit_library(library, artifacts=True)
    assert report["valid"] and report["passages_checked"] and report["originals_complete"]
    source.unlink()
    report = audit_library(library, artifacts=True)
    assert not report["valid"] and report["canonical_valid"] and not report["originals_complete"]
    assert report["status"] == "incomplete" and report["warnings"]


def test_canonical_and_passage_corruption_detected(tmp_path: Path) -> None:
    library, _ = prepare(tmp_path)
    with library.connect() as conn:
        conn.execute("UPDATE sections SET text='Tampered'")
    report = audit_library(library)
    assert not report["valid"]
    assert {error["error"] for error in report["errors"]} == {"citation_integrity", "canonical_content_hash"}


def test_changed_artifact_not_misreported_as_transcription_check(tmp_path: Path) -> None:
    library, source = prepare(tmp_path)
    source.write_text("Changed artifact bytes.")
    assert audit_library(library)["valid"]
    report = audit_library(library, artifacts=True)
    assert not report["valid"] and report["errors"][0]["error"] == "original_artifact_hash"
