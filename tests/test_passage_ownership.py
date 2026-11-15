"""A matching text/hash/slice cannot establish ownership of a foreign section."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from book_rag.api import create_app
from book_rag.library import Library, digest


def import_source(library: Library, directory: Path, identity: str, body: str, *, html: bool = False) -> None:
    path = directory / (identity + (".html" if html else ".txt"))
    path.write_text(body, encoding="utf-8")
    result = library.import_book(
        path,
        {
            "id": identity,
            "title": f"Original CC0 source {identity}",
            "author": f"Original CC0 author {identity}",
            "language": "en",
            "edition": f"CC0 edition {identity}",
            "source_url": f"https://example.org/ownership/{identity}",
            "rights": "CC0 1.0 · original ownership fixture",
        },
    )
    assert result["status"] == "indexed"


def test_foreign_book_attribution_rejected_by_search_single_batch_save_and_api(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    import_source(library, tmp_path, "a", "Amber ownership preserves an exact earlier quotation 🦉.")
    import_source(library, tmp_path, "b", "Bronze context belongs only to another author and source.")
    original = library.search("Amber")["results"][0]
    valid = library.search("Bronze")["results"][0]
    saved = library.save({"kind": "passage", "title": "Legitimate original note", "passage_id": original["id"]})
    with library.connect() as connection:
        # Both FK endpoints remain valid. Text, hash and Unicode offsets still
        # match section A, but source/author/artifact metadata would come from B.
        connection.execute("UPDATE passages SET book_id='b' WHERE id=?", (original["id"],))
        row = connection.execute(
            "SELECT p.*,s.book_id section_book_id,s.text section_text FROM passages p "
            "JOIN sections s ON s.id=p.section_id WHERE p.id=?",
            (original["id"],),
        ).fetchone()
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert row["section_book_id"] == "a" and row["book_id"] == "b"
        assert row["sha256"] == digest(row["text"])
        assert row["section_text"][row["start"] : row["end"]] == row["text"]
    assert library.passage(original["id"]) is None
    batch = library.passages([original["id"], valid["id"], original["id"]])
    assert [hit["id"] for hit in batch] == [valid["id"]]
    assert batch[0]["verified"] and batch[0]["author"] == "Original CC0 author b"
    for mode in ("baseline", "lexical"):
        for scope in (None, "a", "b"):
            assert library.search("Amber", mode=mode, book_id=scope)["results"] == []
    with pytest.raises(ValueError, match="verified passage"):
        library.save({"kind": "passage", "title": "Must not misattribute", "passage_id": original["id"]})
    retained = library.get_saved(saved["id"])
    assert retained is not None and "reader_url" not in retained
    assert retained["text"] == original["text"] and retained["citation"] == original["citation"]
    assert retained["provenance"]["book_id"] == "a"
    historical = library.saved_context(saved["id"])
    assert historical is not None and historical["passage"] is not None
    assert historical["passage"]["citation"] == original["citation"]
    assert len(library.saved()) == 1
    with TestClient(create_app(library.data_dir)) as client:
        assert client.get(f"/api/passages/{original['id']}").status_code == 404
        response = client.post(
            "/api/saved", json={"kind": "passage", "title": "Invalid API note", "passage_id": original["id"]}
        )
        assert response.status_code == 400
        assert client.get("/api/search", params={"q": "Amber", "mode": "lexical"}).json()["results"] == []
    assert len(library.saved()) == 1


@pytest.mark.parametrize("mode", ["baseline", "lexical"])
def test_misowned_high_rank_hits_cannot_consume_pool_before_limit(tmp_path: Path, mode: str) -> None:
    library = Library(tmp_path / "library")
    short_sources = "<h1>Original CC0 chapter fixtures</h1>" + "".join(
        f"<h2>Original section {number}</h2><p>ownershipneedle.</p>" for number in range(205)
    )
    import_source(library, tmp_path, "a", short_sources, html=True)
    import_source(library, tmp_path, "b", "Original CC0 valid context. " + "reading details " * 75 + "ownershipneedle.")
    valid = library.search("ownershipneedle", book_id="b")["results"][0]
    with library.connect() as connection:
        damaged = [row[0] for row in connection.execute("SELECT id FROM passages WHERE book_id='a'")]
        assert len(damaged) > 200
        connection.execute("UPDATE passages SET book_id='b' WHERE book_id='a'")
        high_ranks = connection.execute(
            "SELECT rowid FROM passage_fts WHERE passage_fts MATCH 'ownershipneedle' "
            "ORDER BY bm25(passage_fts,1.0,2.0,1.5,1.2),rowid LIMIT 200"
        ).fetchall()
        assert all(row[0] in damaged for row in high_ranks), "Fixture must poison the entire old pool"
    for scope in (None, "b"):
        hits = library.search("ownershipneedle", mode=mode, book_id=scope, limit=1)["results"]
        assert [hit["id"] for hit in hits] == [valid["id"]]
        assert hits[0]["verified"] and hits[0]["citation"] == valid["citation"]
    assert library.passages(damaged[:3]) == []
