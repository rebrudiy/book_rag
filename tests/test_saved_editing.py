"""Annotations remain editable while quotation snapshots stay immutable."""

from pathlib import Path

from fastapi.testclient import TestClient

from book_rag.api import create_app


def test_edit_annotation_preserves_exact_historical_evidence_and_exports(tmp_path: Path) -> None:
    app = create_app(tmp_path / "library")
    path = tmp_path / "book.txt"
    path.write_text("A preserved quotation with a silver birch.")
    library = app.state.library
    library.import_book(path, {"id": "book", "title": "Book", "edition": "First"})
    hit = library.search("birch")["results"][0]
    saved = library.save({"kind": "passage", "passage_id": hit["id"], "note": "First note", "collection": "Old"})
    before = library.saved_context(saved["id"])
    with TestClient(app) as client:
        result = client.patch(
            f"/api/saved/{saved['id']}",
            json={"title": "Corrected title", "note": "A revised 🦉 note", "collection": "New"},
        )
        assert result.status_code == 200
        edited = result.json()
        assert (edited["title"], edited["note"], edited["collection"]) == (
            "Corrected title",
            "A revised 🦉 note",
            "New",
        )
        for field in ("text", "citation", "provenance", "created_at", "historical_reader_url", "passage_id"):
            assert edited[field] == saved[field]
        assert library.saved_context(saved["id"]) == before
        export = client.get("/api/saved/export", params={"collection": "New"}).text
        assert "Corrected title" in export and "A revised 🦉 note" in export and hit["text"].removesuffix(".") in export
        assert "Записей: 0" in client.get("/api/saved/export", params={"collection": "Old"}).text
        assert client.patch(f"/api/saved/{saved['id']}", json={"note": "", "collection": ""}).status_code == 200


def test_only_annotation_fields_can_change_and_invalid_edits_leave_record_untouched(tmp_path: Path) -> None:
    app = create_app(tmp_path / "library")
    library = app.state.library
    item = library.save({"kind": "query", "query": "Find a situation", "title": "Original", "note": "Keep"})
    with TestClient(app) as client:
        url = f"/api/saved/{item['id']}"
        for changes, code in [
            ({"text": "Forged quotation"}, 422),
            ({"provenance": {}}, 422),
            ({"query": "Replace the saved query"}, 422),
            ({"note": "x" * 10001}, 422),
            ({"collection": None}, 400),
            ({}, 400),
        ]:
            assert client.patch(url, json=changes).status_code == code
            assert library.get_saved(item["id"]) == item
        assert client.patch("/api/saved/999", json={"note": "missing"}).status_code == 404
        assert client.patch(url, json={"title": "Updated search", "note": "Reconsidered"}).status_code == 200
        updated = library.get_saved(item["id"])
        assert updated is not None and updated["query"] == "Find a situation"


def test_edit_survives_book_replacement_without_recreating_quote_snapshot(tmp_path: Path) -> None:
    app = create_app(tmp_path / "library")
    library = app.state.library
    source = tmp_path / "first.txt"
    source.write_text("Historical amber quotation.")
    library.import_book(source, {"id": "book"})
    hit = library.search("amber")["results"][0]
    saved = library.save({"kind": "passage", "passage_id": hit["id"]})
    source.write_text("A different current edition.")
    library.import_book(source, {"id": "book"})
    edited = library.update_saved(saved["id"], {"note": "Revisited after replacement"})
    assert edited is not None and edited["passage_id"] is None
    assert edited["provenance"] == saved["provenance"]
    context = library.saved_context(saved["id"])
    assert context is not None and context["passage"]["text"] == hit["text"]
