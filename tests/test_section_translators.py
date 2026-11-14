"""A book's main translator does not establish the translator of an epigraph."""

import sqlite3
from pathlib import Path

import pytest

from book_rag.attribution import section_attribution
from book_rag.library import Library


def test_explicit_translator_and_unknown_rules_combine_without_inference() -> None:
    rule = {"title_contains": "Epigraph", "author": "Poet", "role": "main_text"}
    result = section_attribution(
        "Epigraph",
        {
            "author": "Novelist",
            "translator": "Main translator",
            "section_attributions": [rule, rule | {"translator": "Poem translator"}],
        },
    )
    assert result["author"] == "Poet"
    assert result["translator"] == "Poem translator"
    assert "translator" not in section_attribution("Chapter", {"author": "Novelist", "translator": "Main"})
    conflict = section_attribution(
        "Epigraph",
        {"section_attributions": [rule | {"translator": "One"}, rule | {"translator": "Two"}]},
    )
    assert conflict["role"] == "unclassified" and "translator" not in conflict


@pytest.mark.parametrize("translator", [None, 5, "", "   "])
def test_malformed_translator_is_not_verified(translator: object) -> None:
    with pytest.raises(ValueError, match="translator"):
        section_attribution(
            "Epigraph",
            {
                "section_attributions": [
                    {"title_contains": "Epigraph", "author": "Poet", "role": "main_text", "translator": translator}
                ]
            },
        )


def test_translator_survives_reader_search_batch_and_saved_provenance(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    source = tmp_path / "poem.html"
    source.write_text("<h1>Epigraph</h1><p>Silver birches preserve a borrowed poem.</p>")
    library.import_book(
        source,
        {
            "id": "book",
            "author": "Novelist",
            "translator": "Main translator",
            "section_attributions": [
                {"title_contains": "Epigraph", "author": "Poet", "role": "main_text", "translator": "Poem translator"}
            ],
        },
    )
    hit = library.search("birches")["results"][0]
    assert hit["section_translator"] == "Poem translator"
    assert "Перевод фрагмента: Poem translator" in hit["citation"]
    assert library.passages([hit["id"]])[0]["section_translator"] == "Poem translator"
    book = library.get_book("book")
    assert book is not None and book["sections"][0]["section_translator"] == "Poem translator"
    saved = library.save({"kind": "passage", "passage_id": hit["id"]})
    assert saved["provenance"]["section_translator"] == "Poem translator"
    library.import_book(source, {"id": "book", "author": "Novelist", "edition": "Changed"})
    assert library.saved()[0]["provenance"]["section_translator"] == "Poem translator"


def test_legacy_attribution_migration_preserves_original_rows(tmp_path: Path) -> None:
    directory = tmp_path / "legacy"
    directory.mkdir()
    with sqlite3.connect(directory / "library.sqlite3") as conn:
        conn.execute(
            "CREATE TABLE section_roles(section_id INTEGER PRIMARY KEY,section_author TEXT NOT NULL,"
            "section_role TEXT NOT NULL,attribution_basis TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO section_roles VALUES (1,'Known author','main_text','existing')")
    library = Library(directory)
    with library.connect() as conn:
        row = dict(conn.execute("SELECT * FROM section_roles").fetchone())
    assert row == {
        "section_id": 1,
        "section_author": "Known author",
        "section_role": "main_text",
        "attribution_basis": "existing",
        "section_translator": "",
    }
