"""Exports retain saved evidence and never invent live locations."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from book_rag.exporting import export_collection

ITEM: dict[str, Any] = {
    "id": 1,
    "kind": "passage",
    "title": "Useful evidence",
    "collection": "Philosophy",
    "text": "A verified quotation.\n\nIts second paragraph.",
    "citation": "Author. Book. Edition. Chapter, chars 0–45.",
    "note": "A note",
    "passage_id": 7,
    "reader_url": "/?book=book-1&section=3&passage=7",
    "provenance": {
        "book_id": "book-1",
        "section_id": 3,
        "source_url": "https://example.org/book",
        "rights": "CC0",
        "rights_url": "https://example.org/rights",
        "artifact_sha256": "abc123",
        "start": 0,
        "end": 45,
    },
}


def test_exports_snapshot_citation_source_and_provenance_without_mutation() -> None:
    original = deepcopy(ITEM)
    output = export_collection([ITEM])
    assert "> A verified quotation" in output
    assert "> Its second paragraph" in output
    assert "abc123" in output
    assert "https://example.org/book" in output
    assert "https://example.org/rights" in output
    assert "/?book=book-1&section=3&passage=7" in output
    payload = output.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(payload) == ITEM["provenance"]
    assert ITEM == original
    assert output == export_collection([ITEM])


def test_exact_collection_filter_and_queries() -> None:
    query = {"kind": "query", "title": "Question", "query": "What is knowledge?", "collection": "", "note": "Test"}
    output = export_collection([ITEM, query], collection="")
    assert "What is knowledge?" in output
    assert "Useful evidence" not in output
    assert "ссылка в читателе" not in output
    assert "Записей: 1" in output
    assert "Записей: 0" in export_collection([ITEM], collection="philosophy")


def test_invalidated_snapshot_retains_source_but_never_reader_link() -> None:
    item = ITEM | {"passage_id": None}
    output = export_collection([item])
    assert "> A verified quotation" in output
    assert "https://example.org/book" in output
    assert "Открыть в локальном читателе" not in output
    assert "текущая ссылка в читателе недоступна" in output
    # Even provenance with all old IDs must not cause a fabricated reader URL.
    assert "Открыть в локальном читателе" not in export_collection([ITEM | {"reader_url": None}])


def test_historical_export_link_survives_removed_live_passage() -> None:
    item = ITEM | {
        "passage_id": None,
        "reader_url": None,
        "historical_reader_url": "/?saved=1",
        "provenance": ITEM["provenance"] | {"canonical_version_id": "cv1-" + "a" * 64},
    }
    output = export_collection([item])
    assert "[Читать сохранённое издание](</?saved=1>)" in output
    assert "текущая ссылка в читателе недоступна" not in output
    for bad in ("/?saved=2", "https://example.org/?saved=1", "/?saved=1&book=other"):
        assert "[Читать сохранённое издание]" not in export_collection([item | {"historical_reader_url": bad}])


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "//evil.example/",
        "https://user:secret@example.org/book",
        "https://example.org/book\n<script>",
        "https://[bad-ipv6/book",
    ],
)
def test_unsafe_source_url_is_not_a_link(url: str) -> None:
    item = ITEM | {"provenance": ITEM["provenance"] | {"source_url": url, "rights_url": url}}
    output = export_collection([item])
    assert "Открыть оригинальный источник" not in output
    assert "Условия источника" not in output


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/?book=book-1&section=3&passage=7",
        "/?book=wrong&section=3&passage=7",
        "/?book=book-1&section=3&passage=8",
        "/?book=book-1&section=3&passage=7&passage=8",
    ],
)
def test_bad_reader_url_is_not_exported(url: str) -> None:
    assert "Открыть в локальном читателе" not in export_collection([ITEM | {"reader_url": url}])


def test_headings_notes_and_quote_html_are_literal() -> None:
    item = ITEM | {
        "title": "Heading\n# injected <img src=x onerror=alert(1)>",
        "note": "<script>alert(1)</script>\n![image](javascript:evil)\n```html\n<div>",
        "text": "<b>book text</b> & **not markup**",
    }
    output = export_collection([item])
    assert "<img" not in output and "<script" not in output and "<b>" not in output
    assert "&lt;script&gt;" in output
    assert "\n# injected" not in output
    assert "![image]" not in output
    assert "&amp;" in output


def test_legacy_saved_item_has_no_invented_metadata_or_location() -> None:
    output = export_collection([{"kind": "passage", "text": "Legacy quotation", "citation": "Old source"}])
    assert "Legacy quotation" in output
    assert "Old source" in output
    assert "```json" not in output
    assert "Открыть в локальном читателе" not in output


@pytest.mark.parametrize("value", ["not json", "[]", "null"])
def test_bad_provenance_json_does_not_break_export(value: str) -> None:
    output = export_collection([ITEM | {"provenance": value}])
    assert "A verified quotation" in output
    assert "```json" not in output


def test_provenance_json_and_backtick_fence_are_safe() -> None:
    provenance = {"title": "```\n<script>hi</script>", "source_url": "https://example.org/a(b)"}
    item = ITEM | {"provenance": json.dumps(provenance)}
    output = export_collection([item])
    assert "````json" in output
    assert "<script>" not in output
    assert "https://example.org/a%28b%29" in output
