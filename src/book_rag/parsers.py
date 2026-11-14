"""Deterministic book extraction with stable sections and conservative normalization.

Canonical section text uses Unicode NFC and LF newlines. Horizontal whitespace is
collapsed, wrapped lines join with a space, and paragraphs are separated by two
LF characters. Quotation offsets refer to this canonical text, not source bytes.
Images and styling are not retained. Tables retain logical row/cell order with
TAB cell separators and blank-line row separators; complex layout is linearized.
Footnotes remain readable text. Explicit code/preformatted whitespace is preserved.
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

from bs4 import BeautifulSoup, Comment, NavigableString, PageElement, Tag
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


def _verbatim_text(text: str) -> str:
    """Canonicalize Unicode/newlines while preserving code whitespace exactly."""
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def _block_text(node: Tag) -> str:
    """Preserve explicit verse/preformatted line breaks, not HTML source wrapping."""
    if node.name == "pre":
        return _verbatim_text(node.get_text())
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


def _table_cell_text(cell: Tag, depth: int = 0) -> str:
    """Keep cell blocks once, explicit BR lines and preformatted source verbatim."""
    parts: list[str] = []
    flow: list[str | None] = []

    def flush() -> None:
        lines: list[str] = []
        current: list[str] = []
        for token in flow:
            if token is None:
                lines.append(normalize_text("".join(current)))
                current.clear()
            else:
                current.append(token)
        lines.append(normalize_text("".join(current)))
        value = "\n".join(lines).strip()
        if value:
            parts.append(value)
        flow.clear()

    # Iterative traversal keeps deeply nested ordinary layout tags off the
    # Python call stack. Only nested tables recurse, with a bounded fallback.
    stack: list[PageElement | None] = list(reversed(cell.contents))
    blocks = {"p", "li", "div", "blockquote", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
    while stack:
        node = stack.pop()
        if node is None:
            flow.append("\n\n")
        elif isinstance(node, Comment):
            continue
        elif isinstance(node, NavigableString):
            flow.append(str(node))
        elif not isinstance(node, Tag):
            continue
        elif node.name == "br":
            flow.append(None)
        elif node.name == "pre":
            flush()
            value = _verbatim_text(node.get_text())
            if value.strip():
                parts.append(value)
        elif node.name == "table":
            flush()
            value = _table_text(node, depth + 1) if depth < 16 else _block_text(node)
            if value.strip():
                parts.append(value)
        else:
            if node.name in blocks:
                flow.append("\n\n")
                stack.append(None)
            stack.extend(reversed(node.contents))
    flush()
    return "\n\n".join(parts)


def _table_span(cell: Tag, name: str) -> int:
    try:
        return min(100, max(1, int(str(cell.get(name, "1")))))
    except ValueError:
        return 1


def _table_text(table: Tag, depth: int = 0) -> str:
    """Linearize logical rows/cells without invented labels or repeated text.

    Tabs delimit cells, blank lines delimit rows, and explicit cell line breaks
    remain LF. Span placeholders retain basic column position. Complex visual
    layout is not a facsimile or a machine-interpreted scientific data model.
    """
    rows: list[str] = []
    for caption in table.find_all("caption"):
        if caption.find_parent("table") is table:
            value = _table_cell_text(caption, depth)
            if value.strip():
                rows.append(value)
    spans: dict[int, int] = {}
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table:
            continue
        cells: list[str] = []
        next_spans = {column: remaining - 1 for column, remaining in spans.items() if remaining > 1}
        for cell in row.find_all(["td", "th"]):
            if cell.find_parent("tr") is not row or cell.find_parent("table") is not table:
                continue
            while len(cells) in spans:
                cells.append("")
            column = len(cells)
            width = _table_span(cell, "colspan")
            cells.append(_table_cell_text(cell, depth))
            cells.extend([""] * (width - 1))
            height = _table_span(cell, "rowspan")
            if height > 1:
                next_spans.update(dict.fromkeys(range(column, column + width), height - 1))
        spans = next_spans
        value = "\t".join(cells)
        if value.strip():
            rows.append(value)
    return "\n\n".join(rows) if rows else _table_cell_text(table, depth)


def _styled_table_heading(node: Tag) -> bool:
    """Recognize the audited Gutenberg poetry title, not generic layout divs."""
    classes = node.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    if node.name != "div" or not {"style1", "xhtml_div_align"}.issubset(classes):
        return False
    value = _text(node)
    if not value or len(value) > 160 or not value.isupper() or node.find(["table", "p", "pre", "div", "img"]):
        return False
    for following in node.find_all_next(["table", "p", "pre", "h1", "h2", "h3", "h4", "h5", "h6"], limit=8):
        if _text(following):
            return following.name == "table"
    return False


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
    pending_headings: list[str] = []
    root = soup.body or soup
    owned: set[int] = set()
    for node in root.find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "blockquote", "dt", "dd", "div", "table"]
    ):
        if any(id(parent) in owned for parent in node.parents):
            continue
        is_heading = node.name.startswith("h") or _styled_table_heading(node)
        if node.name == "div" and not _verse_container(node) and not is_heading:
            # Inline-only DIVs often contain contributor bylines or signatures.
            # Layout containers with block descendants are visited through those
            # descendants instead, so the same words are never emitted twice.
            if node.find(
                ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "blockquote", "dt", "dd", "div", "table"]
            ):
                continue
        if _verse_container(node) and (node.find(class_=lambda value: value == "stanza") or node.find("table")):
            continue
        if any(
            _verse_container(parent)
            and not parent.find(class_=lambda value: value == "stanza")
            and not parent.find("table")
            for parent in node.parents
            if isinstance(parent, Tag)
        ):
            continue
        owns_table = node.name in {"p", "li", "blockquote", "dd"} and bool(node.find("table"))
        if (
            node.name in {"p", "li", "blockquote", "dd", "pre"}
            and not owns_table
            and node.find(["p", "li", "pre", "blockquote"])
        ):
            continue
        # Do not duplicate a paragraph nested in a list/quotation that is emitted itself.
        if node.name == "table":
            value = _table_text(node)
            owned.add(id(node))
        elif owns_table:
            value = _table_cell_text(node)
            owned.add(id(node))
        else:
            value = _text(node) if is_heading else _block_text(node)
            if node.name == "pre":
                owned.add(id(node))
        if not value.strip():
            continue
        if is_heading:
            if paragraphs:
                sections.append(Section(heading, "\n\n".join(paragraphs)))
                paragraphs = []
                pending_headings.clear()
            pending_headings.append(value)
            heading = "\n".join(pending_headings)
        else:
            paragraphs.append(value)
            pending_headings.clear()
    if paragraphs:
        sections.append(Section(heading, "\n\n".join(paragraphs)))
    elif pending_headings and sections:
        # A final heading-only fragment still belongs to the original text.
        sections[-1].text += "\n\n" + "\n".join(pending_headings)
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
            for ordinal, section in enumerate(parsed.sections):
                if section.title == "Text":
                    # Gutenberg sometimes puts a chapter's final footnote before
                    # the next heading in a document whose HTML title names the
                    # removed license footer. That title is not a book heading.
                    boilerplate_title = re.fullmatch(
                        r"THE FULL PROJECT GUTENBERG[™®]? LICENSE", parsed.title, flags=re.IGNORECASE
                    )
                    # Some EPUB generators split a chapter mid-paragraph and
                    # label the new XHTML document with its next heading. Its
                    # leading fragment still belongs to the preceding chapter.
                    # Require an actual subsequent heading matching that title;
                    # a standalone title-only chapter keeps its own label.
                    next_heading = parsed.sections[1].title if len(parsed.sections) > 1 else None
                    if (
                        ordinal == 0
                        and sections
                        and next_heading is not None
                        and (normalize_text(parsed.title) == normalize_text(next_heading) or boilerplate_title)
                    ):
                        sections[-1].text += "\n\n" + section.text
                        continue
                    if not boilerplate_title:
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
            elif name == "code" and child.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve":
                value = _verbatim_text("".join(child.itertext()))
                if value:
                    paragraphs.append(value)
            elif (
                name == "p"
                and len(child) == 1
                and child[0].tag.rsplit("}", 1)[-1] == "code"
                and (
                    child[0].get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"
                    and not (child.text or "").strip()
                    and not (child[0].tail or "").strip()
                )
            ):
                value = _verbatim_text("".join(child[0].itertext()))
                if value:
                    paragraphs.append(value)
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


def parse_book(path: Path, fallback_title: str | None = None) -> ParsedBook:
    """Read TXT, HTML, EPUB, FB2 or text-bearing PDF without extracting files."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Book exceeds the 100 MiB source size limit")
    original_fallback = fallback_title or path.stem
    match path.suffix.lower():
        case ".txt" | ".md":
            parsed = _plain(path.read_bytes(), original_fallback)
        case ".html" | ".htm" | ".xhtml":
            parsed = _html(path.read_bytes(), original_fallback)
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
    if fallback_title and parsed.title == path.stem:
        parsed.title = fallback_title
    parsed.sections = [s for s in parsed.sections if s.text.strip()]
    if not parsed.sections:
        raise ValueError("No extractable text; scanned books require OCR before import")
    return parsed
