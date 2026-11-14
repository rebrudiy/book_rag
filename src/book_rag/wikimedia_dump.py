"""Pinned, bounded Wikimedia dump acquisition and offline book-graph discovery.

This is a metadata/review catalog, never an import into the search library.
Roots, subpages and navigation links do not establish complete unique books.
"""

from __future__ import annotations

import bz2
import hashlib
import json
import math
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx
from defusedxml import ElementTree as ET

from book_rag.sources import DownloadDeferred, _retry_delay

MAX_COMPRESSED_BYTES = 30 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_PAGE_TEXT_BYTES = 4 * 1024 * 1024
MAX_PAGES = 100_000
MAX_LINKS_PER_PAGE = 10_000
MAX_GRAPH_RECORDS = 50_000
MAX_REVIEW_CANDIDATES = 25
USER_AGENT = "BookRAG/0.1 (https://github.com/rebrudiy/book_rag; pinned offline textbook catalog)"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
LICENSE_POLICY = "https://ru.wikibooks.org/wiki/Викиучебник:Авторские_права"
DUMP_LICENSE_POLICY = "https://dumps.wikimedia.org/legal.html"
CATALOG_VERSION = "wikimedia-metadata-graph-v1"
DEFAULT_CANDIDATES = (
    "Python",
    "Haskell",
    "Бухгалтерский учёт",
    "Основы теоретической физики",
    "Биология клетки",
    "Реализации алгоритмов",
    "Справочник по Java",
    "Линейная алгебра",
    "Термостатика",
    "Физика в конспектах",
    "Регулярные выражения",
    "Английский язык",
)


@dataclass(frozen=True, slots=True)
class DumpSpec:
    wiki: str
    date: str
    filename: str
    bytes: int
    sha256: str
    url: str


WIKIBOOKS_SPEC = DumpSpec(
    "ruwikibooks",
    "2026-11-01",
    "ruwikibooks-2026-11-01-p1p35928.xml.bz2",
    25_354_533,
    "b16b4e932e6b24637f557592229ce40bdfe16c70ef9f4afd794e5ad3d8a052e7",
    "https://dumps.wikimedia.org/other/mediawiki_content_current/ruwikibooks/2026-11-01/xml/bzip2/"
    "ruwikibooks-2026-11-01-p1p35928.xml.bz2",
)


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if temporary.is_symlink():
        raise ValueError("Atomic JSON temporary paths must not be symlinks")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _verified(path: Path, spec: DumpSpec) -> bool:
    return path.is_file() and path.stat().st_size == spec.bytes and _sha(path) == spec.sha256


def _validate_spec(spec: DumpSpec) -> None:
    expected = (
        f"https://dumps.wikimedia.org/other/mediawiki_content_current/ruwikibooks/{spec.date}/xml/bzip2/{spec.filename}"
    )
    if (
        spec.wiki != "ruwikibooks"
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", spec.date)
        or not re.fullmatch(r"ruwikibooks-[\d-]+-p\d+p\d+\.xml\.bz2", spec.filename)
        or not spec.filename.startswith(f"ruwikibooks-{spec.date}-")
        or not 0 < spec.bytes <= MAX_COMPRESSED_BYTES
        or not re.fullmatch(r"[0-9a-f]{64}", spec.sha256)
        or spec.url != expected
    ):
        raise ValueError("Expected an explicit bounded official Russian Wikibooks dump specification")


def acquire_wikibooks_dump(destination: Path) -> dict[str, Any]:
    """Acquire only the explicitly pinned RU Wikibooks current bzip2 snapshot.

    A valid local artifact is reused after exact size/SHA checks. One request,
    no redirects/retry loop, atomic publication; provider deferral is persisted.
    """
    spec = WIKIBOOKS_SPEC
    _validate_spec(spec)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / spec.filename
    if path.is_symlink():
        raise ValueError("Dump cache paths must not be symlinks")
    state_path = destination / "acquisition.json"
    if _verified(path, spec):
        return {"path": path, "bytes": spec.bytes, "sha256": spec.sha256, "cached": True, "network_requests": 0}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("source_url") != spec.url:
            raise ValueError("Damaged or unrelated dump provider state; inspect before requesting again")
        until = state.get("retry_at_epoch", 0)
        if not isinstance(until, (int, float)) or not math.isfinite(until):
            raise ValueError("Damaged dump provider cooldown; inspect state before requesting again")
        if until > time.time():
            raise ValueError(f"Dump provider is deferred until {state.get('retry_at')}")
    temporary = path.with_name(path.name + ".part")
    if temporary.is_symlink() or path.is_symlink():
        raise ValueError("Dump cache paths must not be symlinks")
    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(60, connect=10),
            follow_redirects=False,
        ) as client:
            with client.stream("GET", spec.url) as response:
                if response.status_code in {403, 429, 503}:
                    delay = max(60.0, _retry_delay(response, 0))
                    retry = time.time() + delay
                    try:
                        retry_iso = datetime.fromtimestamp(retry, UTC).isoformat()
                    except (ValueError, OverflowError, OSError):
                        retry_iso = None
                    _write_json(
                        state_path,
                        {"status": "deferred", "source_url": spec.url, "retry_at_epoch": retry, "retry_at": retry_iso},
                    )
                    raise DownloadDeferred(response, delay)
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").casefold() not in {"", "identity"}:
                    raise ValueError("Pinned compressed dump requires an undecoded HTTP identity response")
                digest = hashlib.sha256()
                total = 0
                with temporary.open("wb") as stream:
                    for block in response.iter_bytes():
                        total += len(block)
                        if total > min(spec.bytes, MAX_COMPRESSED_BYTES):
                            raise ValueError("Dump download exceeds its pinned size")
                        digest.update(block)
                        stream.write(block)
                if total != spec.bytes or digest.hexdigest() != spec.sha256:
                    raise ValueError("Dump download does not match the pinned size/SHA-256")
        temporary.replace(path)
        _write_json(
            state_path,
            {
                "status": "downloaded_sha_verified",
                "source_url": spec.url,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "bytes": total,
                "sha256": spec.sha256,
                "license_policy": DUMP_LICENSE_POLICY,
            },
        )
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": path, "bytes": spec.bytes, "sha256": spec.sha256, "cached": False, "network_requests": 1}


class _BoundedBzipReader:
    """Decode one bzip2 stream in small blocks, enforcing the XML byte budget."""

    def __init__(self, stream: Any, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.total = 0
        self.decoder = bz2.BZ2Decompressor()
        self.finished = False

    def read(self, size: int = -1) -> bytes:
        if self.finished or size == 0:
            return b""
        size = min(64 * 1024, size if size > 0 else 64 * 1024)
        while True:
            data = self.stream.read(64 * 1024) if self.decoder.needs_input else b""
            if not data and self.decoder.needs_input:
                raise ValueError("Truncated bzip2 dump")
            output = self.decoder.decompress(data, max_length=min(size, self.limit - self.total + 1))
            self.total += len(output)
            if self.total > self.limit:
                raise ValueError("Dump exceeds the uncompressed XML byte limit")
            if self.decoder.eof:
                if self.decoder.unused_data or self.stream.read(1):
                    raise ValueError("Trailing data or concatenated bzip2 streams are not accepted")
                self.finished = True
            if output or self.finished:
                return output


def _title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).replace("_", " ").split())


def _url(title: str) -> str:
    return "https://ru.wikibooks.org/wiki/" + quote(title.replace(" ", "_"), safe="/")


def _link_target(raw: str, source: str) -> tuple[str | None, str | None, str]:
    raw = unquote(raw.split("|", 1)[0].strip())
    if raw.startswith(":"):
        raw = raw[1:]
    target, _, fragment = raw.partition("#")
    if target.startswith(("/", "../")) and target.endswith("/"):
        target = target[:-1]
    if not target:
        return source, fragment or None, "same_page_fragment"
    if re.match(r"(?:https?:|//)", target, re.I):
        return None, fragment or None, "external"
    if ":" in target:
        prefix = target.split(":", 1)[0].casefold()
        if prefix in {
            "file",
            "image",
            "файл",
            "изображение",
            "category",
            "категория",
            "template",
            "шаблон",
            "модуль",
            "module",
        }:
            return _title(target), fragment or None, "noncontent_namespace"
        # Real main-namespace titles such as 1С:ERP remain resolvable by the
        # catalog. Unknown prefixes are classified after target lookup.
    if target.startswith("/"):
        target = source + target
    elif target.startswith("../"):
        base = source
        while target.startswith("../"):
            base = base.rsplit("/", 1)[0] if "/" in base else ""
            target = target[3:]
        target = base + ("/" if base else "") + target
    return _title(target), fragment or None, "internal_candidate"


def _markup(text: str, source: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Conservative metadata extraction; never render or rewrite wikitext."""
    masked = re.sub(
        r"<!--.*?-->|<(nowiki|pre|source|syntaxhighlight|code)\b[^>]*>.*?</\1\s*>",
        lambda match: " " * len(match.group()),
        text,
        flags=re.I | re.S,
    )
    links = []
    issues = []
    for index, match in enumerate(re.finditer(r"\[\[([^\[\]]+)\]\]", masked)):
        if index >= MAX_LINKS_PER_PAGE:
            issues.append("internal_link_extraction_limit")
            break
        target, fragment, kind = _link_target(match.group(1), source)
        if "{{" in match.group(1) or "}}" in match.group(1):
            issues.append("dynamic_link_target")
            kind = "dynamic_unresolved"
        links.append(
            {
                "target": target,
                "fragment": fragment,
                "kind": kind,
                "raw": text[match.start() : match.end()],
                "start": match.start(),
                "end": match.end(),
            }
        )
    stack: list[int] = []
    templates = []
    for token in re.finditer(r"\{\{|\}\}", masked):
        if token.group() == "{{":
            stack.append(token.start())
            if len(stack) > 32:
                issues.append("template_nesting_extraction_limit")
                break
        elif stack:
            start = stack.pop()
            end = token.end()
            raw = text[start:end]
            body = raw[2:-2]
            name = body.split("|", 1)[0].strip()
            templates.append(
                {
                    "name": name,
                    "raw_excerpt": raw[:4000],
                    "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                    "start": start,
                    "end": end,
                    "nested": bool(stack),
                    "parameters_raw": body.partition("|")[2][:4000],
                }
            )
            if len(templates) >= 2000:
                issues.append("template_call_extraction_limit")
                break
        else:
            issues.append("unmatched_template_close")
    if stack:
        issues.append("unclosed_template")
    if "{{{" in masked:
        issues.append("template_parameter_or_dynamic_markup")
    return links, sorted(templates, key=lambda item: item["start"]), sorted(set(issues))


def _page_record(page: Any, namespace: str) -> dict[str, Any]:
    def text(node: Any, name: str) -> str:
        value = node.find(namespace + name)
        return value.text or "" if value is not None else ""

    raw_title = text(page, "title")
    page_id = text(page, "id")
    ns = text(page, "ns")
    revisions = page.findall(namespace + "revision")
    if (
        not raw_title
        or not page_id.isdigit()
        or int(page_id) < 1
        or not ns.lstrip("-").isdigit()
        or len(revisions) != 1
    ):
        raise ValueError("Current dump page requires title, numeric page ID/namespace and exactly one revision")
    revision = revisions[0]
    revision_id = text(revision, "id")
    timestamp = text(revision, "timestamp")
    if not revision_id.isdigit() or int(revision_id) < 1 or not timestamp:
        raise ValueError("Page revision lacks an ID or timestamp")
    slots = revision.find(namespace + "slots")
    content = revision
    if slots is not None:
        mains = [slot for slot in slots.findall(namespace + "slot") if slot.get("role") == "main"]
        if len(mains) != 1:
            raise ValueError("Revision slots must identify exactly one main slot")
        content = mains[0]
    model = text(content, "model")
    body = content.find(namespace + "text")
    if body is not None and len(body):
        raise ValueError("MediaWiki text must be escaped character data, not nested XML elements")
    declared_bytes = body.get("bytes") if body is not None else None
    if declared_bytes is not None and not declared_bytes.isdigit():
        raise ValueError("Invalid declared wikitext byte count")
    raw_text = body.text or "" if body is not None else ""
    encoded = raw_text.encode("utf-8")
    if len(encoded) > MAX_PAGE_TEXT_BYTES:
        raise ValueError("Page exceeds the wikitext byte bound")
    missing = body is None or body.get("deleted") is not None or not raw_text and bool(int(declared_bytes or "0"))
    state = (
        "missing_or_suppressed"
        if missing
        else "unsupported_model"
        if model != "wikitext"
        else "empty"
        if not raw_text.strip()
        else "available"
    )
    if not missing and declared_bytes is not None and len(encoded) != int(declared_bytes):
        state = "declared_size_mismatch"
    normalized = _title(raw_title)
    if not normalized:
        raise ValueError("Empty normalized page title")
    redirect = page.find(namespace + "redirect")
    provider_sha1 = (body.get("sha1") if body is not None else None) or text(content, "sha1") or text(revision, "sha1")
    return {
        "page_id": int(page_id),
        "revision_id": int(revision_id),
        "raw_title": raw_title,
        "title": normalized,
        "namespace": int(ns),
        "model": model,
        "timestamp": timestamp,
        "provider_sha1": provider_sha1,
        "wikitext_sha256": hashlib.sha256(encoded).hexdigest() if not missing else None,
        "text_bytes": len(encoded),
        "wikitext": raw_text if int(ns) == 0 else None,
        "redirect_target": _title(redirect.get("title", "")) if redirect is not None else None,
        "content_state": state,
        "source_url": _url(raw_title),
        "revision_url": f"https://ru.wikibooks.org/w/index.php?oldid={revision_id}",
        "history_url": _url(raw_title) + "?action=history",
    }


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE pages(page_id INTEGER PRIMARY KEY,revision_id INTEGER UNIQUE NOT NULL,
            raw_title TEXT NOT NULL,title TEXT UNIQUE NOT NULL,namespace INTEGER NOT NULL,
            model TEXT,timestamp TEXT,provider_sha1 TEXT,wikitext_sha256 TEXT,text_bytes INTEGER NOT NULL,
            wikitext TEXT,redirect_target TEXT,content_state TEXT,source_url TEXT,revision_url TEXT,history_url TEXT);
        CREATE TABLE links(source_id INTEGER NOT NULL,position INTEGER NOT NULL,target_title TEXT,fragment TEXT,
            kind TEXT NOT NULL,raw TEXT NOT NULL,start INTEGER,end INTEGER,PRIMARY KEY(source_id,position));
        CREATE TABLE templates(source_id INTEGER NOT NULL,position INTEGER NOT NULL,name TEXT NOT NULL,
            raw_excerpt TEXT,raw_sha256 TEXT,start INTEGER,end INTEGER,nested INTEGER,parameters_raw TEXT,
            PRIMARY KEY(source_id,position));
        CREATE TABLE page_issues(page_id INTEGER NOT NULL,issue TEXT NOT NULL);
        CREATE INDEX links_target ON links(target_title);
        CREATE INDEX pages_namespace ON pages(namespace);
    """)


def _store_page(connection: sqlite3.Connection, record: dict[str, Any]) -> None:
    fields = (
        "page_id",
        "revision_id",
        "raw_title",
        "title",
        "namespace",
        "model",
        "timestamp",
        "provider_sha1",
        "wikitext_sha256",
        "text_bytes",
        "wikitext",
        "redirect_target",
        "content_state",
        "source_url",
        "revision_url",
        "history_url",
    )
    connection.execute(
        "INSERT INTO pages VALUES(" + ",".join("?" for _ in fields) + ")", tuple(record[k] for k in fields)
    )
    if record["namespace"] != 0 or record["content_state"] != "available":
        return
    links, templates, issues = _markup(record["wikitext"], record["title"])
    connection.executemany(
        "INSERT INTO links VALUES(?,?,?,?,?,?,?,?)",
        [
            (
                record["page_id"],
                i,
                link["target"],
                link["fragment"],
                link["kind"],
                link["raw"],
                link["start"],
                link["end"],
            )
            for i, link in enumerate(links)
        ],
    )
    connection.executemany(
        "INSERT INTO templates VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (
                record["page_id"],
                i,
                t["name"],
                t["raw_excerpt"],
                t["raw_sha256"],
                t["start"],
                t["end"],
                t["nested"],
                t["parameters_raw"],
            )
            for i, t in enumerate(templates)
        ],
    )
    connection.executemany("INSERT INTO page_issues VALUES(?,?)", [(record["page_id"], issue) for issue in issues])


def _site_info(element: Any, namespace: str, spec: DumpSpec) -> dict[str, Any]:
    dbname = element.findtext(namespace + "dbname")
    base = element.findtext(namespace + "base", "")
    if dbname != spec.wiki or urlparse(base).scheme != "https" or urlparse(base).netloc != "ru.wikibooks.org":
        raise ValueError("Dump site identity does not match pinned Russian Wikibooks")
    return {
        "wiki": dbname,
        "base": base,
        "generator": element.findtext(namespace + "generator"),
        "namespaces": {
            n.get("key"): n.text or "" for n in element.findall(namespace + "namespaces/" + namespace + "namespace")
        },
    }


def build_wikimedia_catalog(
    dump_path: Path, output_dir: Path, candidate_titles: list[str] | None = None, *, spec: DumpSpec | None = None
) -> dict[str, Any]:
    """Stream a pinned current dump into a separate, atomic metadata SQLite catalog.

    Raw namespace-zero wikitext is retained exactly for review, never rendered or
    imported. Failed/partial builds cannot replace a previously published catalog.
    """
    spec = spec or WIKIBOOKS_SPEC
    _validate_spec(spec)
    if dump_path.is_symlink() or not _verified(dump_path, spec):
        raise ValueError("Offline dump does not match the bounded pinned size/SHA-256")
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = output_dir / "catalog.sqlite3.building"
    temporary.unlink(missing_ok=True)
    started = time.monotonic()
    connection = sqlite3.connect(temporary)
    connection.row_factory = sqlite3.Row
    try:
        _schema(connection)
        pages = 0
        site = None
        namespace = ""
        root = None
        with dump_path.open("rb") as stream:
            reader = _BoundedBzipReader(stream, MAX_UNCOMPRESSED_BYTES)
            for event, element in ET.iterparse(
                reader, events=("start", "end"), forbid_dtd=True, forbid_entities=True, forbid_external=True
            ):
                if root is None:
                    root = element
                    if element.tag not in {
                        "{http://www.mediawiki.org/xml/export-0.10/}mediawiki",
                        "{http://www.mediawiki.org/xml/export-0.11/}mediawiki",
                    }:
                        raise ValueError("Unexpected MediaWiki export root/namespace")
                    namespace = element.tag.removesuffix("mediawiki")
                if event != "end":
                    continue
                if element.tag == namespace + "siteinfo":
                    if site is not None:
                        raise ValueError("Duplicate dump siteinfo")
                    site = _site_info(element, namespace, spec)
                    root.remove(element)
                elif element.tag == namespace + "page":
                    if site is None:
                        raise ValueError("Page precedes validated siteinfo")
                    _store_page(connection, _page_record(element, namespace))
                    pages += 1
                    if pages > MAX_PAGES:
                        raise ValueError("Dump exceeds the page-count bound")
                    root.remove(element)
            if site is None or not pages:
                raise ValueError("Dump contains no validated pages")
        connection.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            [
                ("catalog_version", CATALOG_VERSION),
                ("dump_sha256", spec.sha256),
                ("dump_url", spec.url),
                ("dump_date", spec.date),
                ("siteinfo", json.dumps(site, ensure_ascii=False)),
                ("license", "CC-BY-SA-4.0; per-page exceptions and borrowed-text rights require review"),
                ("license_url", LICENSE_URL),
                ("license_policy", LICENSE_POLICY),
                ("dump_license_policy", DUMP_LICENSE_POLICY),
            ],
        )
        report = _catalog_report(connection, list(DEFAULT_CANDIDATES) if candidate_titles is None else candidate_titles)
        report.update(
            {
                "schema_version": 1,
                "catalog_version": CATALOG_VERSION,
                "dump_url": spec.url,
                "dump_sha256": spec.sha256,
                "compressed_bytes": spec.bytes,
                "uncompressed_bytes": reader.total,
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "siteinfo": site,
                "library_modified": False,
                "book_completeness_claimed": False,
                "license_url": LICENSE_URL,
                "license_policy": LICENSE_POLICY,
                "dump_license_policy": DUMP_LICENSE_POLICY,
            }
        )
        connection.commit()
        connection.close()
        target = output_dir / "catalog.sqlite3"
        temporary.replace(target)
        report["catalog"] = str(target)
        _write_json(output_dir / "catalog-report.json", report)
        return report
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise


def _resolve(connection: sqlite3.Connection, title: str) -> tuple[sqlite3.Row | None, list[str], str | None]:
    path = []
    for _ in range(16):
        title = _title(title.split("#", 1)[0])
        if title in path:
            return None, path + [title], "redirect_cycle"
        path.append(title)
        row = connection.execute("SELECT * FROM pages WHERE title=?", (title,)).fetchone()
        if row is None and title:
            row = connection.execute("SELECT * FROM pages WHERE title=?", (title[0].upper() + title[1:],)).fetchone()
        if row is None:
            return None, path, "missing_target"
        if not row["redirect_target"]:
            return row, path, None
        title = row["redirect_target"]
    return None, path, "redirect_depth_limit"


def _cycle(edges: dict[str, set[str]]) -> bool:
    # Only explicit inclusion dependencies use this check; normal navigation
    # backlinks commonly cycle and are not treated as broken books.
    visited: set[str] = set()
    active: set[str] = set()

    for start in edges:
        if start in visited:
            continue
        stack = [(start, iter(edges.get(start, set())))]
        visited.add(start)
        active.add(start)
        while stack:
            node, children = stack[-1]
            other = next(children, None)
            if other is None:
                active.remove(node)
                stack.pop()
            elif other in active:
                return True
            elif other not in visited:
                visited.add(other)
                active.add(other)
                stack.append((other, iter(edges.get(other, set()))))
    return False


def _candidate(connection: sqlite3.Connection, requested: str) -> dict[str, Any]:
    root, redirect_path, error = _resolve(connection, requested)
    report: dict[str, Any] = {
        "requested_title": requested,
        "redirect_path": redirect_path,
        "coverage": "unverified",
        "identity_status": "provisional_family_not_verified_unique_work",
        "rendering_status": "raw_wikitext_only",
        "full_book_claimed": False,
    }
    if root is None or root["namespace"] != 0:
        return {**report, "status": error or "noncontent_root"}
    title = root["title"]
    prefix = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
    family_predicate = "namespace=0 AND (title=? OR title LIKE ? ESCAPE '\\')"
    family_count = connection.execute(
        "SELECT COUNT(*) FROM pages WHERE " + family_predicate, (title, prefix)
    ).fetchone()[0]
    if family_count > 2000:
        return {**report, "status": "family_exceeds_review_bound", "family_pages": family_count}
    family = connection.execute(
        "SELECT page_id,revision_id,raw_title,title,timestamp,provider_sha1,wikitext_sha256,text_bytes,"
        "content_state,redirect_target,source_url,revision_url FROM pages "
        "WHERE " + family_predicate + " ORDER BY page_id",
        (title, prefix),
    ).fetchall()
    titles = {p["title"] for p in family}
    edges: list[dict[str, Any]] = []
    missing = []
    references = 0
    root_outside_family = []
    root_order = []
    inclusion: dict[str, set[str]] = {}
    templates = []
    issues = []
    observed_graph_records = 0
    for page in family:
        page_title = page["title"]
        if page["content_state"] != "available":
            issues.append({"title": page_title, "reason": page["content_state"]})
        for link in connection.execute("SELECT * FROM links WHERE source_id=? ORDER BY position", (page["page_id"],)):
            observed_graph_records += 1
            if observed_graph_records > MAX_GRAPH_RECORDS:
                return {**report, "status": "graph_exceeds_review_bound", "family_pages": family_count}
            target = link["target_title"]
            if not target or link["kind"] in {"external", "noncontent_namespace", "dynamic_unresolved"}:
                references += 1
                continue
            resolved, path, problem = _resolve(connection, target)
            target_title = resolved["title"] if resolved else target
            if target_title in titles and resolved is not None:
                edges.append(
                    {
                        "source": page_title,
                        "target": target_title,
                        "fragment": link["fragment"],
                        "source_start": link["start"],
                        "source_end": link["end"],
                        "redirect_path": path,
                    }
                )
                if page_title == title and target_title != title and target_title not in root_order:
                    root_order.append(target_title)
            elif (target == title or target.startswith(title + "/")) and problem:
                missing.append({"source": page_title, "target": target, "reason": problem, "redirect_path": path})
            else:
                references += 1
                if page_title == title and len(root_outside_family) < MAX_LINKS_PER_PAGE:
                    root_outside_family.append(
                        {
                            "target": target_title,
                            "source_start": link["start"],
                            "source_end": link["end"],
                            "exists_in_main_namespace": resolved is not None and resolved["namespace"] == 0,
                            "redirect_path": path,
                            "classification": "reference_or_chapter_membership_requires_review",
                        }
                    )
        for template in connection.execute(
            "SELECT * FROM templates WHERE source_id=? ORDER BY position", (page["page_id"],)
        ):
            observed_graph_records += 1
            if observed_graph_records > MAX_GRAPH_RECORDS:
                return {**report, "status": "graph_exceeds_review_bound", "family_pages": family_count}
            t = dict(template)
            name = t["name"]
            if page_title == title:
                templates.append(t)
            target = _title(name[1:]) if name.startswith(":") else page_title + name if name.startswith("/") else None
            if target:
                resolved, path, problem = _resolve(connection, target)
                if resolved is not None and resolved["title"] in titles:
                    inclusion.setdefault(page_title, set()).add(resolved["title"])
                elif problem:
                    missing.append(
                        {
                            "source": page_title,
                            "target": target,
                            "reason": "inclusion_" + problem,
                            "redirect_path": path,
                        }
                    )
        issues.extend(
            {"title": page_title, "reason": r["issue"]}
            for r in connection.execute("SELECT issue FROM page_issues WHERE page_id=?", (page["page_id"],))
        )
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge["source"], set()).add(edge["target"])
    for source, targets in inclusion.items():
        adjacency.setdefault(source, set()).update(targets)
    reachable: set[str] = set()
    pending = [title]
    while pending:
        node = pending.pop()
        if node not in reachable:
            reachable.add(node)
            pending.extend(adjacency.get(node, set()) - reachable)
    nonredirect_titles = {p["title"] for p in family if not p["redirect_target"]}
    status_claims = []
    for t in templates:
        if re.search(r"название учебника|заверш|готов|статус|качество", _title(t["name"]), re.I):
            status_claims.append(
                {
                    "template": t["name"],
                    "parameters_raw": t["parameters_raw"],
                    "percent_literals": re.findall(r"(?<!\d)(?:100|[0-9]{1,2})\s*%", t["parameters_raw"]),
                    "raw_excerpt": t["raw_excerpt"],
                    "interpretation": "source_claim_not_independent_completion_verification",
                }
            )
    raw = root["wikitext"] or ""
    rights_alerts = []
    if re.search(r"Шилдт|Schildt|Полный справочник по Java", raw, re.I):
        rights_alerts.append("named_third_party_book_source_requires_license_review")
    if re.search(r"перевод.{0,100}(?:английск|en[.:/])", raw, re.I | re.S):
        rights_alerts.append("translation_source_and_contributor_attribution_require_review")
    incomplete = [
        claim for claim in status_claims if any(int(re.sub(r"\D", "", p)) != 100 for p in claim["percent_literals"])
    ]
    draft = bool(
        re.search(
            r"(?:книга|учебник).{0,100}(?:пишется|недописан|заготовк)|(?:незавершён|незавершен)",
            raw[:5000],
            re.I | re.S,
        )
    )
    explicit_cycle = _cycle(inclusion)
    graph_closed = not missing and not issues and nonredirect_titles.issubset(reachable) and not explicit_cycle
    return {
        **report,
        "status": "rights_review_required"
        if rights_alerts
        else "explicit_incomplete_or_draft"
        if incomplete or draft
        else "graph_review_candidate"
        if graph_closed
        else "graph_partial_or_template_navigation",
        "canonical_root_title": title,
        "root_page_id": root["page_id"],
        "root_revision_id": root["revision_id"],
        "root_revision_url": root["revision_url"],
        "root_wikitext_sha256": root["wikitext_sha256"],
        "root_wikitext_bytes": root["text_bytes"],
        "source_url": root["source_url"],
        "family_pages": len(family),
        "family_nonredirect_pages": len(nonredirect_titles),
        "reachable_family_pages": len(nonredirect_titles & reachable),
        "graph_closed_on_explicit_links": graph_closed,
        "graph_scope": "title_subpage_family_only_not_authoritative_book_toc",
        "root_link_order": root_order,
        "chapter_edges": edges,
        "missing_family_targets": missing,
        "unreachable_family_titles": sorted(nonredirect_titles - reachable),
        "nonchapter_reference_links": references,
        "root_outside_family_links": root_outside_family,
        "explicit_inclusion_cycle": explicit_cycle,
        "content_issues": issues,
        "root_templates": templates,
        "explicit_status_claims": status_claims,
        "draft_language_observed": draft,
        "rights_review_alerts": rights_alerts,
        "root_toc_wikitext_excerpt": raw[:6000],
        "family_records": [
            {
                k: p[k]
                for k in (
                    "page_id",
                    "revision_id",
                    "raw_title",
                    "title",
                    "timestamp",
                    "provider_sha1",
                    "wikitext_sha256",
                    "text_bytes",
                    "content_state",
                    "redirect_target",
                    "source_url",
                    "revision_url",
                )
            }
            for p in family
        ],
        "review_requirements": [
            "Resolve root navigation/templates and chapters; explicit links alone are not complete TOC proof.",
            "Verify per-page rights/imported text, contributor history and snapshot provenance.",
            "Render only supported markup faithfully, then check source quotes; no search import performed.",
        ],
    }


def _catalog_report(connection: sqlite3.Connection, candidates: list[str]) -> dict[str, Any]:
    if len(candidates) > MAX_REVIEW_CANDIDATES:
        raise ValueError("Too many candidate titles for a bounded review report")
    counts = dict(connection.execute("SELECT namespace,COUNT(*) FROM pages GROUP BY namespace"))
    states = dict(connection.execute("SELECT content_state,COUNT(*) FROM pages GROUP BY content_state"))
    return {
        "pages_total": sum(counts.values()),
        "pages_by_namespace": counts,
        "content_states": states,
        "main_namespace_pages": counts.get(0, 0),
        "main_namespace_redirects": connection.execute(
            "SELECT COUNT(*) FROM pages WHERE namespace=0 AND redirect_target IS NOT NULL"
        ).fetchone()[0],
        "wikitext_bytes_stored": connection.execute(
            "SELECT COALESCE(SUM(text_bytes),0) FROM pages WHERE namespace=0"
        ).fetchone()[0],
        "link_records": connection.execute("SELECT COUNT(*) FROM links").fetchone()[0],
        "template_records": connection.execute("SELECT COUNT(*) FROM templates").fetchone()[0],
        "metadata_extraction_issues": dict(
            connection.execute("SELECT issue,COUNT(*) FROM page_issues GROUP BY issue ORDER BY issue")
        ),
        "candidates": [_candidate(connection, _title(title)) for title in dict.fromkeys(candidates)],
    }


def export_wikimedia_review_packet(catalog: Path, title: str, destination: Path) -> dict[str, Any]:
    """Publish a bounded family of exact raw source pages for human review.

    The packet is deliberately not a search-library source manifest: no author,
    book completeness, rendered text or unique-work identity is asserted.
    Existing page files have immutable page/revision names and checked SHA-256.
    """
    connection = sqlite3.connect(catalog.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        if metadata.get("catalog_version") != CATALOG_VERSION:
            raise ValueError("Unsupported Wikimedia review catalog version")
        site = json.loads(metadata["siteinfo"])
        if site.get("wiki") != "ruwikibooks":
            raise ValueError("Review packets require a Russian Wikibooks catalog")
        candidate = _candidate(connection, _title(title))
        if "root_page_id" not in candidate:
            raise ValueError("Candidate has no bounded, resolvable main-namespace family")
        output = destination / f"ruwikibooks-page-{candidate['root_page_id']}"
        output.mkdir(parents=True, exist_ok=True)
        records = []
        for record in candidate["family_records"]:
            item = dict(record)
            row = connection.execute(
                "SELECT wikitext,history_url FROM pages WHERE page_id=?", (record["page_id"],)
            ).fetchone()
            if record["content_state"] == "available":
                body = row["wikitext"].encode("utf-8")
                if len(body) != record["text_bytes"] or hashlib.sha256(body).hexdigest() != record["wikitext_sha256"]:
                    raise ValueError("Catalog wikitext no longer matches its recorded size/SHA-256")
                name = f"page-{record['page_id']}-revision-{record['revision_id']}.wikitext"
                path = output / name
                temporary = path.with_name(name + ".tmp")
                if path.is_symlink() or temporary.is_symlink():
                    raise ValueError("Review packet paths must not be symlinks")
                if not path.is_file() or _sha(path) != record["wikitext_sha256"]:
                    temporary.write_bytes(body)
                    temporary.replace(path)
                item["raw_wikitext_file"] = name
            item["history_url"] = row["history_url"]
            records.append(item)
        packet = {
            "schema_version": 1,
            "catalog_version": CATALOG_VERSION,
            "source_dump": {k: metadata[k] for k in ("dump_sha256", "dump_url", "dump_date")},
            "license": {
                "id": "CC-BY-SA-4.0",
                "url": LICENSE_URL,
                "project_policy": LICENSE_POLICY,
                "dump_policy": DUMP_LICENSE_POLICY,
                "attribution": "Russian Wikibooks contributors; per-page source/revision/history URLs retained",
                "per_page_rights_review": "required_before_import; general project license has exceptions",
                "changes": "No text rendering or rewriting; XML-decoded UTF-8 wikitext exported verbatim",
            },
            "review": candidate,
            "pages": records,
            "coverage": "unverified",
            "rendering_status": "raw_wikitext_only",
            "illustrations_included": False,
            "full_book_claimed": False,
            "library_modified": False,
        }
        manifest = output / "review-packet.json"
        _write_json(manifest, packet)
        return {
            "packet": manifest,
            "root_title": candidate["canonical_root_title"],
            "status": candidate["status"],
            "pages": len(records),
            "raw_wikitext_files": sum("raw_wikitext_file" in r for r in records),
            "bytes": sum(r["text_bytes"] for r in records if "raw_wikitext_file" in r),
            "coverage": "unverified",
        }
    finally:
        connection.close()
