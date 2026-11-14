"""Regression tests for transactional indexing and citation provenance."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from book_rag.api import create_app
from book_rag.library import Library, chunk_spans


@pytest.fixture()
def library(tmp_path: Path) -> Library:
    return Library(tmp_path / "library")


def import_text(library: Library, tmp_path: Path, text: str, book_id: str = "test") -> dict:
    path = tmp_path / f"{book_id}.txt"
    path.write_text(text, encoding="utf-8")
    return library.import_book(
        path,
        {
            "id": book_id,
            "title": "A Test Book",
            "author": "An Author",
            "language": "en",
            "edition": "Test edition",
            "work_id": "test-work",
            "source_url": "https://example.org/book",
            "rights": "CC0",
        },
    )


def test_exact_spans_and_unicode() -> None:
    text = ("Книга — мысль и мир.\n\n" * 300) + "Конец."
    spans = chunk_spans(text)
    assert len(spans) > 1
    assert all(0 <= a < b <= len(text) and b - a <= 2000 for a, b in spans)
    assert "".join(text[a:b] for a, b in spans).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(
        " ", ""
    )
    assert all(b <= spans[i + 1][0] for i, (_, b) in enumerate(spans[:-1]))


def test_import_search_reader_and_resume(library: Library, tmp_path: Path) -> None:
    first = import_text(library, tmp_path, "Knowledge grows through curiosity.\n\nA second paragraph.")
    assert first["status"] == "indexed"
    stats = library.stats()
    assert stats["books"] == 1
    hits = library.search("curiosity")["results"]
    assert len(hits) == 1 and hits[0]["verified"]
    hit = hits[0]
    book = library.get_book("test")
    assert book is not None
    section = book["sections"][0]
    assert section["text"][hit["start"] : hit["end"]] == hit["text"]
    passage = library.passage(hit["id"])
    assert passage is not None and passage["verified"]
    assert import_text(library, tmp_path, "Knowledge grows through curiosity.\n\nA second paragraph.")["skipped"]
    assert library.stats() == stats


def test_reindex_removes_old_fts_and_preserves_saved(library: Library, tmp_path: Path) -> None:
    import_text(library, tmp_path, "Elephant curiosity.")
    hit = library.search("Elephant")["results"][0]
    library.save({"kind": "passage", "title": "Note", "passage_id": hit["id"]})
    import_text(library, tmp_path, "Penguin discovery.")
    assert not library.search("Elephant")["results"]
    assert library.search("Penguin")["results"]
    assert library.saved()[0]["text"] == hit["text"]
    assert library.saved()[0]["passage_id"] is None


def test_dedup_and_work_link(library: Library, tmp_path: Path) -> None:
    import_text(library, tmp_path, "Identical canonical text.")
    duplicate = import_text(library, tmp_path, "Identical canonical text.", "copy")
    assert duplicate["status"] == "duplicate"
    assert library.stats()["books"] == 1
    assert library.stats()["status_counts"]["duplicate"] == 1


def test_query_syntax_filters_and_abstention(library: Library, tmp_path: Path) -> None:
    import_text(library, tmp_path, "Philosophy explores knowledge.")
    assert not library.search('" OR NOT * ^ : ( )')["results"]
    assert not library.search("quantumunicornxyz")["results"]
    assert not library.search("knowledge", language="ru")["results"]
    assert not library.search("knowledge", book_id="missing")["results"]
    assert library.research("quantumunicornxyz")["abstained"]
    assert library.research("knowledge")["method"] == "extractive"


def test_corruption_suppressed(library: Library, tmp_path: Path) -> None:
    import_text(library, tmp_path, "Verified quotation.")
    with library.connect() as conn:
        conn.execute("UPDATE sections SET text='tampered'")
    result = library.search("quotation")
    assert not result["results"] and result["warnings"]


def test_semantic_fallback_is_explicit(library: Library, tmp_path: Path) -> None:
    import_text(library, tmp_path, "Verified quotation.")
    result = library.search("quotation", mode="hybrid")
    assert result["mode"] == "lexical" and result["warnings"]


def test_pipeline_failure_visible(library: Library, tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("")
    with pytest.raises(ValueError):
        library.import_book(path, {"id": "empty"})
    assert library.stats()["status_counts"] == {"failed": 1}


def test_api_persistence_validation_and_reader(tmp_path: Path) -> None:
    app = create_app(tmp_path / "api")
    import_text(app.state.library, tmp_path, "A curiosity in the library.")
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/api/books/test").status_code == 200
        assert client.get("/api/books/missing").status_code == 404
        assert client.get("/api/search", params={"q": "curiosity", "limit": 999}).status_code == 422
        hit = client.get("/api/search", params={"q": "curiosity"}).json()["results"][0]
        assert client.get(f"/api/passages/{hit['id']}").json()["verified"]
        saved = client.post(
            "/api/saved", json={"kind": "passage", "title": "Research", "passage_id": hit["id"], "collection": "Ideas"}
        )
        assert saved.status_code == 201
        assert client.get("/api/saved").json()["items"][0]["text"] == hit["text"]
        item_id = saved.json()["id"]
        assert client.delete(f"/api/saved/{item_id}").status_code == 200
        assert client.delete(f"/api/saved/{item_id}").status_code == 404
        assert client.post("/api/saved", json={"kind": "passage", "passage_id": -1}).status_code == 400


@pytest.mark.parametrize("changed_text", [True, False])
def test_reimport_provenance_is_atomic(library: Library, tmp_path: Path, changed_text: bool) -> None:
    import_text(library, tmp_path, "Original evidence.")
    path = tmp_path / "test.txt"
    if changed_text:
        path.write_text("Replacement evidence.")
    metadata = {
        "id": "test",
        "title": "New title",
        "author": "New Author",
        "edition": "Edition B",
        "source_url": "https://example.org/new",
        "rights": "CC BY 4.0",
        "work_id": "new-work",
        "language": "en",
        "coverage": "excerpt",
        "retrieved_at": "2026-11-14",
    }
    assert not library.import_book(path, metadata)["skipped"]
    hit = library.search("evidence")["results"][0]
    assert hit["edition"] == "Edition B" and hit["source_url"] == metadata["source_url"]
    assert hit["work_id"] == "new-work" and hit["rights"] == "CC BY 4.0"
    assert "Edition B" in hit["citation"] and "New title" in hit["citation"]
    book = library.get_book("test")
    assert book is not None and book["source_metadata"]["coverage"] == "excerpt"


def test_missing_import_is_failed(library: Library, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        library.import_book(tmp_path / "nonexistent.txt", {"id": "missing"})
    assert library.stats()["status_counts"]["failed"] == 1


def test_saved_provenance_survives_edition_replacement(library: Library, tmp_path: Path) -> None:
    import_text(library, tmp_path, "Original unique evidence.")
    hit = library.search("unique")["results"][0]
    saved = library.save({"kind": "passage", "title": "Original", "passage_id": hit["id"]})
    assert saved["reader_url"] == hit["reader_url"]
    assert saved["provenance"]["source_url"] == "https://example.org/book"
    assert saved["provenance"]["artifact_sha256"]
    import_text(library, tmp_path, "Replacement unrelated text.")
    saved = library.saved()[0]
    assert "reader_url" not in saved
    assert saved["provenance"]["edition"] == "Test edition"
    assert saved["text"] == "Original unique evidence."


def test_parser_version_upgrade_reindexes_unchanged_artifact(library: Library, tmp_path: Path) -> None:
    import_text(library, tmp_path, "A source with unchanged bytes.")
    before = library.search("unchanged")["results"][0]
    with library.connect() as conn:
        conn.execute("UPDATE index_versions SET fingerprint='previous-parser'")
    report = import_text(library, tmp_path, "A source with unchanged bytes.")
    assert not report["skipped"]
    after = library.search("unchanged")["results"][0]
    assert after["id"] != before["id"]
    assert after["text"] == before["text"]


def test_invalid_end_offset_suppressed(library: Library, tmp_path: Path) -> None:
    import_text(library, tmp_path, "Exact quotation.")
    with library.connect() as conn:
        conn.execute("UPDATE passages SET end=end+100")
    result = library.search("quotation")
    assert not result["results"] and result["warnings"]


def test_explicit_editor_contribution_survives_reindex(library: Library, tmp_path: Path) -> None:
    path = tmp_path / "book.html"
    path.write_text(
        "<h1>INTRODUCTION AND ANALYSIS</h1><p>The editor presents a critical introduction.</p>"
        "<h1>BOOK I</h1><p>A dialogue on the meaning of justice.</p>"
    )
    metadata = {
        "id": "dialogue",
        "title": "Republic",
        "author": "Plato",
        "edition": "Jowett translation",
        "section_attributions": [
            {"title_contains": "INTRODUCTION AND ANALYSIS", "author": "Benjamin Jowett", "role": "editorial"}
        ],
    }
    library.import_book(path, metadata)
    hit = library.search("critical introduction")["results"][0]
    assert hit["author"] == "Plato" and hit["section_author"] == "Benjamin Jowett"
    assert hit["section_role"] == "editorial" and "Benjamin Jowett" in hit["citation"]
    assert "Plato" in hit["citation"]
    assert library.search("Benjamin Jowett")["results"]
    book = library.get_book("dialogue")
    assert book is not None and book["sections"][0]["section_author"] == "Benjamin Jowett"
    # Changing an explicit rule invalidates the prior index even with unchanged file bytes.
    metadata["section_attributions"][0]["author"] = "A corrected contributor"
    assert not library.import_book(path, metadata)["skipped"]
    assert library.search("critical")["results"][0]["section_author"] == "A corrected contributor"
    assert not library.search("Benjamin Jowett")["results"]


def test_api_bad_query_is_client_error(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "api")) as client:
        assert client.get("/api/search", params={"q": "   "}).status_code == 400
        too_many = " ".join(f'"phrase{i}"' for i in range(9))
        assert client.get("/api/search", params={"q": too_many}).status_code == 400
        assert client.post("/api/research", json={"query": "   "}).status_code == 400


def test_reranker_unavailable_fallback_is_labeled(
    library: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_text(library, tmp_path, "Curiosity encourages discovery.")

    def unavailable(*args: object, **kwargs: object) -> dict:
        return {"available": False, "results": [], "warnings": ["Reranker unavailable"]}

    monkeypatch.setattr("book_rag.reranking.rerank", unavailable)
    result = library.search("curiosity", mode="reranked")
    assert result["mode"] == "lexical" and result["results"]
    assert "Reranker unavailable" in result["warnings"]


def test_catalog_paging_filters_and_unicode_literal_query(library: Library, tmp_path: Path) -> None:
    for n in range(5):
        import_text(library, tmp_path, f"Text number {n}.", f"book{n}")
    library.register({"id": "pending", "title": "Awaiting download", "author": "Тестовый Автор"})
    first = library.book_page(status="indexed", limit=2)
    second = library.book_page(status="indexed", offset=2, limit=2)
    last = library.book_page(status="indexed", offset=4, limit=2)
    assert first["total"] == 5 and first["has_more"] and second["has_more"]
    assert not last["has_more"] and len(last["books"]) == 1
    assert {b["id"] for b in first["books"]}.isdisjoint(b["id"] for b in second["books"])
    assert len(library.books()) == 6  # Internal complete-corpus consumers remain complete.
    assert library.book_page(q="тестовый автор")["total"] == 1
    assert library.book_page(q="%_", status="indexed")["total"] == 0
    assert library.book_page(status="discovered")["books"][0]["id"] == "pending"


def test_batch_passages_keep_order_and_verify_without_full_chapters(library: Library, tmp_path: Path) -> None:
    import_text(library, tmp_path, "First curiosity.\n\n" * 200)
    hits = library.search("curiosity", limit=3)["results"]
    ids = [h["id"] for h in hits]
    batch = library.passages(list(reversed(ids)) + [9999999, ids[0]])
    assert [h["id"] for h in batch] == list(reversed(ids)) + [ids[0]]
    assert all(h["verified"] and "context" not in h and "section_text" not in h for h in batch)
    with library.connect() as conn:
        conn.execute("UPDATE passages SET sha256='corrupt' WHERE id=?", (ids[0],))
    assert not library.passages([ids[0]])[0]["verified"]
    library.stage("test", "failed", "Controlled import error")
    assert library.passages(ids) == []
