"""Bounded Russian Wikisource originals with revision-checked page provenance."""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from book_rag.sources import DownloadDeferred, _retry_delay, cached_source

ORIGIN = "https://ru.wikisource.org"
API = ORIGIN + "/w/api.php"
USER_AGENT = "BookRAG/0.1 (https://github.com/rebrudiy/book_rag) httpx/0.28.1"
REQUEST_INTERVAL = 2.0
MAX_PAGE_BYTES = 5 * 1024 * 1024
PACKAGING_TITLE = "Происхождение и условия использования — Book RAG"
ROMANS = (
    "I",
    "II",
    "III",
    "IV",
    "V",
    "VI",
    "VII",
    "VIII",
    "IX",
    "X",
    "XI",
    "XII",
    "XIII",
    "XIV",
    "XV",
    "XVI",
    "XVII",
    "XVIII",
    "XIX",
)


@dataclass(frozen=True, slots=True)
class RussianWork:
    slug: str
    title: str
    author: str
    root: str
    publication_year: int
    death_year: int
    edition: str
    chapters: tuple[str, ...] = ()
    mode: str = "chapters"
    require_pd_banner: bool = False
    author_note_signatures: tuple[str, ...] = ()


RUSSIAN_WORKS = (
    RussianWork(
        "ivan-ilyich",
        "Смерть Ивана Ильича",
        "Лев Николаевич Толстой",
        "Смерть Ивана Ильича (Толстой)",
        1886,
        1910,
        "Викитека; электронная транскрипция az.lib.ru с исправлением орфографии и форматирования; "
        "дата первой публикации 1886 не является идентификатором печатного источника",
        mode="tolstoy",
        require_pd_banner=True,
    ),
    RussianWork(
        "underground",
        "Записки из подполья",
        "Фёдор Михайлович Достоевский",
        "Записки из подполья (Достоевский)",
        1864,
        1881,
        "Викитека; электронная транскрипция из 21 главы; оригинал опубликован в Эпохе, 1864",
        tuple(
            f"Записки из подполья (Достоевский)/Часть {part}/Глава {chapter}"
            for part, total in ((1, 11), (2, 10))
            for chapter in range(1, total + 1)
        ),
        author_note_signatures=("Федор Достоевский", "Фёдор Достоевский"),
    ),
    RussianWork(
        "overcoat",
        "Шинель",
        "Николай Васильевич Гоголь",
        "Шинель (Гоголь)/ПСС 1938 (СО)",
        1842,
        1852,
        "Викитека; Гоголь, ПСС в 14 томах, АН СССР, 1938, т. 3, с. 139–174; "
        "только оригинальное повествование, без позднего редакционного аппарата",
        mode="gogol",
        require_pd_banner=True,
    ),
    RussianWork(
        "fathers-and-sons",
        "Отцы и дети",
        "Иван Сергеевич Тургенев",
        "Отцы и дети (Тургенев)",
        1862,
        1883,
        "Викитека; электронная транскрипция ilibrary.ru; оригинал опубликован в 1862",
        tuple(f"Отцы и дети (Тургенев)/Глава {n}" for n in range(1, 29)),
        require_pd_banner=True,
    ),
    RussianWork(
        "ward-six",
        "Палата № 6",
        "Антон Павлович Чехов",
        "Палата № 6 (Чехов)",
        1892,
        1904,
        "Викитека/ФЭБ; Чехов, ПСС и писем в 30 томах, Наука, 1977, т. 8, с. 72–126; "
        "оригинальное повествование без поздних редакционных примечаний",
        tuple(f"Палата № 6 (Чехов)/Глава {roman}" for roman in ROMANS),
    ),
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _norm(value: str) -> str:
    return " ".join(value.split()).casefold().replace("ё", "е")


def _url(title: str) -> str:
    return ORIGIN + "/wiki/" + quote(title.replace(" ", "_"), safe="/()")


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _save_json(path: Path, value: Any) -> None:
    _atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


class WikiDeferred(RuntimeError):
    def __init__(self, reason: str, seconds: float = 60.0) -> None:
        self.retry_at = datetime.now(UTC) + timedelta(seconds=seconds)
        super().__init__(f"Wikisource {reason}; host deferred until {self.retry_at.isoformat()}")


class _Acquisition:
    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.state_path = destination / "state.json"
        self.state: dict[str, Any] = (
            json.loads(self.state_path.read_text())
            if self.state_path.exists()
            else {
                "schema_version": 1,
                "pages": {},
                "works": {},
            }
        )
        if (
            not isinstance(self.state, dict)
            or self.state.get("schema_version") != 1
            or not isinstance(self.state.get("pages"), dict)
            or not isinstance(self.state.get("works"), dict)
        ):
            raise ValueError(
                "Invalid Wikisource acquisition state; preserve it for inspection and choose a new destination"
            )
        self.next_request = 0.0
        self.network_requests = 0
        self.cache_hits = 0
        self.client = httpx.Client(
            timeout=45.0, follow_redirects=False, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        )

    def save(self) -> None:
        _save_json(self.state_path, self.state)

    def close(self) -> None:
        self.client.close()

    def fetch(self, url: str, kind: str, refresh: bool = False) -> tuple[bytes, dict[str, Any]]:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "ru.wikisource.org":
            raise ValueError("Source URL must remain on the allowed Wikisource host")
        if kind == "html" and (not parsed.path.startswith("/wiki/") or parsed.query):
            raise ValueError("HTML acquisition requires canonical /wiki/ URLs without query parameters")
        identity = "wsru-" + _sha(url.encode())[:32]
        entry = {"id": identity, "format": kind, "download_url": url}
        raw_dir = self.destination / "raw"
        cached = cached_source(entry, raw_dir)
        if cached is not None and not refresh:
            self.cache_hits += 1
            info = json.loads(cached.with_suffix(cached.suffix + ".source.json").read_text())
            return cached.read_bytes(), {**info, "local_path": str(cached.relative_to(self.destination))}
        deferred_until = self.state.get("host_deferred_until")
        if isinstance(deferred_until, str):
            remaining = (datetime.fromisoformat(deferred_until) - datetime.now(UTC)).total_seconds()
            if remaining > 0:
                raise WikiDeferred("persisted host cooldown", remaining)
        delay = self.next_request - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        start = time.monotonic()
        try:
            with self.client.stream("GET", url) as response:
                self.network_requests += 1
                if response.status_code == 403:
                    raise WikiDeferred("HTTP 403 access denied; review client/provider access", 900)
                if response.status_code == 429 or response.status_code >= 500:
                    seconds = _retry_delay(response, 0) if response.headers.get("retry-after") else 60.0
                    raise DownloadDeferred(response, seconds)
                if response.is_redirect:
                    raise ValueError("Unexpected canonical URL redirect; review source route before acquisition")
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_PAGE_BYTES:
                        raise ValueError("Wikisource response exceeds the bounded page limit")
                    chunks.append(chunk)
                data = b"".join(chunks)
                if not data:
                    raise ValueError("Wikisource returned an empty response")
                if kind == "txt":
                    payload = json.loads(data)
                    if "error" in payload:
                        code = str(payload["error"].get("code", "unknown"))
                        if code in {"maxlag", "ratelimited", "readonly"}:
                            raise WikiDeferred("API " + code)
                        raise ValueError("Wikisource API error: " + code)
                info = {
                    "schema_version": 1,
                    "download_url": url,
                    "final_url": str(response.url),
                    "sha256": _sha(data),
                    "bytes": size,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                }
                path = raw_dir / f"{identity}.{kind}"
                _atomic(path, data)
                _save_json(path.with_suffix(path.suffix + ".source.json"), info)
                return data, {**info, "local_path": str(path.relative_to(self.destination))}
        finally:
            elapsed = time.monotonic() - start
            self.next_request = time.monotonic() + (5.0 if elapsed > 1.0 else REQUEST_INTERVAL)
            self.client.cookies.clear()

    def metadata(self, titles: list[str]) -> dict[str, dict[str, Any]]:
        if not titles:
            return {}
        if len(titles) > 50:
            raise ValueError("Metadata batches are bounded to fifty known titles")
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "titles": "|".join(sorted(titles)),
            "prop": "revisions|info",
            "rvprop": "ids|timestamp|sha1",
            "inprop": "url",
            "maxlag": "5",
        }
        url = str(httpx.URL(API, params=params))
        if len(url) > 6500 and len(titles) > 1:
            middle = len(titles) // 2
            return {**self.metadata(titles[:middle]), **self.metadata(titles[middle:])}
        data, _ = self.fetch(url, "txt", refresh=True)
        payload = json.loads(data)
        result = {}
        for page in payload.get("query", {}).get("pages", []):
            if page.get("missing") or not page.get("revisions"):
                continue
            result[page["title"]] = {
                "page_id": page["pageid"],
                "revision_id": page["revisions"][0]["revid"],
                "revision_timestamp": page["revisions"][0]["timestamp"],
                "provider_sha1": page["revisions"][0].get("sha1", ""),
            }
        return result

    def revision_metadata(self, revision: int) -> dict[str, Any] | None:
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "revids": str(revision),
            "prop": "revisions|info",
            "rvprop": "ids|timestamp|sha1",
            "maxlag": "5",
        }
        data, _ = self.fetch(str(httpx.URL(API, params=params)), "txt", refresh=True)
        for page in json.loads(data).get("query", {}).get("pages", []):
            for item in page.get("revisions", []):
                if item.get("revid") == revision:
                    return {
                        "title": page["title"],
                        "page_id": page["pageid"],
                        "revision_id": revision,
                        "revision_timestamp": item["timestamp"],
                        "provider_sha1": item.get("sha1", ""),
                    }
        return None

    def pages(self, titles: list[str]) -> dict[str, tuple[bytes, dict[str, Any]]]:
        result: dict[str, tuple[bytes, dict[str, Any]]] = {}
        pending = []
        for title in titles:
            record = self.state["pages"].get(title)
            raw_dir = self.destination / "raw"
            source = {"id": "wsru-" + _sha(_url(title).encode())[:32], "format": "html", "download_url": _url(title)}
            cached = cached_source(source, raw_dir)
            if (
                record
                and record.get("status") == "verified"
                and cached is not None
                and _sha(cached.read_bytes()) == record["raw_sha256"]
            ):
                self.cache_hits += 1
                result[title] = (cached.read_bytes(), record)
            else:
                pending.append(title)
        before = self.metadata(pending)
        uncertain = []
        for title in pending:
            if title not in before:
                raise ValueError("Missing source chapter metadata: " + title)
            data, raw = self.fetch(_url(title), "html", refresh=True)
            record = {
                **before[title],
                "title": title,
                "source_url": _url(title),
                "raw_sha256": _sha(data),
                "raw_bytes": len(data),
                "raw_path": raw["local_path"],
                "retrieved_at": raw["retrieved_at"],
                "permanent_url": f"{ORIGIN}/w/index.php?oldid={before[title]['revision_id']}",
                "status": "downloaded",
            }
            self.state["pages"][title] = record
            self.save()
            match = re.search(rb'"wgRevisionId"\s*:\s*(\d+)', data)
            if match:
                actual = int(match.group(1))
                if actual != before[title]["revision_id"]:
                    displayed = self.revision_metadata(actual)
                    if (
                        displayed is None
                        or displayed["page_id"] != before[title]["page_id"]
                        or displayed["title"] != title
                    ):
                        record.update(status="failed", error="HTML/API revision mismatch is not verifiable")
                        self.save()
                        raise ValueError("HTML/API revision mismatch is not verifiable: " + title)
                    record.update(
                        **displayed,
                        api_latest_revision_at_retrieval=before[title]["revision_id"],
                        permanent_url=f"{ORIGIN}/w/index.php?oldid={actual}",
                        status="verified",
                        revision_evidence="html_displayed_revision_verified_by_id",
                    )
                else:
                    record.update(status="verified", revision_evidence="html_wgRevisionId")
            else:
                uncertain.append(title)
            result[title] = (data, record)
            self.save()
        after = self.metadata(uncertain)
        for title in uncertain:
            record = result[title][1]
            if title not in after or after[title]["revision_id"] != before[title]["revision_id"]:
                record.update(status="failed", error="Revision changed across retrieval")
                self.save()
                raise ValueError("Revision changed across retrieval: " + title)
            record.update(status="verified", revision_evidence="fresh_metadata_pre_post_match")
        if uncertain:
            self.save()
        return result


def _core(data: bytes) -> Tag:
    soup = BeautifulSoup(data, "html.parser")
    core = soup.select_one(".mw-parser-output")
    if core is None:
        raise ValueError("Wikisource page lacks its content container")
    return core


def _rights(work: RussianWork, data: bytes) -> dict[str, Any]:
    soup = BeautifulSoup(data, "html.parser")
    core = _core(data)
    text = _norm(core.get_text(" ", strip=True))
    if _norm(work.author) not in text:
        raise ValueError("Source author credit does not match curated work")
    license_link = soup.find("a", href=re.compile(r"creativecommons\.org/licenses/by-sa/4\.0"))
    if license_link is None:
        raise ValueError("Source transcription license is not verified")
    pd = "общественное достояние" in text or "общественное достояние" in _norm(soup.get_text(" ", strip=True))
    if work.require_pd_banner and not pd:
        raise ValueError("Required source public-domain notice is absent")
    if work.publication_year > 1900 or work.death_year > 1910 or work.publication_year > work.death_year:
        raise ValueError("Source is outside the manually verified nineteenth-century original selection")
    if str(work.publication_year) not in text:
        raise ValueError("Source first-publication evidence is absent")
    return {
        "original": "public_domain",
        "publication_year": work.publication_year,
        "author_death_year": work.death_year,
        "source_pd_notice_observed": pd,
        "transcription": "CC BY-SA 4.0",
        "license_url": str(license_link["href"]),
        "basis": (
            "Curated original published during author's lifetime; "
            "source work/author metadata and rights guidance audited."
        ),
        "rights_source": "https://ru.wikisource.org/wiki/Викитека:Авторские_права",
    }


def _chapter_inventory(work: RussianWork, data: bytes) -> list[str]:
    expected = set(work.chapters)
    found = set()
    for link in _core(data).find_all("a", href=True):
        href = str(link["href"])
        target = urlparse(urljoin(_url(work.root), href))
        path = unquote(target.path)
        title = path.removeprefix("/wiki/").replace("_", " ")
        is_chapter = title in expected or (title.startswith(work.root + "/") and "/Глава " in title)
        if not is_chapter:
            continue
        if (
            target.scheme != "https"
            or target.netloc != "ru.wikisource.org"
            or not path.startswith("/wiki/")
            or target.query
        ):
            raise ValueError("Chapter link leaves the allowed canonical source host/path")
        if any(part in {"..", "."} for part in path.split("/")) or "\\" in path:
            raise ValueError("Unsafe chapter path")
        if title not in expected:
            raise ValueError("Unexpected chapter outside curated coverage: " + title)
        found.add(title)
    missing = expected - found
    if missing:
        raise ValueError("Chapter inventory incomplete: " + "; ".join(sorted(missing)))
    return list(work.chapters)


def _sanitize(node: Tag, prefix: str) -> str:
    fragment = BeautifulSoup(str(node), "html.parser")
    for bad in fragment.select("script,style,img,iframe,object,.mw-editsection,.mw-cite-backlink"):
        bad.decompose()
    for tag in fragment.find_all(True):
        for key in list(tag.attrs):
            if key not in {"id", "href", "title", "class"}:
                del tag.attrs[key]
        if tag.has_attr("id"):
            tag["id"] = prefix + str(tag["id"])
        if tag.has_attr("href"):
            href = str(tag["href"])
            if href.startswith("#"):
                tag["href"] = "#" + prefix + href[1:]
            elif href.startswith("/wiki/"):
                tag["href"] = ORIGIN + href
            elif not href.startswith("https://"):
                del tag["href"]
    return str(fragment)


def _extract(work: RussianWork, data: bytes, chapter_title: str) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    core = _core(data)
    kept_notes: list[str] = []
    omitted = []
    for note in core.select("ol.references > li"):
        text = _norm(note.get_text(" ", strip=True))
        author_note = any(_norm(signature) in text for signature in work.author_note_signatures) or bool(
            re.search(r"(?:^|[(\[])\s*(?:примеч(?:ание|\.)|прим\.)\s*автора(?:[.)\]]|$)", text)
        )
        if author_note:
            copied = BeautifulSoup(str(note), "html.parser")
            for backlink in copied.select(".mw-cite-backlink"):
                backlink.decompose()
            note_id = _sha(chapter_title.encode())[:10] + "-" + str(note.get("id", "author-note"))
            # Flatten note block wrappers into one paragraph so mixed inline
            # note prose and a separate signature both survive HTML extraction.
            note_text = copied.get_text(" ", strip=True)
            kept_notes.append('<p id="' + html.escape(note_id, quote=True) + '">' + html.escape(note_text) + "</p>")
        else:
            omitted.append({"source_id": note.get("id", ""), "reason": "unverified_editorial_contributor_or_rights"})
    for bad in core.select(
        "table,script,style,nav,.ws-noexport,.ws-noinclude,.noprint,.navbox,.mw-editsection,ol.references"
    ):
        bad.decompose()
    result: list[tuple[str, str]] = []
    paragraphs: list[str] = []
    current = chapter_title.rsplit("/", 2)[-2:] if "/" in chapter_title else [work.title]
    heading = " / ".join(current)
    active = work.mode not in {"tolstoy", "gogol"}
    seen_romans: list[str] = []
    editorial_heading = ""

    def flush() -> None:
        if paragraphs:
            result.append((heading, "\n".join(paragraphs)))
            paragraphs.clear()

    for node in core.find_all(["h2", "h3", "p", "pre", "blockquote", "div"]):
        if node.name == "div":
            classes = node.get("class") or []
            if not set(classes).intersection({"poem", "stanza", "verse", "epigraph", "epigraph2", "dedication"}):
                continue
        if any(
            isinstance(parent, Tag)
            and set(parent.get("class") or []).intersection(
                {"poem", "stanza", "verse", "epigraph", "epigraph2", "dedication"}
            )
            for parent in node.parents
            if parent is not core
        ):
            continue
        if node.name in {"blockquote", "p"} and node.find(["p", "blockquote"]):
            continue
        text = " ".join(node.get_text().split())
        if not text:
            continue
        if node.name in {"h2", "h3"}:
            if _norm(text).startswith(("примечания", "комментарии", "источники")):
                editorial_heading = text
                break
            if work.mode == "tolstoy" and text in ROMANS[:12]:
                flush()
                heading = "Глава " + text
                seen_romans.append(text)
                active = True
            elif active and text not in ROMANS:
                rendered = BeautifulSoup(_sanitize(node, _sha(chapter_title.encode())[:10] + "-"), "html.parser")
                heading_node = rendered.find(["h2", "h3"])
                if heading_node is not None:
                    heading_node.name = "p"
                paragraphs.append(str(rendered))
            continue
        if work.mode == "gogol" and text.startswith("В департаменте"):
            active = True
        if active:
            rendered = _sanitize(node, _sha(chapter_title.encode())[:10] + "-")
            if node.name == "div" and set(node.get("class") or []).intersection(
                {"epigraph", "epigraph2", "dedication"}
            ):
                fragment = BeautifulSoup(rendered, "html.parser")
                outer = fragment.find("div")
                if outer is not None:
                    outer.name = "blockquote"
                    for leaf in outer.find_all("div"):
                        if not leaf.find(["div", "p", "pre", "blockquote"]):
                            leaf.name = "p"
                rendered = str(fragment)
            paragraphs.append(rendered)
    flush()
    if work.mode == "tolstoy" and seen_romans != list(ROMANS[:12]):
        raise ValueError("Tolstoy narrative must contain all twelve chapters in order")
    if not result or any(len(BeautifulSoup(fragment, "html.parser").get_text().strip()) < 80 for _, fragment in result):
        raise ValueError("Source narrative extraction is empty or unexpectedly short")
    if kept_notes:
        result.append(("Авторские примечания — " + heading, "".join(kept_notes)))
    return result, {
        "retained_author_notes": len(kept_notes),
        "omitted_notes": omitted,
        "editorial_tail_excluded": bool(editorial_heading),
        "excluded_editorial_heading": editorial_heading,
        "narrative_sections": len(result) - bool(kept_notes),
    }


def acquire_russian(destination: Path, limit: int = 5) -> dict[str, Any]:
    """Acquire at most five curated complete originals, persisting partial stages.

    Uses serial canonical HTML and batched Action API metadata only. Failed or
    incomplete works are excluded from the import manifest. Deferrals stop all
    further requests to the Wikisource host. Verified captured pages resume with
    their original revision proof; they are not relabelled with live metadata.
    """
    if not 1 <= limit <= len(RUSSIAN_WORKS):
        raise ValueError("limit must be between one and five")
    destination = destination.resolve()
    acquisition = _Acquisition(destination)
    entries: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "manifest": destination / "manifest.json",
        "works_ready": 0,
        "chapters": 0,
        "failures": [],
        "deferred": False,
        "scope": "five curated complete original works at most",
    }
    try:
        for work in RUSSIAN_WORKS[:limit]:
            record: dict[str, Any] = {"status": "discovered", "coverage": "partial", "completed_source_pages": 0}
            acquisition.state["works"][work.slug] = record
            acquisition.save()
            try:
                root_data, root_source = acquisition.pages([work.root])[work.root]
                rights = _rights(work, root_data)
                titles = _chapter_inventory(work, root_data) if work.chapters else [work.root]
                pages = acquisition.pages(titles)
                narrative: list[tuple[str, str]] = []
                provenance: list[dict[str, Any]] = []
                for title in titles:
                    data, source = pages[title]
                    fragments, extraction = _extract(work, data, title)
                    narrative.extend(fragments)
                    provenance.append(
                        {
                            **source,
                            "extraction": extraction,
                            "edition": work.edition,
                            "rights": rights,
                            "author": work.author,
                        }
                    )
                    record["completed_source_pages"] += 1
                    record["status"] = "processed"
                    acquisition.save()
                bundle = _sha(
                    json.dumps(
                        {p["title"]: p["raw_sha256"] for p in [root_source, *provenance]}, sort_keys=True
                    ).encode()
                )
                attribution = (
                    f"{work.title} — {work.author}. {work.edition}. "
                    f"Источник и история участников: {_url(work.root)}. "
                    "Оригинальное произведение — общественное достояние; вклад участников Викитеки — CC BY-SA 4.0. "
                    "Book RAG выделил оригинальное повествование, сохранил авторские примечания; "
                    "поздний редакционный аппарат с неустановленными правами не включён."
                )
                credits = (
                    "<ul>"
                    + "".join(
                        '<li><a href="'
                        + html.escape(source["source_url"], quote=True)
                        + '">'
                        + html.escape(source["source_url"])
                        + '</a> — <a href="'
                        + html.escape(source["permanent_url"], quote=True)
                        + '">редакция '
                        + str(source["revision_id"])
                        + "</a></li>"
                        for source in [root_source, *provenance]
                        if source is root_source or source["title"] != work.root
                    )
                    + "</ul>"
                )
                content = (
                    '<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>'
                    + html.escape(work.title)
                    + '</title><meta name="author" content="'
                    + html.escape(work.author, quote=True)
                    + '"></head><body><h1>'
                    + html.escape(PACKAGING_TITLE)
                    + "</h1><p>"
                    + html.escape(attribution)
                    + "</p><p>https://creativecommons.org/licenses/by-sa/4.0/</p>"
                    + credits
                    + "\n".join("<h2>" + html.escape(title) + "</h2>" + fragment for title, fragment in narrative)
                    + "</body></html>\n"
                ).encode()
                artifact = destination / f"{work.slug}-{_sha(content)[:16]}.html"
                _atomic(artifact, content)
                book_provenance = {
                    "work": work.slug,
                    "root": root_source,
                    "rights": rights,
                    "source_pages": provenance,
                    "source_bundle_sha256": bundle,
                    "artifact_sha256": _sha(content),
                    "coverage": "full",
                    "coverage_scope": "original_work_only",
                    "expected_source_pages": len(titles),
                    "chapters": sum(p["extraction"]["narrative_sections"] for p in provenance),
                }
                _save_json(destination / f"{work.slug}-provenance.json", book_provenance)
                entry = {
                    "id": f"wsru-{work.slug}-{bundle[:12]}",
                    "title": work.title,
                    "author": work.author,
                    "language": "ru",
                    "work_id": f"ru-{work.slug}",
                    "work_identity_status": "curated",
                    "work_type": "book_or_text",
                    "edition": work.edition + "; captured wiki revision bundle " + bundle[:16],
                    "source_url": _url(work.root),
                    "format": "html",
                    "local_path": artifact.name,
                    "rights": "Original narrative: public domain; Wikisource contributor text: CC BY-SA 4.0. "
                    "Source credits, histories and modification notice retained; later editorial apparatus omitted.",
                    "rights_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                    "rights_evidence": rights,
                    "coverage": "full",
                    "coverage_scope": "original_work_only",
                    "notes_excluded": [
                        {"source_page": p["title"], **note}
                        for p in provenance
                        for note in p["extraction"]["omitted_notes"]
                    ],
                    "retained_author_notes": sum(p["extraction"]["retained_author_notes"] for p in provenance),
                    "editorial_sections_excluded": [
                        {
                            "source_page": p["title"],
                            "heading": p["extraction"]["excluded_editorial_heading"],
                            "reason": "editorial apparatus outside original narrative and verified author notes",
                        }
                        for p in provenance
                        if p["extraction"]["editorial_tail_excluded"]
                    ],
                    "coverage_note": (
                        "All original narrative chapters accounted for; later editorial apparatus excluded."
                    ),
                    "source_bundle_sha256": bundle,
                    "artifact_sha256": _sha(content),
                    "chapter_sources": provenance,
                    "section_attributions": [
                        {"title_contains": PACKAGING_TITLE, "author": "Book RAG", "role": "editorial"}
                    ],
                }
                entries.append(entry)
                record.update(
                    status="ready",
                    coverage="full",
                    coverage_scope="original_work_only",
                    chapters=book_provenance["chapters"],
                    artifact=artifact.name,
                )
                report["chapters"] += book_provenance["chapters"]
            except (DownloadDeferred, WikiDeferred) as error:
                record.update(status="deferred", error=str(error))
                acquisition.state["host_deferred_until"] = error.retry_at.isoformat() if error.retry_at else None
                report.update(deferred=True, retry_at=error.retry_at.isoformat() if error.retry_at else None)
                report["failures"].append({"work": work.slug, "error": str(error), "coverage": "partial"})
                acquisition.save()
                break
            except (ValueError, httpx.HTTPError, OSError) as error:
                record.update(status="failed", error=str(error))
                report["failures"].append({"work": work.slug, "error": str(error), "coverage": "partial"})
            acquisition.save()
            _save_json(report["manifest"], entries)
    finally:
        acquisition.close()
        acquisition.save()
        _save_json(report["manifest"], entries)
    report.update(
        works_ready=len(entries), network_requests=acquisition.network_requests, cache_hits=acquisition.cache_hits
    )
    _save_json(
        destination / "acquisition-report.json", {k: str(v) if isinstance(v, Path) else v for k, v in report.items()}
    )
    return report
