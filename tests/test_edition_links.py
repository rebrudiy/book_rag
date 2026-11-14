"""Only confirmed, searchable editions belong in work navigation."""

from pathlib import Path

from fastapi.testclient import TestClient

from book_rag.api import create_app


def test_edition_navigation_requires_curated_identity_and_available_text(tmp_path: Path) -> None:
    app = create_app(tmp_path / "library")
    library = app.state.library
    for book_id, work_id, identity, text in [
        ("original", "confirmed-work", "curated", "The first edition evidence."),
        ("translation", "confirmed-work", "curated", "Перевод того же произведения."),
        ("provisional", "confirmed-work", "provisional", "An unverified catalog match."),
        ("different-work", "another-work", "curated", "Another work by the same author."),
        ("duplicate", "confirmed-work", "curated", "The first edition evidence."),
    ]:
        path = tmp_path / f"{book_id}.txt"
        path.write_text(text)
        library.import_book(
            path,
            {
                "id": book_id,
                "title": book_id,
                "author": "Author",
                "work_id": work_id,
                "work_identity_status": identity,
                "language": "ru" if book_id == "translation" else "en",
                "edition": f"Edition {book_id}",
                "translator": "Verified Translator" if book_id == "translation" else None,
                "coverage": "full",
            },
        )
    library.register({"id": "pending", "work_id": "confirmed-work", "work_identity_status": "curated"})
    with TestClient(app) as client:
        original = client.get("/api/books/original").json()
        assert original["related_editions_total"] == 1
        assert [item["id"] for item in original["related_editions"]] == ["translation"]
        assert original["related_editions"][0]["translator"] == "Verified Translator"
        assert [item["id"] for item in client.get("/api/books/translation").json()["related_editions"]] == ["original"]
        assert client.get("/api/books/provisional").json()["related_editions"] == []
        assert client.get("/api/books/different-work").json()["related_editions"] == []
        assert client.get("/api/books/duplicate").json()["related_editions"] == []
        assert client.get("/api/books/pending").json()["related_editions"] == []


def test_edition_navigation_is_bounded(tmp_path: Path) -> None:
    app = create_app(tmp_path / "library")
    for index in range(23):
        path = tmp_path / f"{index}.txt"
        path.write_text(f"Distinct text of edition number {index}.")
        app.state.library.import_book(
            path,
            {"id": str(index), "work_id": "work", "work_identity_status": "curated", "edition": str(index)},
        )
    book = app.state.library.get_book("0")
    assert book is not None
    assert book["related_editions_total"] == 22
    assert len(book["related_editions"]) == 20


def test_confirmed_full_work_lower_bound_excludes_uncurated_partial_and_periodicals(tmp_path: Path) -> None:
    app = create_app(tmp_path / "library")
    entries = [
        ("first", "same-work", "curated", "full", "book"),
        ("translation", "same-work", "curated", "full", "book"),
        ("unknown", "provisional-work", "provisional", "full", "book"),
        ("selection", "partial-work", "curated", "selected_chapters", "book"),
        ("issue", "periodical-work", "curated", "full", "periodical_issue"),
    ]
    for book_id, work_id, identity, coverage, work_type in entries:
        path = tmp_path / f"{book_id}.txt"
        path.write_text(f"Separate source text for {book_id}.")
        app.state.library.import_book(
            path,
            {
                "id": book_id,
                "work_id": work_id,
                "work_identity_status": identity,
                "coverage": coverage,
                "work_type": work_type,
            },
        )
    stats = app.state.library.stats()
    assert stats["books"] == 5 and stats["work_count_is_provisional"]
    assert stats["confirmed_full_works_lower_bound"] == 1
