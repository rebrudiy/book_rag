"""Contributor attribution requires explicit audited source metadata."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from book_rag.attribution import section_attribution


@pytest.mark.parametrize("heading", ["Introduction", "Preface", "Notes", "BOOK I.", "A Translator's Preface"])
def test_headings_alone_do_not_verify_authorship(heading: str) -> None:
    result = section_attribution(heading, {"author": "Plato"})
    assert result == {"role": "unclassified", "author": "Plato", "attribution_basis": "book_metadata_unclassified"}


def test_explicit_editorial_rule_distinguishes_book_and_contributor() -> None:
    metadata = {
        "author": "Plato",
        "section_attributions": [
            {"title_contains": "INTRODUCTION AND ANALYSIS", "author": "Benjamin Jowett", "role": "editorial"},
        ],
    }
    assert section_attribution("INTRODUCTION AND ANALYSIS.", metadata) == {
        "role": "editorial",
        "author": "Benjamin Jowett",
        "attribution_basis": "explicit_section_attributions[0]",
    }
    assert section_attribution("BOOK I.", metadata)["author"] == "Plato"
    assert section_attribution("BOOK I.", metadata)["role"] == "unclassified"


def test_matching_normalizes_case_unicode_and_whitespace() -> None:
    metadata = {
        "author": "Author",
        "section_attributions": [
            {"title_contains": "Cafe\u0301 notes", "author": "Named editor", "role": "editorial"},
        ],
    }
    assert section_attribution("CAFÉ\n\nNOTES", metadata)["author"] == "Named editor"


def test_main_text_can_be_explicitly_verified() -> None:
    metadata = {
        "author": "Book author",
        "section_attributions": [
            {"title_contains": "Book I.", "author": "Verified author", "role": "main_text"},
        ],
    }
    assert section_attribution("BOOK I. The argument", metadata)["role"] == "main_text"
    assert section_attribution("BOOK II.", metadata)["role"] == "unclassified"


def test_conflicting_rules_do_not_choose_an_author() -> None:
    metadata = {
        "author": "Book author",
        "section_attributions": [
            {"title_contains": "Introduction", "author": "One editor", "role": "editorial"},
            {"title_contains": "Introduction", "author": "Other editor", "role": "editorial"},
        ],
    }
    result = section_attribution("Introduction", metadata)
    assert result["role"] == "unclassified"
    assert result["author"] == "Book author"
    assert result["attribution_basis"] == "conflicting_explicit_section_attributions"


def test_agreeing_rules_retain_all_rule_indices() -> None:
    metadata = {
        "author": "Book author",
        "section_attributions": [
            {"title_contains": "Introduction", "author": "Editor", "role": "editorial"},
            {"title_contains": "Analysis", "author": "Editor", "role": "editorial"},
        ],
    }
    result = section_attribution("Introduction and analysis", metadata)
    assert result["author"] == "Editor"
    assert result["attribution_basis"] == "explicit_section_attributions[0,1]"


@pytest.mark.parametrize(
    "rules",
    [
        {},
        "guess from title",
        ["editor"],
        [{}],
        [{"title_contains": "", "author": "Editor", "role": "editorial"}],
        [{"title_contains": "Notes", "author": "", "role": "editorial"}],
        [{"title_contains": "Notes", "author": "Editor", "role": "guessed"}],
    ],
)
def test_malformed_rules_fail_instead_of_becoming_verified(rules: object) -> None:
    with pytest.raises(ValueError, match="section_attributions"):
        section_attribution("Notes", {"author": "Author", "section_attributions": rules})


def test_curation_is_not_modified_by_classification() -> None:
    metadata = {
        "author": "Author",
        "section_attributions": [
            {"title_contains": "Notes", "author": "Editor", "role": "editorial"},
        ],
    }
    before = copy.deepcopy(metadata)
    section_attribution("Notes", metadata)
    assert metadata == before


def test_curated_republic_rule_is_narrow_and_names_verified_contributor() -> None:
    manifest = Path(__file__).resolve().parents[1] / "corpus/manifest.json"
    republic = next(e for e in json.loads(manifest.read_text()) if e["id"] == "pg1497-full")
    result = section_attribution("INTRODUCTION AND ANALYSIS.", republic)
    assert (result["role"], result["author"]) == ("editorial", "Benjamin Jowett")
    for heading in ("BOOK I.", "BOOK II.", "PERSONS OF THE DIALOGUE."):
        assert section_attribution(heading, republic)["role"] == "unclassified"
