"""Deterministic book extraction with stable sections and conservative normalization.

Canonical section text uses Unicode NFC and LF newlines. Horizontal whitespace is
collapsed, wrapped lines join with a space, and paragraphs are separated by two
LF characters. Quotation offsets refer to this canonical text, not source bytes.
Images, styling and layout are not retained. Footnotes remain readable text.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import unicodedata
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from urllib.parse import unquote
from xml.etree.ElementTree import Element
from zipfile import ZipFile

from bs4 import BeautifulSoup, Tag
from defusedxml import ElementTree as ET
from pypdf import PdfReader

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_MEMBER_BYTES = 20 * 1024 * 1024


def _loaded_parser_fingerprint() -> str:
    """Bind identity once to the source and dependency versions being loaded."""
    source_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    dependencies = {name: version(name) for name in ("beautifulsoup4", "defusedxml", "pypdf")}
    identity = {"source_sha256": source_digest, "dependencies": dependencies}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


# Never recompute this during import_book: a running worker must keep the
# identity of its loaded parser even if another process edits the source file.
PARSER_FINGERPRINT = _loaded_parser_fingerprint()


@dataclass(slots=True)
class Section:
    title: str
    text: str


@dataclass(slots=True)
class ParsedBook:
    title: str
    author: str
    language: str
    sections: list[Section]


def normalize_text(text: str) -> str:
    """Produce deterministic canonical paragraph text without changing wording."""
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    paragraphs = re.split(r"\n\s*\n", text)
    return "\n\n".join(" ".join(p.split()) for p in paragraphs if p.strip())


def _text(node: Tag) -> str:
    return normalize_text(node.get_text(" ", strip=True))


def _verse_container(node: Tag) -> bool:
    classes = node.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    return node.name == "div" and bool(set(classes) & {"poem", "stanza", "verse"})


def _block_text(node: Tag) -> str:
    """Preserve explicit verse/preformatted line breaks, not HTML source wrapping."""
    has_lines = node.name == "pre" or bool(node.find("br")) or _verse_container(node)
    if not has_lines:
        return normalize_text(node.get_text())
    for line_break in node.find_all("br"):
        line_break.replace_with("\n")
    for line in node.find_all(class_=lambda value: value is not None and value in {"verse", "verse-line", "line"}):
        line.append("\n")
    text = unicodedata.normalize("NFC", node.get_text().replace("\r\n", "\n").replace("\r", "\n"))
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _html(data: bytes | str, fallback: str) -> ParsedBook:
    soup = BeautifulSoup(data, "html.parser")
    title = _text(soup.title) if soup.title else fallback
    author_tag = soup.find("meta", attrs={"name": re.compile(r"^(author|dc.creator)$", re.I)})
    author = str(author_tag.get("content", "")) if isinstance(author_tag, Tag) else ""
    language = str(soup.html.get("lang", "und")) if soup.html else "und"
    for unwanted in soup.select("script,style,nav,header,footer,#pg-header,#pg-footer,.pg-boilerplate"):
        unwanted.decompose()
    sections: list[Section] = []
    heading, paragraphs = "Text", []
    root = soup.body or soup
    for node in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "blockquote", "dt", "dd", "div"]):
        if node.name == "div" and not _verse_container(node):
            continue
        if _verse_container(node) and node.find(class_=lambda value: value == "stanza"):
            continue
        if any(
            _verse_container(parent) and not parent.find(class_=lambda value: value == "stanza")
            for parent in node.parents
            if isinstance(parent, Tag)
        ):
            continue
        if node.name in {"p", "li", "blockquote", "dd", "pre"} and node.find(["p", "li", "pre", "blockquote"]):
            continue
        # Do not duplicate a paragraph nested in a list/quotation that is emitted itself.
        value = _text(node) if node.name.startswith("h") else _block_text(node)
        if not value:
            continue
        if node.name.startswith("h"):
            if paragraphs:
                sections.append(Section(heading, "\n\n".join(paragraphs)))
                paragraphs = []
            heading = value
        else:
            paragraphs.append(value)
    if paragraphs:
        sections.append(Section(heading, "\n\n".join(paragraphs)))
    if not sections:
        value = normalize_text(root.get_text("\n\n", strip=True))
        if value:
            sections = [Section("Text", value)]
    return ParsedBook(title, author, language, sections)


def _plain(data: bytes, fallback: str) -> ParsedBook:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # A legacy Cyrillic text must be converted explicitly rather than silently
        # decoded as Latin-1. This avoids plausible-looking but corrupt quotations.
        raise ValueError("Text must use UTF-8; convert legacy encodings explicitly") from None
    metadata: dict[str, str] = {}
    for field in ("Title", "Author", "Language"):
        match = re.search(rf"^{field}:\s*(.+)$", text, re.M)
        metadata[field] = match.group(1).strip() if match else ""
    start = re.search(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\n", text, re.I)
    end = re.search(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK", text, re.I)
    text = text[start.end() if start else 0 : end.start() if end else len(text)]
    sections: list[Section] = []
    title, chunks = "Text", []
    heading_re = re.compile(r"^(?:CHAPTER|BOOK|PART|VOLUME|ГЛАВА|ЧАСТЬ|LIVRE|CHAPITRE)\s+.{1,100}$", re.I)
    for para in re.split(r"\n\s*\n", text):
        para = normalize_text(para)
        if not para:
            continue
        if heading_re.fullmatch(para):
            if chunks:
                sections.append(Section(title, "\n\n".join(chunks)))
                chunks = []
            title = para
        else:
            chunks.append(para)
    if chunks:
        sections.append(Section(title, "\n\n".join(chunks)))
    return ParsedBook(metadata["Title"] or fallback, metadata["Author"], metadata["Language"] or "und", sections)


def _safe_member(base: str, relative: str) -> str:
    relative = unquote(relative.split("#", 1)[0])
    if ":" in relative or "\\" in relative or relative.startswith("/"):
        raise ValueError("EPUB contains a non-local or unsafe resource path")
    name = posixpath.normpath(posixpath.join(base, relative))
    if name == ".." or name.startswith("../"):
        raise ValueError("EPUB resource escapes the archive")
    return name


def _epub(path: Path) -> ParsedBook:
    with ZipFile(path) as archive:
        if len(archive.infolist()) > 10000 or sum(i.file_size for i in archive.infolist()) > MAX_FILE_BYTES:
            raise ValueError("EPUB exceeds the extraction size limit")
        if any(i.file_size > MAX_MEMBER_BYTES for i in archive.infolist()):
            raise ValueError("EPUB member exceeds the extraction size limit")
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile")
        if rootfile is None or not rootfile.get("full-path"):
            raise ValueError("EPUB container does not identify a package")
        package_name = _safe_member("", rootfile.attrib["full-path"])
        package = ET.fromstring(archive.read(package_name))
        metadata = package.find("{*}metadata")

        def field(name: str, fallback: str = "") -> str:
            node = metadata.find(f"{{*}}{name}") if metadata is not None else None
            return normalize_text("".join(node.itertext())) if node is not None else fallback

        manifest = {n.attrib["id"]: n for n in package.findall("{*}manifest/{*}item")}
        sections: list[Section] = []
        for ref in package.findall("{*}spine/{*}itemref"):
            item = manifest.get(ref.get("idref", ""))
            if item is None:
                raise ValueError("EPUB spine references a missing manifest item")
            media_type = item.get("media-type", "")
            if media_type not in {"application/xhtml+xml", "text/html"}:
                continue
            name = _safe_member(posixpath.dirname(package_name), item.attrib["href"])
            parsed = _html(archive.read(name), name)
            for section in parsed.sections:
                if section.title == "Text":
                    section.title = parsed.title if parsed.title != name else name
                sections.append(section)
        return ParsedBook(field("title", path.stem), field("creator"), field("language", "und"), sections)


def _fb2(path: Path) -> ParsedBook:
    root = ET.fromstring(path.read_bytes())
    info = root.find(".//{*}title-info")

    def field(name: str) -> str:
        node = info.find(f"{{*}}{name}") if info is not None else None
        return normalize_text(" ".join(node.itertext())) if node is not None else ""

    author_nodes = info.findall("{*}author") if info is not None else []
    authors = [normalize_text(" ".join(n.itertext())) for n in author_nodes]
    sections: list[Section] = []

    def visit(node: Element, inherited: str) -> None:
        # ElementTree elements are intentionally kept local to avoid accepting
        # arbitrary parser objects at the public boundary.
        title_node = node.find("{*}title")
        title = normalize_text(" ".join(title_node.itertext())) if title_node is not None else inherited
        paragraphs: list[str] = []

        def flush() -> None:
            if paragraphs:
                sections.append(Section(title, "\n\n".join(paragraphs)))
                paragraphs.clear()

        for child in node:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "title":
                continue
            if name == "section":
                flush()
                visit(child, title)
            elif name == "poem":
                stanza_texts = []
                for stanza in child.findall("{*}stanza"):
                    lines = [normalize_text("".join(line.itertext())) for line in stanza.findall("{*}v")]
                    stanza_texts.append("\n".join(lines))
                poem_title = child.find("{*}title")
                if poem_title is not None:
                    stanza_texts.insert(0, normalize_text("".join(poem_title.itertext())))
                value = "\n\n".join(stanza_texts).strip()
                if value:
                    paragraphs.append(value)
            elif name != "binary":
                value = normalize_text("".join(child.itertext()))
                if value:
                    paragraphs.append(value)
        flush()

    for body in root.findall("{*}body"):
        visit(body, "Notes" if body.get("name") == "notes" else "Text")
    return ParsedBook(field("book-title") or path.stem, "; ".join(authors), field("lang") or "und", sections)


def parse_book(path: Path) -> ParsedBook:
    """Read TXT, HTML, EPUB, FB2 or text-bearing PDF without extracting files."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Book exceeds the 100 MiB source size limit")
    match path.suffix.lower():
        case ".txt" | ".md":
            parsed = _plain(path.read_bytes(), path.stem)
        case ".html" | ".htm" | ".xhtml":
            parsed = _html(path.read_bytes(), path.stem)
        case ".epub":
            parsed = _epub(path)
        case ".fb2":
            parsed = _fb2(path)
        case ".pdf":
            reader = PdfReader(path)
            if reader.is_encrypted:
                raise ValueError("Encrypted PDFs must be unlocked before import")
            sections = [
                Section(f"Page {i}", normalize_text(page.extract_text() or ""))
                for i, page in enumerate(reader.pages, 1)
            ]
            meta = reader.metadata
            parsed = ParsedBook(
                str(meta.title or path.stem) if meta else path.stem,
                str(meta.author or "") if meta else "",
                "und",
                sections,
            )
        case _:
            raise ValueError(f"Unsupported book format: {path.suffix}")
    parsed.sections = [s for s in parsed.sections if s.text.strip()]
    if not parsed.sections:
        raise ValueError("No extractable text; scanned books require OCR before import")
    return parsed
