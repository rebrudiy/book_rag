"""Snapshot isolation, FTS integrity and safe failure regression tests."""

from pathlib import Path

import pytest

from book_rag.library import Library
from book_rag.snapshots import create_snapshot


def seed(tmp_path: Path) -> Library:
    library = Library(tmp_path / "live")
    for identity, text in (("one", "Curiosity and discovery."), ("two", "History and memory.")):
        source = tmp_path / f"{identity}.txt"
        source.write_text(text)
        library.import_book(source, {"id": identity, "title": identity, "author": "A", "edition": "Original"})
    return library


def test_consistent_snapshot_and_independent_later_changes(tmp_path: Path) -> None:
    library = seed(tmp_path)
    hit = library.search("curiosity")["results"][0]
    library.save({"kind": "passage", "passage_id": hit["id"]})
    destination = tmp_path / "snapshot"
    report = create_snapshot(library, destination)
    snapshot = Library(destination)
    assert report["stats"]["books"] == 2
    assert snapshot.saved()[0]["text"] == hit["text"]
    source = tmp_path / "one.txt"
    source.write_text("Completely replaced.")
    library.import_book(source, {"id": "one"})
    assert snapshot.search("curiosity")["results"]
    assert not library.search("curiosity")["results"]


def test_subset_removes_fts_rows_and_personal_notes(tmp_path: Path) -> None:
    library = seed(tmp_path)
    hit = library.search("history")["results"][0]
    library.save({"kind": "passage", "passage_id": hit["id"]})
    destination = tmp_path / "subset"
    create_snapshot(library, destination, ["one"], include_saved=False)
    snapshot = Library(destination)
    assert snapshot.stats()["books"] == 1 and not snapshot.saved()
    assert not snapshot.search("history")["results"]
    assert snapshot.search("curiosity")["results"]
    with snapshot.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM passage_fts").fetchone()[0] == 1
    assert library.stats()["books"] == 2 and library.saved()


def test_missing_book_fails_atomically_and_never_overwrites(tmp_path: Path) -> None:
    library = seed(tmp_path)
    destination = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="not indexed"):
        create_snapshot(library, destination, ["absent"])
    assert not destination.exists() and not list(tmp_path.glob("snapshot.staging-*"))
    create_snapshot(library, destination)
    with pytest.raises(FileExistsError):
        create_snapshot(library, destination)
    assert Library(destination).stats()["books"] == 2
