"""One pinned Wikibooks reference page, with explicit render provenance.

No crawling or implicit book-completeness promotion. Source text and API HTML
are separate artifacts: historical page text may use current template renders.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
import time
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from book_rag.parsers import parse_book
from book_rag.sources import DownloadDeferred, _retry_delay

API = "https://ru.wikibooks.org/w/api.php"
TITLE = "Регулярные выражения"
PAGE_ID = 5267
REVISION_ID = 266423
WIKITEXT_SHA256 = "0a8ef5106a6ee30553406e864c4269000bb561a1df805913aeb001160ef5413a"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
REQUEST_INTERVAL = 3.0
USER_AGENT = "BookRAG/0.1 (https://github.com/rebrudiy/book_rag; one pinned licensed reference page)"
SOURCE_URL = "https://ru.wikibooks.org/wiki/Регулярные_выражения"
REVISION_URL = f"https://ru.wikibooks.org/w/index.php?oldid={REVISION_ID}"
HISTORY_URL = SOURCE_URL + "?action=history"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
TERMS_URL = "https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use"
POLICY_URL = "https://ru.wikibooks.org/wiki/Викиучебник:Авторские_права"
PACKAGING_TITLE = "Источник, лицензия и область покрытия (Book RAG)"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.is_symlink() or path.is_symlink():
        raise ValueError("Wikibooks cache paths must not be symlinks")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _json(path: Path, data: Any) -> None:
    _atomic(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


def _api_url(params: dict[str, str]) -> str:
    return str(httpx.URL(API, params={"format": "json", "formatversion": "2", "maxlag": "5", **params}))


class _Fetch:
    """Serial, bounded cache; any provider denial persists a host cooldown."""

    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.state_path = destination / "provider-state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if not isinstance(self.state, dict):
            raise ValueError("Damaged Wikibooks provider state")
        until = self.state.get("retry_at_epoch", 0)
        next_epoch = self.state.get("next_request_epoch", 0)
        if (
            not isinstance(until, (int, float))
            or not math.isfinite(until)
            or not isinstance(next_epoch, (int, float))
            or not math.isfinite(next_epoch)
            or until
            and self.state.get("host") not in {"ru.wikibooks.org", "foundation.wikimedia.org", "creativecommons.org"}
        ):
            raise ValueError("Damaged Wikibooks provider cooldown")
        self.next_request = 0.0
        self.network_requests = 0
        self.cache_hits = 0
        self.client = httpx.Client(
            timeout=httpx.Timeout(45, connect=10),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
        )

    def close(self) -> None:
        self.client.close()

    def _defer(self, response: httpx.Response, reason: str, seconds: float) -> None:
        until = time.time() + max(60.0, seconds)
        _json(
            self.state_path,
            {"host": response.url.host, "reason": reason, "retry_at_epoch": until, "source_url": str(response.url)},
        )
        self.state = json.loads(self.state_path.read_text())
        raise DownloadDeferred(response, max(60.0, seconds))

    def fetch(self, url: str, label: str, *, api: bool = True) -> tuple[bytes, dict[str, Any]]:
        url = str(httpx.URL(url))
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc not in {
            "ru.wikibooks.org",
            "foundation.wikimedia.org",
            "creativecommons.org",
        }:
            raise ValueError("Only explicitly approved HTTPS Wikimedia/license sources are allowed")
        if api and (parsed.netloc != "ru.wikibooks.org" or parsed.path != "/w/api.php"):
            raise ValueError("Unexpected Wikibooks API route")
        request = {"method": "GET", "url": url, "user_agent": USER_AGENT, "accept_encoding": "gzip"}
        request_sha = _sha(json.dumps(request, sort_keys=True).encode())
        target = self.destination / "responses" / (request_sha + (".json" if api else ".html"))
        sidecar = target.with_suffix(target.suffix + ".source.json")
        if target.is_symlink() or sidecar.is_symlink():
            raise ValueError("Wikibooks cache paths must not be symlinks")
        if target.is_file() and sidecar.is_file():
            info = json.loads(sidecar.read_text())
            if (
                isinstance(info, dict)
                and info.get("request") == request
                and info.get("bytes") == target.stat().st_size
                and target.stat().st_size <= MAX_RESPONSE_BYTES
            ):
                data = target.read_bytes()
                if len(data) <= MAX_RESPONSE_BYTES and _sha(data) == info.get("response_sha256"):
                    self.cache_hits += 1
                    return data, info
        if self.state.get("retry_at_epoch", 0) > time.time() and self.state.get("host") == parsed.netloc:
            raise ValueError("Persisted provider cooldown; do not send another request to this host yet")
        delay = max(self.next_request - time.monotonic(), self.state.get("next_request_epoch", 0) - time.time())
        if delay > 0:
            time.sleep(delay)
        try:
            with self.client.stream("GET", url) as response:
                self.network_requests += 1
                if response.status_code == 403:
                    self._defer(response, "HTTP 403 access denied; review source access", 900)
                if response.status_code == 429 or response.status_code >= 500:
                    self._defer(response, "HTTP provider throttle/error", _retry_delay(response, 0))
                response.raise_for_status()
                body = bytearray()
                for block in response.iter_bytes(chunk_size=64 * 1024):
                    if len(body) + len(block) > MAX_RESPONSE_BYTES:
                        raise ValueError("Wikibooks response exceeds the 5 MiB decoded limit")
                    body.extend(block)
                data = bytes(body)
                if not data:
                    raise ValueError("Empty Wikibooks response")
                if api:
                    payload = json.loads(data)
                    if not isinstance(payload, dict):
                        raise ValueError("Expected an API JSON object")
                    if "error" in payload:
                        code = str(payload["error"].get("code", "unknown"))
                        if code in {"maxlag", "ratelimited", "readonly"}:
                            self._defer(response, "API " + code, 60)
                        raise ValueError("Wikibooks API error: " + code)
                info = {
                    "schema_version": 1,
                    "label": label,
                    "request": request,
                    "request_sha256": request_sha,
                    "response_sha256": _sha(data),
                    "bytes": len(data),
                    "status_code": response.status_code,
                    "final_url": str(response.url),
                    "response_content_type": response.headers.get("content-type"),
                    "response_etag": response.headers.get("etag"),
                    "response_date": response.headers.get("date"),
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "local_path": str(target.relative_to(self.destination)),
                    "response_representation": "decoded HTTP entity bytes; transfer compression is not retained",
                }
                _atomic(target, data)
                _json(sidecar, info)
                return data, info
        finally:
            self.next_request = time.monotonic() + REQUEST_INTERVAL
            self.state["next_request_epoch"] = time.time() + REQUEST_INTERVAL
            _json(self.state_path, self.state)
            self.client.cookies.clear()


def _render_request() -> str:
    return _api_url(
        {
            "action": "parse",
            "oldid": str(REVISION_ID),
            "prop": "text|wikitext|sections|revid|templates|properties|parsewarnings|headhtml",
            "disableeditsection": "1",
        }
    )


def _talk_request() -> str:
    return _api_url(
        {
            "action": "query",
            "meta": "siteinfo",
            "siprop": "rightsinfo",
            "prop": "revisions|info",
            "titles": "Обсуждение:" + TITLE,
            "rvprop": "ids|timestamp|content",
            "rvslots": "main",
            "rvlimit": "1",
            "inprop": "url",
        }
    )


def _history_request() -> str:
    return _api_url(
        {
            "action": "query",
            "prop": "revisions",
            "titles": TITLE,
            "rvprop": "ids|timestamp|comment|user",
            "rvdir": "newer",
            "rvlimit": "50",
        }
    )


def _validated_parse(payload: dict[str, Any]) -> dict[str, Any]:
    parsed = payload.get("parse")
    if not isinstance(parsed, dict) or (parsed.get("pageid"), parsed.get("revid"), parsed.get("title")) != (
        PAGE_ID,
        REVISION_ID,
        TITLE,
    ):
        raise ValueError("Rendered page identity does not match the pinned Wikibooks revision")
    raw = parsed.get("wikitext")
    if not isinstance(raw, str) or _sha(raw.encode()) != WIKITEXT_SHA256:
        raise ValueError("API wikitext differs from the SHA-verified offline revision")
    if not isinstance(parsed.get("text"), str) or not parsed["text"].strip():
        raise ValueError("Pinned parse response contains no HTML")
    if parsed.get("parsewarnings"):
        raise ValueError("Pinned source has parser warnings that require review")
    return parsed


def _rights_review(raw: str, talk: dict[str, Any], history: dict[str, Any]) -> dict[str, Any]:
    rights = talk.get("query", {}).get("rightsinfo", {})
    if rights.get("url") not in {LICENSE_URL, LICENSE_URL + "deed.ru"}:
        raise ValueError("Publisher rights metadata does not explicitly identify CC BY-SA 4.0")
    pages = talk.get("query", {}).get("pages", [])
    talk_text = "\n".join(
        revision.get("slots", {}).get("main", {}).get("content", "")
        for page in pages
        for revision in page.get("revisions", [])
    )
    prohibited = r"copyright|all rights reserved|non.?commercial|fair.?use|нарушен\w* авторск|несвободн\w* текст"
    if re.search(prohibited, raw + "\n" + talk_text, re.I):
        raise ValueError("Explicit source rights restriction requires manual review")
    main_pages = history.get("query", {}).get("pages", [])
    if len(main_pages) != 1 or main_pages[0].get("pageid") != PAGE_ID:
        raise ValueError("History response is not bound to the pinned source page")
    revisions = main_pages[0].get("revisions", [])
    first = revisions[0] if revisions else {}
    if (
        first.get("revid") != 25968
        or first.get("parentid") != 0
        or "[[w:Регулярные выражения]]" not in first.get("comment", "")
    ):
        raise ValueError("Expected upstream Wikipedia import evidence is missing")
    return {
        "status": "no_explicit_restriction_observed_in_bounded_review",
        "license": "CC-BY-SA-4.0",
        "license_url": LICENSE_URL,
        "publisher_rightsinfo": rights,
        "scope": "pinned page body, current talk revision and oldest 50 history records; not a full history audit",
        "attribution": [
            {"author": "Участники русского Викиучебника", "source_url": SOURCE_URL, "history_url": HISTORY_URL},
            {
                "author": "Участники русской Википедии",
                "source_url": "https://ru.wikipedia.org/wiki/Регулярные_выражения",
                "history_url": "https://ru.wikipedia.org/w/index.php?title=Регулярные_выражения&action=history",
                "evidence_revision_url": "https://ru.wikibooks.org/w/index.php?oldid=25968",
                "evidence_comment": first["comment"],
            },
        ],
        "uncertain_upstream_notes": [
            {
                "revision_id": r["revid"],
                "comment": r.get("comment", ""),
                "interpretation": "exact upstream not identified",
            }
            for r in revisions
            if "англ версии" in r.get("comment", "")
        ],
        "bibliography": "Retained as source references; listing a book is not treated as a license to copy that book",
        "images": "not acquired or redistributed; original captions retained",
    }


def _project(parsed: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project this bounded rendered page, preserving every non-decoration text node once."""
    soup = BeautifulSoup(parsed["text"], "html.parser")
    core = soup.select_one(".mw-parser-output")
    if core is None:
        raise ValueError("Expected one MediaWiki content root")
    omitted = Counter()
    for node in list(core.select("script, style, link, meta, img, .mw-editsection")):
        omitted[node.name] += 1
        node.decompose()
    if core.find("math") is not None or core.select_one(".mwe-math-element") is not None:
        raise ValueError("Math rendering is not supported by this bounded projection; do not silently drop formulas")
    source_codes = list(
        re.finditer(
            r"<(?:source|syntaxhighlight)\b[^>]*>(.*?)</(?:source|syntaxhighlight)\s*>", parsed["wikitext"], re.I | re.S
        )
    )
    pres = core.find_all("pre")
    if len(source_codes) != len(pres):
        raise ValueError("Source code blocks and rendered pre elements do not correspond one to one")
    corrections = []
    for index, (source, rendered) in enumerate(zip(source_codes, pres, strict=True)):
        raw = source.group(1)
        previous = rendered.get_text()
        corrections.append(
            {
                "ordinal": index,
                "source_start": source.start(1),
                "source_end": source.end(1),
                "raw_sha256": _sha(raw.encode()),
                "rendered_text_sha256": _sha(previous.encode()),
                "render_changes_beyond_edge_lf": raw.strip("\n") != previous.strip("\n"),
                "projection": "restore exact pinned wikitext source block, including edge LF",
            }
        )
        rendered.clear()
        rendered.append(NavigableString(raw))
    without_sources = re.sub(
        r"<(?:source|syntaxhighlight)\b[^>]*>.*?</(?:source|syntaxhighlight)\s*>",
        "",
        parsed["wikitext"],
        flags=re.I | re.S,
    )
    source_inline = re.findall(r"<code\b[^>]*>(.*?)</code\s*>", without_sources, re.I | re.S)
    inline = core.find_all("code")
    if len(source_inline) != len(inline):
        raise ValueError("Inline code elements do not correspond to the pinned source")
    inline_audit = []

    def literal(node: Any) -> str:
        if isinstance(node, Comment):
            return ""
        if isinstance(node, NavigableString):
            return str(node)
        if isinstance(node, Tag):
            return "\n" if node.name == "br" else "".join(literal(c) for c in node.children)
        return ""

    for index, (raw, rendered) in enumerate(zip(source_inline, inline, strict=True)):
        if "{{" in raw:
            inline_audit.append(
                {
                    "ordinal": index,
                    "basis": "cached official template expansion",
                    "source_sha256": _sha(raw.encode()),
                    "projected_text": literal(rendered),
                }
            )
            continue
        expected = html.unescape(re.sub(r"</?nowiki\s*>", "", raw, flags=re.I))
        if rendered.get_text() != expected:
            raise ValueError(f"Inline code {index} differs from its literal source; requires a reviewed projection")
        inline_audit.append(
            {
                "ordinal": index,
                "basis": "literal source verified",
                "text_sha256": _sha(expected.encode()),
                "projected_text": expected,
            }
        )
    consumed: Counter[int] = Counter()

    def text(node: Any) -> str:
        if isinstance(node, Comment):
            return ""
        if isinstance(node, NavigableString):
            consumed[id(node)] += 1
            return str(node)
        if isinstance(node, Tag):
            if node.name == "br":
                return "\n"
            return "".join(text(child) for child in node.children)
        return ""

    blocks: list[dict[str, Any]] = []
    table_audit = []

    def emit(kind: str, value: str, **extra: Any) -> None:
        if value.strip():
            blocks.append({"kind": kind, "text": value, "text_sha256": _sha(value.encode()), **extra})

    def walk(node: Any) -> None:
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            emit("text", text(node))
            return
        if not isinstance(node, Tag):
            return
        if re.fullmatch(r"h[1-6]", node.name):
            emit("heading", text(node), level=int(node.name[1]), anchor=node.get("id"))
        elif node.name == "pre":
            emit("source_code", text(node))
        elif node.name == "table":
            if node.find("table") is not None:
                raise ValueError("Nested tables require a separate faithful projection")
            rows = []
            cells_audit = []
            if caption := node.find("caption", recursive=False):
                emit("table_caption", text(caption))
            for row_index, row in enumerate(node.find_all("tr")):
                values = []
                for cell_index, cell in enumerate(row.find_all(["td", "th"], recursive=False)):
                    if cell.get("rowspan", "1") != "1":
                        raise ValueError("Rowspan requires a separate faithful table projection")
                    span = str(cell.get("colspan", "1"))
                    if not span.isdigit() or not 1 <= int(span) <= 32:
                        raise ValueError("Unsupported table colspan")
                    value = text(cell)
                    values.extend([value] + [""] * (int(span) - 1))
                    cells_audit.append(
                        {
                            "row": row_index,
                            "cell": cell_index,
                            "header": cell.name == "th",
                            "colspan": int(span),
                            "text_sha256": _sha(value.encode()),
                        }
                    )
                if values:
                    rows.append("\t".join(values))
            value = "\n".join(rows)
            ordinal = len(table_audit)
            table_audit.append(
                {"ordinal": ordinal, "rows": len(rows), "cells": cells_audit, "text_sha256": _sha(value.encode())}
            )
            emit("table", value, table_ordinal=ordinal)
        elif not node.find(
            [
                "p",
                "li",
                "ul",
                "ol",
                "dl",
                "dt",
                "dd",
                "div",
                "table",
                "pre",
                "figure",
                "figcaption",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
            ]
        ):
            emit(node.name, text(node))
        else:
            pending: list[str] = []

            def flush() -> None:
                emit(node.name, "".join(pending))
                pending.clear()

            for child in node.children:
                if isinstance(child, Tag) and child.name in {
                    "p",
                    "li",
                    "ul",
                    "ol",
                    "dl",
                    "dt",
                    "dd",
                    "div",
                    "table",
                    "pre",
                    "figure",
                    "figcaption",
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    "h5",
                    "h6",
                }:
                    flush()
                    walk(child)
                else:
                    pending.append(text(child))
            flush()

    walk(core)
    expected = {id(n) for n in core.find_all(string=True) if not isinstance(n, Comment) and str(n).strip()}
    if any(consumed[n] != 1 for n in expected):
        raise ValueError("Projection did not consume every main-content text node exactly once")
    headings = [b for b in blocks if b["kind"] == "heading"]
    if len(headings) != len(parsed.get("sections", [])):
        raise ValueError("Projected headings do not match the API section inventory")
    return blocks, {
        "source_blocks": corrections,
        "inline_code": inline_audit,
        "tables": table_audit,
        "headings": [{k: b[k] for k in ("text", "level", "anchor")} for b in headings],
        "main_content_text_nodes": len(expected),
        "main_content_text_nodes_consumed_exactly_once": True,
        "omitted_decoration": dict(omitted),
        "math_elements": 0,
        "references": len(core.select(".reference, .references")),
        "block_count": len(blocks),
        "table_representation": "LF separates rows; TAB separates cells; colspan adds empty cells; cell LF retained",
        "text_basis": "cached official HTML with raw source-block restoration; not reproduction from wikitext alone",
    }


def _fb2(blocks: list[dict[str, Any]], attribution: str) -> bytes:
    ns = "http://www.gribuser.ru/xml/fictionbook/2.0"
    root = Element("FictionBook", {"xmlns": ns})
    description = SubElement(root, "description")
    info = SubElement(description, "title-info")
    SubElement(info, "genre").text = "sci_tech"
    SubElement(SubElement(info, "author"), "nickname").text = "Участники русского Викиучебника и русской Википедии"
    SubElement(info, "book-title").text = TITLE
    SubElement(info, "lang").text = "ru"
    body = SubElement(root, "body")

    def section(title: str) -> Element:
        section = SubElement(body, "section")
        SubElement(SubElement(section, "title"), "p").text = title
        return section

    def paragraph(target: Element, value: str) -> None:
        if any(ord(c) < 32 and c not in "\t\r\n" for c in value):
            raise ValueError("Source control character cannot be represented in FB2")
        SubElement(
            SubElement(target, "p"), "code", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}
        ).text = value

    paragraph(section(PACKAGING_TITLE), attribution)
    current = section(TITLE)
    pending_headings = []
    has_body = False
    for block in blocks:
        if block["kind"] == "heading":
            if has_body:
                pending_headings.clear()
            pending_headings.append(block["text"])
            has_body = False
            continue
        if pending_headings:
            current = section(" / ".join(pending_headings))
            pending_headings.clear()
        paragraph(current, block["text"])
        has_body = True
    if pending_headings:
        # Preserve a source heading with no body as source text in its own
        # section; an empty FB2 section would disappear from the reader parser.
        current = section(" / ".join(pending_headings))
        paragraph(current, "\n".join(pending_headings))
    return tostring(root, encoding="utf-8", xml_declaration=True)


def acquire_wikibooks_regex(destination: Path) -> dict[str, Any]:
    """Prepare one pinned educational reference manifest; no library/DB writes.

    Three bounded API responses and three license/terms pages are cached. The
    manifest is published only after body/code/table/heading preservation checks
    through the existing reader parser. Overall book completion stays unverified.
    """
    destination.mkdir(parents=True, exist_ok=True)
    fetcher = _Fetch(destination)
    try:
        response, render_info = fetcher.fetch(_render_request(), "pinned-rendered-page")
        parsed = _validated_parse(json.loads(response))
        talk, talk_info = fetcher.fetch(_talk_request(), "talk-and-publisher-rights")
        history, history_info = fetcher.fetch(_history_request(), "oldest-50-revision-credits")
        rights = _rights_review(parsed["wikitext"], json.loads(talk), json.loads(history))
        responses = [render_info, talk_info, history_info]
        for label, url in (
            ("publisher-license-policy", POLICY_URL),
            ("wikimedia-terms", TERMS_URL),
            ("cc-by-sa-4-legalcode", "https://creativecommons.org/licenses/by-sa/4.0/legalcode.en"),
        ):
            license_body, info = fetcher.fetch(url, label, api=False)
            visible = BeautifulSoup(license_body, "html.parser").get_text(" ", strip=True).casefold()
            required = {
                "publisher-license-policy": ("авторские права", "cc by-sa", "4.0"),
                "wikimedia-terms": ("licensing of content", "re-use", "attribution"),
                "cc-by-sa-4-legalcode": ("attribution-sharealike 4.0 international", "section 3"),
            }[label]
            if not all(value in visible for value in required):
                raise ValueError("License/terms response does not contain the expected policy: " + label)
            responses.append(info)
        blocks, audit = _project(parsed)
        _atomic(destination / "source-revision-266423.wikitext", parsed["wikitext"].encode())
        _atomic(destination / "rendered-revision-266423.html", parsed["text"].encode())
        attribution = (
            "Регулярные выражения. Участники русского Викиучебника и русской Википедии.\n"
            f"Источник и история участников: {SOURCE_URL}; {HISTORY_URL}.\n"
            "Исходное заимствование: https://ru.wikipedia.org/wiki/Регулярные_выражения; "
            "https://ru.wikipedia.org/w/index.php?title=Регулярные_выражения&action=history; "
            "свидетельство: https://ru.wikibooks.org/w/index.php?oldid=25968.\n"
            f"Закреплённая ревизия: {REVISION_URL}; wikitext SHA-256: {WIKITEXT_SHA256}.\n"
            f"CC BY-SA 4.0: {LICENSE_URL}; условия: {TERMS_URL}; {POLICY_URL}.\n"
            "Область покрытия: полный текст одной закреплённой страницы без изображений; "
            "завершённость книги не подтверждена. Материал — современный коллективный учебный справочник.\n"
            f"HTML получен {render_info['retrieved_at']}; SHA-256 ответа API: {render_info['response_sha256']}. "
            "Старый текст страницы может использовать текущие версии шаблонов при рендеринге.\n"
            "Book RAG преобразовал сохранённый HTML в текстовые блоки FB2; "
            f"{len(audit['source_blocks'])} блоков примеров восстановлены буквально из исходного wikitext. "
            "Строки таблиц разделены LF, ячейки — TAB; код и пробелы сохранены. "
            "Изображения не загружались; подписи сохранены. "
            "Производный текст распространяется под CC BY-SA 4.0. "
            "Лицензии, API-ответы, исходная разметка и карта преобразования сохранены в пакете происхождения."
        )
        data = _fb2(blocks, attribution)
        artifact_sha = _sha(data)
        book = destination / f"wikibooks-regex-266423-{artifact_sha[:16]}.fb2"
        _atomic(book, data)
        reader = parse_book(book)
        canonical_body = "\n\n".join(section.text for section in reader.sections)
        canonical_titles = "\n".join(section.title for section in reader.sections)

        def canonical(value: str) -> str:
            return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))

        if any(canonical(b["text"]) not in canonical_body for b in blocks if b["kind"] != "heading"):
            raise ValueError("Reader parse lost or changed a projected main-content block")
        if any(canonical(b["text"]).strip() not in canonical_titles for b in blocks if b["kind"] == "heading"):
            raise ValueError("Reader parse lost a source heading")
        if any(canonical(item["projected_text"]) not in canonical_body for item in audit["inline_code"]):
            raise ValueError("Reader parse changed an inline regular expression/code literal")
        audit["reader_validation"] = {
            "all_main_content_blocks_retained": True,
            "all_source_headings_retained": True,
            "all_inline_code_values_retained": True,
            "reader_sections": len(reader.sections),
            "reader_characters": sum(len(s.text) for s in reader.sections),
            "artifact_sha256": artifact_sha,
        }
        bundle = _sha(
            json.dumps({i["request"]["url"]: i["response_sha256"] for i in responses}, sort_keys=True).encode()
        )
        packaged_at = datetime.now(UTC).isoformat()
        provenance = {
            "schema_version": 1,
            "page_id": PAGE_ID,
            "revision_id": REVISION_ID,
            "source_wikitext_sha256": WIKITEXT_SHA256,
            "source_revision_url": REVISION_URL,
            "rendered_html_sha256": _sha(parsed["text"].encode()),
            "render_provenance": "exact oldid text rendered by official API; templates may reflect current versions",
            "source_bundle_sha256": bundle,
            "retrieved_at": render_info["retrieved_at"],
            "packaged_at": packaged_at,
            "artifact_sha256": artifact_sha,
            "responses": responses,
            "rights_review": rights,
            "projection_audit": audit,
            "coverage": "partial",
            "source_page_coverage": "complete_pinned_page_text_without_images",
            "book_completion": "unverified",
            "library_modified": False,
        }
        _json(destination / "provenance.json", provenance)
        entry = {
            "id": "ruwikibooks-regex-266423",
            "title": TITLE,
            "author": "Участники русского Викиучебника и русской Википедии",
            "language": "ru",
            "work_id": "ruwikibooks-page-5267",
            "work_identity_status": "provisional",
            "work_type": "book_or_text",
            "content_type": "modern_collaborative_educational_reference",
            "edition": (
                f"Русский Викиучебник, ревизия {REVISION_ID}; HTML {render_info['retrieved_at']}; FB2 projection v1"
            ),
            "source_url": REVISION_URL,
            "source_revision": str(REVISION_ID),
            "format": "fb2",
            "local_path": book.name,
            "rights": "CC BY-SA 4.0; Wikibooks and upstream Russian Wikipedia contributors credited; images excluded",
            "rights_url": LICENSE_URL,
            "artifact_sha256": artifact_sha,
            "source_artifact_sha256": artifact_sha,
            "source_wikitext_sha256": WIKITEXT_SHA256,
            "source_bundle_sha256": bundle,
            "rendered_response_sha256": render_info["response_sha256"],
            "rendered_at": render_info["retrieved_at"],
            "retrieved_at": render_info["retrieved_at"],
            "packaged_at": packaged_at,
            "render_provenance": provenance["render_provenance"],
            "coverage": "partial",
            "coverage_note": "Complete pinned page text without images; overall book completion unverified",
            "coverage_scope": "pinned_page_text_without_images",
            "book_completion": "unverified",
            "source_page_coverage": "complete_pinned_page_text_without_images",
            "illustrations_included": False,
            "attribution": attribution,
            "upstream_attribution": rights["attribution"],
            "uncertain_upstream_notes": rights["uncertain_upstream_notes"],
            "section_attributions": [
                {
                    "title_contains": PACKAGING_TITLE,
                    "author": "Book RAG",
                    "role": "editorial",
                    "source_artifact_sha256": artifact_sha,
                    "text_contains": "Область покрытия: полный текст одной закреплённой страницы без изображений",
                }
            ],
        }
        manifest = destination / "manifest.json"
        _json(manifest, [entry])
        return {
            "manifest": manifest,
            "book": book,
            "source_revision": REVISION_ID,
            "source_bundle_sha256": bundle,
            "artifact_sha256": artifact_sha,
            "network_requests": fetcher.network_requests,
            "cache_hits": fetcher.cache_hits,
            "bytes": sum(i["bytes"] for i in responses),
            "coverage": "partial",
            "reader_validation": audit["reader_validation"],
        }
    finally:
        fetcher.close()
