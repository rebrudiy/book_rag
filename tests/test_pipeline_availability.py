"""Refresh attempts must not withdraw or relabel the last successful edition."""

from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError

import pytest
from fastapi.testclient import TestClient

from book_rag.api import create_app
from book_rag.library import Library
from book_rag.semantic import corpus_fingerprint
from book_rag.snapshots import create_snapshot

OLD_TEXT = "Amber bookmarks preserve the exact earlier quotation."
NEW_TEXT = "Azure bookmarks belong only to the successfully replaced edition."


def metadata(identity: str = "refresh") -> dict[str, Any]:
    return {
        "id": identity,
        "title": "Original indexed book",
        "author": "Original CC0 fixture author",
        "language": "en",
        "edition": "First edition",
        "source_url": "https://example.org/first",
        "rights": "CC0",
        "rights_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "coverage": "excerpt",
    }


def seed(tmp_path: Path) -> tuple[Library, dict[str, Any], dict[str, Any], dict[str, Any]]:
    library = Library(tmp_path / "library")
    source = tmp_path / "first.txt"
    source.write_text(OLD_TEXT)
    library.import_book(source, metadata())
    book = library.get_book("refresh")
    assert book is not None
    hit = library.search("Amber")["results"][0]
    saved = library.save({"kind": "passage", "passage_id": hit["id"], "title": "Retained first quotation"})
    return library, book, hit, saved


def assert_retained(library: Library, before: dict[str, Any], hit: dict[str, Any]) -> dict[str, Any]:
    current = library.get_book("refresh")
    assert current is not None
    for key in (
        "status",
        "local_path",
        "artifact_sha256",
        "content_sha256",
        "indexed_at",
        "title",
        "author",
        "edition",
        "language",
        "source_url",
        "rights",
        "rights_url",
        "source_metadata",
        "sections",
        "error",
    ):
        assert current[key] == before[key], key
    result = library.search("Amber")["results"][0]
    assert result["id"] == hit["id"] and result["verified"]
    assert result["citation"] == hit["citation"] and result["artifact_sha256"] == before["artifact_sha256"]
    assert not library.search("Azure")["results"]
    assert library.saved()[0]["passage_id"] == hit["id"]
    assert Path(current["local_path"]).read_text() == OLD_TEXT
    return current


@pytest.mark.parametrize("failure", ["empty", "invalid_encoding", "malformed_fb2", "missing", "corrupt_archive"])
def test_failed_source_refresh_keeps_verified_edition(tmp_path: Path, failure: str) -> None:
    library, before, hit, saved = seed(tmp_path)
    fingerprint = corpus_fingerprint(library)
    source = tmp_path / ("new.fb2" if failure == "malformed_fb2" else "new.txt")
    source.write_bytes(
        b""
        if failure == "empty"
        else b"\xff\xfe\xfd"
        if failure == "invalid_encoding"
        else b"<not-valid-fb2"
        if failure == "malformed_fb2"
        else NEW_TEXT.encode()
    )
    if failure == "missing":
        source.unlink()
    elif failure == "corrupt_archive":
        import hashlib

        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        corrupt = library.data_dir / "artifacts" / sha[:2] / f"{sha}.txt"
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_bytes(b"corrupt owned fixture")
    with pytest.raises((ValueError, FileNotFoundError, ParseError)):
        library.import_book(source, metadata() | {"title": "Uncommitted title", "edition": "Uncommitted edition"})
    current = assert_retained(library, before, hit)
    assert current["ingestion_status"] == "failed" and current["ingestion_error"]
    assert current["ingestion_local_path"] != current["local_path"]
    assert corpus_fingerprint(library) == fingerprint
    assert library.saved()[0]["artifact_url"] == saved["artifact_url"]
    with TestClient(create_app(library.data_dir)) as client:
        assert client.get(before["artifact_url"]).content == OLD_TEXT.encode()
        response = client.get("/api/search", params={"q": "Amber", "mode": "lexical"}).json()
        assert response["results"][0]["ingestion_warning"] in response["warnings"]
        assert response["results"][0]["edition"] == "First edition"
        assert client.get("/api/books/refresh").json()["ingestion_status"] == "failed"


@pytest.mark.parametrize("bad_metadata", [{"section_attributions": object()}, {"section_attributions": [None]}])
def test_metadata_failure_before_or_inside_transaction_preserves_everything(
    tmp_path: Path, bad_metadata: dict[str, Any]
) -> None:
    library, before, hit, _ = seed(tmp_path)
    source = tmp_path / "new.txt"
    source.write_text(NEW_TEXT)
    with pytest.raises((TypeError, ValueError)):
        library.import_book(source, metadata() | {"title": "Never publish", "rights": "Never publish"} | bad_metadata)
    current = assert_retained(library, before, hit)
    assert current["ingestion_status"] == "failed" and current["ingestion_error"]
    with library.connect() as conn:
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.execute("INSERT INTO passage_fts(passage_fts) VALUES ('integrity-check')")


def test_previous_edition_remains_available_during_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from book_rag import parsers

    library, before, hit, _ = seed(tmp_path)
    source = tmp_path / "new.txt"
    source.write_text(NEW_TEXT)
    original_parse = parsers.parse_book

    def inspect_parse(path: Path, fallback_title: str | None = None) -> Any:
        current = assert_retained(library, before, hit)
        assert current["ingestion_status"] == "downloaded"
        assert current["ingestion_local_path"] == str(source.resolve())
        return original_parse(path, fallback_title)

    monkeypatch.setattr(parsers, "parse_book", inspect_parse)
    library.import_book(source, metadata() | {"edition": "Second edition"})
    current = library.get_book("refresh")
    assert current is not None and current["edition"] == "Second edition"
    assert current["ingestion_status"] == current["status"] == "indexed"
    assert current["ingestion_local_path"] == current["local_path"] != before["local_path"]
    assert not library.search("Amber")["results"]
    assert library.search("Azure")["results"][0]["edition"] == "Second edition"
    assert library.saved()[0]["passage_id"] is None
    assert library.saved()[0]["artifact_url"] == before["artifact_url"]


def test_failed_deferred_and_skipped_retry_keep_attempt_path(tmp_path: Path) -> None:
    library, before, hit, _ = seed(tmp_path)
    attempt = str((tmp_path / "unavailable-download.epub").resolve())
    library.stage("refresh", "failed", "Provider unavailable", local_path=attempt)
    library.stage("refresh", "failed", "CLI repeats failure without a path")
    current = assert_retained(library, before, hit)
    assert current["ingestion_local_path"] == attempt
    library.stage("refresh", "deferred", "Retry after cooldown")
    assert library.search("Amber", mode="hybrid")["results"][0]["ingestion_status"] == "deferred"
    # Retry with unchanged archived bytes; success must clear the failed attempt.
    result = library.import_book(Path(before["local_path"]), metadata())
    assert result["skipped"]
    current = library.get_book("refresh")
    assert current is not None and current["ingestion_status"] == "indexed" and current["ingestion_error"] is None
    assert current["ingestion_local_path"] == current["local_path"]
    assert "ingestion_warning" not in library.search("Amber")["results"][0]


def test_valid_replacement_after_failed_refresh_publishes_only_new_edition(tmp_path: Path) -> None:
    library, before, hit, _ = seed(tmp_path)
    source = tmp_path / "replacement.txt"
    source.write_text("")
    replacement = metadata() | {"edition": "Second edition", "source_url": "https://example.org/second"}
    with pytest.raises(ValueError):
        library.import_book(source, replacement)
    assert_retained(library, before, hit)
    source.write_text(NEW_TEXT)
    assert not library.import_book(source, replacement)["skipped"]
    current = library.get_book("refresh")
    assert current is not None and current["status"] == current["ingestion_status"] == "indexed"
    assert current["ingestion_error"] is None and current["source_url"] == replacement["source_url"]
    assert current["local_path"] == current["ingestion_local_path"] != before["local_path"]
    assert library.passage(hit["id"]) is None and not library.search("Amber")["results"]
    result = library.search("Azure")["results"][0]
    assert result["edition"] == "Second edition" and "ingestion_warning" not in result
    assert library.saved()[0]["artifact_url"] == before["artifact_url"]
    with TestClient(create_app(library.data_dir)) as client:
        assert client.get(before["artifact_url"]).content == OLD_TEXT.encode()
        assert client.get(current["artifact_url"]).content == NEW_TEXT.encode()


def test_new_failure_retry_and_availability_filters_are_separate(tmp_path: Path) -> None:
    library, before, hit, _ = seed(tmp_path)
    library.stage("refresh", "failed", "Refresh failed", local_path=str(tmp_path / "refresh-new.txt"))
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    with pytest.raises(ValueError):
        library.import_book(empty, metadata("new"))
    assert_retained(library, before, hit)
    assert not library.search("Amber", book_id="new")["results"]
    with TestClient(create_app(library.data_dir)) as client:
        stats = client.get("/api/stats").json()
        assert stats["books"] == stats["searchable_books"] == 1
        assert stats["status_counts"] == {"indexed": 1, "failed": 1}
        assert stats["ingestion_status_counts"] == {"failed": 2}
        page = client.get("/api/books", params={"status": "indexed", "ingestion_status": "failed"}).json()
        assert page["total"] == 1 and page["books"][0]["id"] == "refresh"
        assert page["books"][0]["ingestion_error"] == "Refresh failed"
        assert client.get("/api/books", params={"status": "failed"}).json()["books"][0]["id"] == "new"
        assert client.get("/api/books", params={"ingestion_status": "not-a-stage"}).status_code == 422
    empty.write_text("Newly recovered source text.")
    library.import_book(empty, metadata("new"))
    assert library.stats()["books"] == 2
    assert library.stats()["ingestion_status_counts"] == {"indexed": 1, "failed": 1}


def test_duplicate_refresh_publishes_pipeline_and_current_path_atomically(tmp_path: Path) -> None:
    library, before, hit, _ = seed(tmp_path)
    other = tmp_path / "other.txt"
    other.write_text(NEW_TEXT)
    library.import_book(other, metadata("other"))
    result = library.import_book(other, metadata() | {"edition": "Replacement duplicate"})
    assert result["status"] == "duplicate" and result["duplicate_of"] == "other"
    current = library.get_book("refresh")
    assert current is not None and current["status"] == current["ingestion_status"] == "duplicate"
    assert current["local_path"] == current["ingestion_local_path"] != before["local_path"]
    assert library.passage(hit["id"]) is None and library.saved()[0]["passage_id"] is None
    assert library.saved()[0]["artifact_url"] == before["artifact_url"]
    assert library.stats()["status_counts"] == library.stats()["ingestion_status_counts"]


def test_migration_does_not_restore_legacy_failed_index_and_subset_cascades(tmp_path: Path) -> None:
    library, _, _, _ = seed(tmp_path)
    other = tmp_path / "other.txt"
    other.write_text(NEW_TEXT)
    library.import_book(other, metadata("legacy-failed"))
    with library.connect() as conn:
        conn.execute("UPDATE books SET status='failed',error='Legacy failure' WHERE id='legacy-failed'")
        conn.execute("DROP TABLE source_pipeline")
    migrated = Library(library.data_dir)
    legacy = migrated.get_book("legacy-failed")
    assert legacy is not None and legacy["status"] == legacy["ingestion_status"] == "failed"
    assert legacy["ingestion_error"] == "Legacy failure" and legacy["sections"]
    assert not migrated.search("Azure")["results"]
    destination = tmp_path / "owned-subset"
    create_snapshot(migrated, destination, ["refresh"])
    subset = Library(destination)
    with subset.connect() as conn:
        assert [row[0] for row in conn.execute("SELECT book_id FROM source_pipeline")] == ["refresh"]
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
