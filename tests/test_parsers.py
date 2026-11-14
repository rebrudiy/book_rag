"""Format, provenance and download regression checks on synthetic source files."""

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from defusedxml.common import DefusedXmlException
from pypdf import PdfWriter

from book_rag.parsers import normalize_text, parse_book
from book_rag.sources import download_source, load_manifest


def test_normalization_is_deterministic_and_idempotent() -> None:
    value = "  Cafe\u0301\t monde\r\nwrapped\r\n\r\nSecond\tparagraph.  "
    assert normalize_text(value) == "Café monde wrapped\n\nSecond paragraph."
    assert normalize_text(normalize_text(value)) == normalize_text(value)


def test_text_extracts_metadata_chapters_and_removes_gutenberg_license(tmp_path: Path) -> None:
    path = tmp_path / "book.txt"
    path.write_text(
        "Title: Sample\nAuthor: Writer\nLanguage: English\n\n"
        "*** START OF THE PROJECT GUTENBERG EBOOK SAMPLE ***\n\n"
        "CHAPTER I\n\nWrapped\nparagraph.\n\nCHAPTER II\n\nOther paragraph.\n\n"
        "*** END OF THE PROJECT GUTENBERG EBOOK SAMPLE ***\nLicense noise"
    )
    parsed = parse_book(path)
    assert (parsed.title, parsed.author) == ("Sample", "Writer")
    assert [s.title for s in parsed.sections] == ["CHAPTER I", "CHAPTER II"]
    assert parsed.sections[0].text == "Wrapped paragraph."
    assert "License" not in parsed.sections[-1].text


def test_legacy_text_is_not_silently_corrupted(tmp_path: Path) -> None:
    path = tmp_path / "legacy.txt"
    path.write_bytes("Привет".encode("cp1251"))
    with pytest.raises(ValueError, match="UTF-8"):
        parse_book(path)


def test_html_headings_notes_and_unsafe_markup(tmp_path: Path) -> None:
    path = tmp_path / "book.html"
    path.write_text(
        '<html lang="ru"><head><title>Книга</title><meta name="author" content="Автор"></head>'
        '<body><div id="pg-header"><p>Boilerplate</p></div><h1>Глава</h1>'
        '<p>Один <em>два</em> три.<a href="#note">[1]</a></p><h2>Notes</h2>'
        '<aside role="doc-footnote"><p>[1] Footnote text.</p></aside>'
        '<script>alert(1)</script><div id="pg-footer">License</div></body></html>'
    )
    parsed = parse_book(path)
    assert (parsed.title, parsed.author, parsed.language) == ("Книга", "Автор", "ru")
    assert [s.title for s in parsed.sections] == ["Глава", "Notes"]
    assert "Footnote text" in parsed.sections[1].text
    assert all("alert" not in s.text and "Boilerplate" not in s.text for s in parsed.sections)


def make_epub(path: Path, href: str = "text/ch2.xhtml") -> None:
    with ZipFile(path, "w") as z:
        z.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
        )
        z.writestr(
            "OPS/book.opf",
            '<package xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<metadata><dc:title>Test EPUB</dc:title><dc:creator>Writer</dc:creator>"
            "<dc:language>en</dc:language></metadata>"
            '<manifest><item id="second" href="' + href + '" media-type="application/xhtml+xml"/>'
            '<item id="first" href="text/ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="first"/><itemref idref="second"/></spine></package>',
        )
        z.writestr("OPS/text/ch1.xhtml", "<html><body><h1>First</h1><p>First paragraph.</p></body></html>")
        z.writestr("OPS/text/ch2.xhtml", "<html><body><h1>Second</h1><p>Second paragraph.</p></body></html>")


def test_epub_uses_spine_order_and_package_metadata(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    make_epub(path)
    parsed = parse_book(path)
    assert (parsed.title, parsed.author, parsed.language) == ("Test EPUB", "Writer", "en")
    assert [s.title for s in parsed.sections] == ["First", "Second"]


def test_epub_footer_document_title_does_not_label_remaining_book_text(tmp_path: Path) -> None:
    path = tmp_path / "footnote.epub"
    make_epub(path)
    with ZipFile(path) as source:
        files = {name: source.read(name) for name in source.namelist()}
    files["OPS/text/ch2.xhtml"] = (
        "<html><head><title>THE FULL PROJECT GUTENBERG™ LICENSE</title></head><body>"
        '<div class="footnote"><p>[1] The earlier chapter note.</p></div>'
        "<h2>XXVIII</h2><p>The final chapter is still part of the book.</p>"
        '<div id="pg-footer"><h2>THE FULL PROJECT GUTENBERG™ LICENSE</h2><p>License text.</p></div>'
        "</body></html>"
    ).encode()
    with ZipFile(path, "w") as target:
        for name, data in files.items():
            target.writestr(name, data)
    parsed = parse_book(path)
    assert [s.title for s in parsed.sections] == ["First", "XXVIII"]
    assert parsed.sections[0].text == "First paragraph.\n\n[1] The earlier chapter note."
    assert parsed.sections[1].text == "The final chapter is still part of the book."


def test_epub_generated_next_heading_title_does_not_move_continuation(tmp_path: Path) -> None:
    path = tmp_path / "continuation.epub"
    make_epub(path)
    with ZipFile(path) as source:
        files = {name: source.read(name) for name in source.namelist()}
    files["OPS/text/ch2.xhtml"] = (
        b"<html><head><title>Second</title></head><body><p>Still the first chapter.</p>"
        b"<h1>Second</h1><p>The second chapter.</p></body></html>"
    )
    with ZipFile(path, "w") as target:
        for name, data in files.items():
            target.writestr(name, data)
    parsed = parse_book(path)
    assert [(s.title, s.text) for s in parsed.sections] == [
        ("First", "First paragraph.\n\nStill the first chapter."),
        ("Second", "The second chapter."),
    ]


def test_html_inline_only_div_bylines_are_preserved_once(tmp_path: Path) -> None:
    path = tmp_path / "bylines.html"
    path.write_text(
        "<html><body><h1>Criticism</h1><div>By <em>Named Critic</em></div>"
        "<div><p>The critical essay.</p><div><span>Signed Critic</span></div></div>"
        '<div id="pg-footer"><div>Provider license.</div></div></body></html>'
    )
    parsed = parse_book(path)
    assert [(s.title, s.text) for s in parsed.sections] == [
        ("Criticism", "By Named Critic\n\nThe critical essay.\n\nSigned Critic")
    ]


@pytest.mark.parametrize("href", ["../../outside.xhtml", "https://example.com/book.xhtml", "/absolute", "..\\evil"])
def test_epub_rejects_nonlocal_paths(tmp_path: Path, href: str) -> None:
    path = tmp_path / "book.epub"
    make_epub(path, href)
    with pytest.raises(ValueError, match="unsafe|escapes"):
        parse_book(path)


def test_fb2_preserves_nested_sections_and_note_body(tmp_path: Path) -> None:
    path = tmp_path / "book.fb2"
    path.write_text(
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
        "<description><title-info><book-title>Книга</book-title><author><first-name>Иван</first-name>"
        "<last-name>Иванов</last-name></author><lang>ru</lang></title-info></description>"
        "<body><section><title><p>Часть</p></title><p>Начало.</p><section><title><p>Глава</p></title>"
        "<p>Текст <emphasis>важен</emphasis>!</p></section><p>Конец.</p></section></body>"
        '<body name="notes"><section><title><p>1</p></title><p>Сноска.</p></section></body></FictionBook>'
    )
    parsed = parse_book(path)
    assert parsed.author == "Иван Иванов"
    assert [s.title for s in parsed.sections] == ["Часть", "Глава", "Часть", "1"]
    assert parsed.sections[1].text == "Текст важен!"
    assert parsed.sections[-1].text == "Сноска."


def test_fb2_rejects_external_entities(tmp_path: Path) -> None:
    path = tmp_path / "evil.fb2"
    path.write_text('<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><FictionBook>&secret;</FictionBook>')
    with pytest.raises(DefusedXmlException):
        parse_book(path)


def test_scanned_pdf_has_actionable_error(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(path)
    with pytest.raises(ValueError, match="OCR"):
        parse_book(path)


def test_unsupported_format(tmp_path: Path) -> None:
    path = tmp_path / "book.exe"
    path.touch()
    with pytest.raises(ValueError, match="Unsupported"):
        parse_book(path)


def test_demo_manifest_resolves_paths_and_all_books_parse() -> None:
    manifest = Path(__file__).resolve().parents[1] / "corpus/demo/manifest.json"
    entries = load_manifest(manifest)
    assert len(entries) >= 6
    assert {e["language"] for e in entries} >= {"en", "fr", "de", "ru"}
    for entry in entries:
        path = download_source(entry, Path("unused"))
        assert path.is_absolute()
        assert parse_book(path).sections


def test_manifest_rejects_duplicates(tmp_path: Path) -> None:
    demo = Path(__file__).resolve().parents[1] / "corpus/demo/manifest.json"
    entry = json.loads(demo.read_text())[0]
    manifest = tmp_path / "bad.json"
    manifest.write_text(json.dumps([entry, entry]))
    with pytest.raises(ValueError, match="Duplicate"):
        load_manifest(manifest)


def test_download_is_atomic_and_reuses_completed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_client = httpx.Client
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"Complete book.")

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    entry = {"id": "sample", "format": "txt", "download_url": "https://example.com/book.txt"}
    (tmp_path / "sample.txt.part").write_text("incomplete")
    result = download_source(entry, tmp_path)
    assert result.read_text() == "Complete book."
    assert not (tmp_path / "sample.txt.part").exists()
    assert download_source(entry, tmp_path) == result
    assert len(calls) == 1


def test_download_rejects_insecure_redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://example.com/insecure"})

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    entry = {"id": "sample", "format": "txt", "download_url": "https://example.com/book"}
    with pytest.raises(ValueError, match="insecure"):
        download_source(entry, tmp_path)
    assert not (tmp_path / "sample.txt").exists()


def test_html_preserves_explicit_poetry_and_preformatted_lines(tmp_path: Path) -> None:
    path = tmp_path / "verse.html"
    path.write_text(
        "<h1>Poetry</h1><p>First <em>line</em>.<br>Second line.</p>"
        '<div class="poem"><div class="stanza"><span>Third line.</span><br>'
        "<span>Fourth line.</span></div></div><pre>Fifth line.\nSixth line.</pre>"
    )
    text = parse_book(path).sections[0].text
    assert text == "First line.\nSecond line.\n\nThird line.\nFourth line.\n\nFifth line.\nSixth line."


def test_fb2_preserves_stanzas_and_verse_lines(tmp_path: Path) -> None:
    path = tmp_path / "verse.fb2"
    path.write_text(
        "<FictionBook><body><section><title><p>Poetry</p></title><poem>"
        "<stanza><v>First line.</v><v>Second <emphasis>line</emphasis>.</v></stanza>"
        "<stanza><v>Third line.</v></stanza></poem></section></body></FictionBook>"
    )
    assert parse_book(path).sections[0].text == "First line.\nSecond line.\n\nThird line."


def test_explicit_fb2_code_block_preserves_source_whitespace(tmp_path: Path) -> None:
    from xml.etree.ElementTree import Element, SubElement, tostring

    code = '```python\r\n\tif True:\r\n    message = "a  b"\r\n    print(message)\r\n```'
    root = Element("FictionBook")
    section = SubElement(SubElement(root, "body"), "section")
    paragraph = SubElement(section, "p")
    SubElement(paragraph, "code", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}).text = code
    path = tmp_path / "code.fb2"
    path.write_bytes(tostring(root, encoding="utf-8"))
    assert parse_book(path).sections[0].text == code.replace("\r\n", "\n")


def test_html_pre_preserves_indentation_tabs_and_string_spaces(tmp_path: Path) -> None:
    from html import escape

    code = '\tif True:\n    message = "a  b"\n    print(message)\n'
    path = tmp_path / "code.html"
    path.write_text("<html><body><pre><code>" + escape(code) + "</code></pre></body></html>")
    assert parse_book(path).sections[0].text == code


def test_table_poetry_survives_whitespace_only_pre_without_fallback_loss(tmp_path: Path) -> None:
    path = tmp_path / "table-poetry.html"
    path.write_text(
        "<h1>The Infant's Delight</h1><pre> \n\t&#160; </pre>"
        "<table><tr><td>When the win-ter winds are blow-ing,<br>"
        " And we ga-ther glad and gay,<br>Where the fire its light is throw-ing,"
        "</td></tr></table><h2>The Dead Robin</h2><table><tr><td>Another verse.</td></tr></table>"
    )
    parsed = parse_book(path)
    assert [section.title for section in parsed.sections] == ["The Infant's Delight", "The Dead Robin"]
    assert parsed.sections[0].text == (
        "When the win-ter winds are blow-ing,\nAnd we ga-ther glad and gay,\nWhere the fire its light is throw-ing,"
    )
    assert parsed.sections[1].text == "Another verse."
    assert all(section.text.strip() for section in parsed.sections)


def test_whitespace_only_pre_does_not_disable_generic_text_fallback(tmp_path: Path) -> None:
    path = tmp_path / "blank-pre.html"
    path.write_text("<pre>\n\t&#160;</pre><div>Actual previously hidden text.</div>")
    assert parse_book(path).sections[0].text == "Actual previously hidden text."


def test_scientific_table_preserves_caption_headers_rows_and_column_association(tmp_path: Path) -> None:
    path = tmp_path / "science-table.html"
    path.write_text(
        "<h1>Measurements</h1><table><caption>Water samples</caption>"
        "<thead><tr><th>Temperature (°C)</th><th>Density (kg/m³)</th></tr></thead>"
        "<tbody><tr><td><p>4</p></td><td>1000</td></tr>"
        "<tr><td>20</td><td><p>998.2</p></td></tr></tbody></table><p>End of experiment.</p>"
    )
    parsed = parse_book(path)
    assert len(parsed.sections) == 1
    assert parsed.sections[0].title == "Measurements"
    assert parsed.sections[0].text == (
        "Water samples\n\nTemperature (°C)\tDensity (kg/m³)\n\n4\t1000\n\n20\t998.2\n\nEnd of experiment."
    )


def test_nested_layout_tables_do_not_repeat_paragraphs_lists_or_nested_cells(tmp_path: Path) -> None:
    path = tmp_path / "nested-tables.html"
    path.write_text(
        "<h1>Layout</h1><table><tr><td><p>Before nested table.</p>"
        "<table><tr><td><p>Nested <em>cell</em>.</p></td><td>Nested value.</td></tr></table>"
        "<p>After nested table.</p></td><td><ul><li>List item one.</li>"
        "<li><p>List item two.</p></li></ul></td></tr></table>"
    )
    text = parse_book(path).sections[0].text
    for phrase in (
        "Before nested table.",
        "Nested cell.",
        "Nested value.",
        "After nested table.",
        "List item one.",
        "List item two.",
    ):
        assert text.count(phrase) == 1
    assert text.index("Before nested table.") < text.index("Nested cell.") < text.index("After nested table.")
    assert "Nested cell.\tNested value." in text
    assert text.count("List item") == 2


def test_table_in_quotation_retains_sibling_text_and_is_emitted_once(tmp_path: Path) -> None:
    path = tmp_path / "quoted-table.html"
    path.write_text(
        "<h1>Quotation</h1><blockquote>Before quotation table.<table><tr><td><p>Cell text.</p></td>"
        "<td>Cell value.</td></tr></table>After quotation table.</blockquote><p>Following paragraph.</p>"
    )
    text = parse_book(path).sections[0].text
    for phrase in (
        "Before quotation table.",
        "Cell text.",
        "Cell value.",
        "After quotation table.",
        "Following paragraph.",
    ):
        assert text.count(phrase) == 1
    assert text.index("Before quotation table.") < text.index("Cell text.") < text.index("After quotation table.")


def test_table_cell_pre_keeps_code_whitespace_and_string_literals(tmp_path: Path) -> None:
    from html import escape

    code = '\tif True:\n    message = "a  b"\n    print(message)\n'
    path = tmp_path / "table-code.html"
    path.write_text(
        "<h1>Code</h1><table><tr><td><p>Code example</p><pre><code>"
        + escape(code)
        + "</code></pre><p>Explanation.</p></td><td>Python</td></tr></table>"
    )
    text = parse_book(path).sections[0].text
    assert code in text
    assert text.count('message = "a  b"') == 1
    assert text.count("Explanation.") == 1
    assert "\tPython" in text


def test_table_spans_keep_empty_column_positions_without_repeating_text(tmp_path: Path) -> None:
    path = tmp_path / "spans.html"
    path.write_text(
        '<h1>Groups</h1><table><tr><th rowspan="2">Group</th><th colspan="2">Measurements</th></tr>'
        "<tr><th>First</th><th>Second</th></tr><tr><td>A</td><td>1</td><td>2</td></tr></table>"
    )
    text = parse_book(path).sections[0].text
    assert text == "Group\tMeasurements\t\n\n\tFirst\tSecond\n\nA\t1\t2"
    assert text.count("Group") == 1
    assert text.count("Measurements") == 1


def test_consecutive_poem_title_and_epigraph_headings_both_survive_once(tmp_path: Path) -> None:
    path = tmp_path / "poem-epigraph.html"
    path.write_text(
        '<h2>ALL THINGS OBEY GOD.</h2><h2>"He saith to the snow, Be thou on the earth."</h2>'
        "<pre> &#160; </pre><table><tr><td>First source line.<br>Second source line.</td></tr></table>"
    )
    parsed = parse_book(path)
    assert len(parsed.sections) == 1
    section = parsed.sections[0]
    assert section.title == 'ALL THINGS OBEY GOD.\n"He saith to the snow, Be thou on the earth."'
    assert section.text == "First source line.\nSecond source line."
    combined = section.title + "\n" + section.text
    assert combined.count("ALL THINGS OBEY GOD.") == 1
    assert combined.count('"He saith to the snow, Be thou on the earth."') == 1


def test_ordinary_h1_h2_chapters_keep_heading_order_and_unchanged_prose(tmp_path: Path) -> None:
    path = tmp_path / "chapters.html"
    path.write_text(
        "<h1>Book title</h1><h2>Chapter I</h2><p>Original first prose.</p>"
        "<p>Next first paragraph.</p><h2>Chapter II</h2><p>Original second prose.</p>"
    )
    parsed = parse_book(path)
    assert [section.title for section in parsed.sections] == ["Book title\nChapter I", "Chapter II"]
    assert [section.text for section in parsed.sections] == [
        "Original first prose.\n\nNext first paragraph.",
        "Original second prose.",
    ]


def test_audited_styled_div_poem_title_is_retained_before_its_table(tmp_path: Path) -> None:
    path = tmp_path / "styled-poem.html"
    path.write_text(
        '<div class="style1 xhtml_div_align">BLIND MAN\'S BUFF.</div><div><br></div>'
        "<table><tr><td>When the win-ter winds are blow-ing,<br>Another line.</td></tr></table>"
    )
    section = parse_book(path).sections[0]
    assert section.title == "BLIND MAN'S BUFF."
    assert section.text == "When the win-ter winds are blow-ing,\nAnother line."


def test_styled_div_gate_does_not_promote_generic_layout_or_cross_another_heading(tmp_path: Path) -> None:
    path = tmp_path / "layout-gate.html"
    path.write_text(
        '<div class="xhtml_div_align">GENERIC LAYOUT</div>'
        '<div class="style1 xhtml_div_align">NOT A TABLE TITLE</div>'
        "<h2>Actual heading</h2><p>Original prose.</p><table><tr><td>Table data.</td></tr></table>"
    )
    parsed = parse_book(path)
    assert parsed.sections[0].title == "Text"
    assert parsed.sections[0].text == "GENERIC LAYOUT\n\nNOT A TABLE TITLE"
    section = parsed.sections[1]
    assert section.title == "Actual heading"
    assert section.text == "Original prose.\n\nTable data."


def test_trailing_heading_without_body_is_not_silently_lost_or_empty_section(tmp_path: Path) -> None:
    path = tmp_path / "trailing-heading.html"
    path.write_text("<h1>Main</h1><p>Body text.</p><h2>Original ending heading</h2>")
    parsed = parse_book(path)
    assert len(parsed.sections) == 1
    assert parsed.sections[0].text == "Body text.\n\nOriginal ending heading"
