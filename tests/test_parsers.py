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
