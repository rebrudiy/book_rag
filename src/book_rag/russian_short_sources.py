"""A fixed, audited selection of complete Russian Wikisource short works.

Capture reuses the existing provider cache and access state. Packaging is offline
and requires a raw-SHA-bound review of every source note and body boundary. A
captured webpage alone never establishes complete original-work coverage.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from book_rag import russian_sources as ws
from book_rag.sources import DownloadDeferred, cached_source

LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
TERMS_URL = "https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use"
PACKAGING_TITLE = ws.PACKAGING_TITLE
HEADINGS = {f"h{n}" for n in range(1, 7)}
BLOCKS = HEADINGS | {"p", "pre", "blockquote", "table", "ul", "ol", "center"}
VERSE_CLASSES = {"poem", "stanza", "verse", "epigraph", "epigraph2", "dedication"}


@dataclass(frozen=True, slots=True)
class ShortWork:
    work: ws.RussianWork
    credited_author: str = ""
    expected_markers: tuple[str, ...] = ()
    original_publication: str = ""
    author_source: str = ""


def _work(
    slug: str,
    title: str,
    author: str,
    root: str,
    year: int,
    death: int,
    edition: str,
    *,
    markers: tuple[str, ...] = (),
    credit: str = "",
    publication: str = "",
) -> ShortWork:
    return ShortWork(
        ws.RussianWork(slug, title, author, root, year, death, edition, mode="short"),
        credit,
        markers,
        publication,
        ws._url("Автор:" + author),
    )


SHORT_WORKS = (
    _work(
        "queen-of-spades",
        "Пиковая дама",
        "Александр Сергеевич Пушкин",
        "Пиковая дама (Пушкин)",
        1834,
        1837,
        "Викитека/ФЭБ; Пушкин, ПСС в 10 томах, Наука, 1978, т. 6, с. 210–237; "
        "оригинальный текст без поздних примечаний",
        markers=(*ws.ROMANS[:6], "Заключение"),
        publication="Библиотека для чтения, 1834, т. II, отд. I, с. 109–140",
    ),
    _work(
        "lefty",
        "Левша",
        "Николай Семёнович Лесков",
        "Левша (Лесков)/ПСС 1902—1903 (ВТ)",
        1881,
        1895,
        "Викитека; Лесков, ПСС, 3-е изд., Типография А. Ф. Маркса, 1902, т. 4, с. 109–141; "
        "редакция ВТ с обновлённой орфографией",
        markers=tuple(
            "ГЛАВА " + ordinal + "."
            for ordinal in (
                "ПЕРВАЯ",
                "ВТОРАЯ",
                "ТРЕТЬЯ",
                "ЧЕТВЕРТАЯ",
                "ПЯТАЯ",
                "ШЕСТАЯ",
                "СЕДЬМАЯ",
                "ВОСЬМАЯ",
                "ДЕВЯТАЯ",
                "ДЕСЯТАЯ",
                "ОДИННАДЦАТАЯ",
                "ДВЕНАДЦАТАЯ",
                "ТРИНАДЦАТАЯ",
                "ЧЕТЫРНАДЦАТАЯ",
                "ПЯТНАДЦАТАЯ",
                "ШЕСТНАДЦАТАЯ",
                "СЕМНАДЦАТАЯ",
                "ВОСЕМНАДЦАТАЯ",
                "ДЕВЯТНАДЦАТАЯ",
                "ДВАДЦАТАЯ",
            )
        ),
        publication="Русь, 1881; издание источника ПСС 1902, т. 4",
    ),
    _work(
        "chameleon",
        "Хамелеон",
        "Антон Павлович Чехов",
        "Хамелеон (Чехов)/Вариант журнала «Осколки»",
        1884,
        1904,
        "Викитека/ФЭБ; вариант журнала Осколки, 1884, № 36, с. 4; "
        "восстановлен по аппарату ПСС, Наука, 1975, т. 3, с. 52–55, 487",
        publication="Осколки, 1884, № 36, 8 сентября, с. 4; подпись А. Чехонте",
    ),
    _work(
        "man-in-a-case",
        "Человек в футляре",
        "Антон Павлович Чехов",
        "Человек в футляре (Чехов)",
        1898,
        1904,
        "Викитека/Библиотека Комарова; Чехов, ПСС и писем в 30 томах, Наука, 1977, т. 10, с. 42–54; "
        "источник также указывает издание Маркса 1903, т. XII, с. 130–143",
        publication="Русская мысль, 1898, кн. VII, с. 120–131",
    ),
    _work(
        "dream-ridiculous-man",
        "Сон смешного человека",
        "Фёдор Михайлович Достоевский",
        "Сон смешного человека (Достоевский)",
        1877,
        1881,
        "Викитека; электронная транскрипция az.lib.ru; печатный экземпляр не установлен",
        markers=ws.ROMANS[:5],
        publication="Дневник писателя, 1877, апрель",
    ),
    _work(
        "mumu",
        "Муму",
        "Иван Сергеевич Тургенев",
        "Муму (Тургенев)",
        1854,
        1883,
        "Викитека; электронная транскрипция turgenev.org.ru; печатный экземпляр не установлен",
        publication="Современник, 1854, т. XLIV, № 3, отд. I, с. 9–36",
    ),
    _work(
        "wise-gudgeon",
        "Премудрый пискарь",
        "Михаил Евграфович Салтыков-Щедрин",
        "Премудрый пискарь (Салтыков-Щедрин)",
        1883,
        1889,
        "Викитека/РВБ; Салтыков-Щедрин, Собрание сочинений в 20 томах, "
        "М.: Художественная литература, 1974, т. 16, кн. 1, с. 30–34; "
        "проверено по титульному листу РВБ; заголовок Викитеки содержит ошибочное описание издания",
        credit="М. Е. Салтыков (Щедрин)",
        publication="Общее дело, Женева, 1883, № 55, с. 2–4; в России Отечественные записки, 1884, № 1",
    ),
    _work(
        "red-flower",
        "Красный цветок",
        "Всеволод Михайлович Гаршин",
        "Красный цветок (Гаршин)",
        1883,
        1888,
        "Викитека; электронная транскрипция Библиотеки Максима Мошкова (az.lib.ru); печатный экземпляр не установлен",
        markers=ws.ROMANS[:6],
        publication="Отечественные записки, 1883, т. CCLXX, № 10, отд. I, с. 297–310",
    ),
    _work(
        "how-much-land",
        "Много ли человеку земли нужно",
        "Лев Николаевич Толстой",
        "Много ли человеку земли нужно (Толстой)",
        1886,
        1910,
        "Викитека/РВБ; Толстой, Собрание сочинений в 22 томах, Художественная литература, 1982, т. 10",
        markers=ws.ROMANS[:9],
        publication="Русское богатство, 1886, № 4",
    ),
    _work(
        "three-hermits",
        "Три старца",
        "Лев Николаевич Толстой",
        "Три старца (Толстой)",
        1886,
        1910,
        "Викитека/РВБ; Толстой, Собрание сочинений в 22 томах, Художественная литература, 1982, т. 10",
        publication="Нива, 1886, № 13",
    ),
)


def _check_cooldowns(paths: tuple[Path, ...]) -> None:
    """Explicit shared guards prevent a new destination from bypassing deferral."""
    for path in paths:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("Malformed shared provider access state: " + str(path))
        value = state.get("host_deferred_until") or state.get("retry_at")
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("Malformed shared provider cooldown: " + str(path))
        until = datetime.fromisoformat(value)
        if until.tzinfo is None:
            raise ValueError("Provider cooldown must specify a timezone")
        remaining = (until - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            raise ws.WikiDeferred("shared access-state cooldown: " + str(path), remaining)


def _is_block(node: Tag) -> bool:
    if node.name in BLOCKS:
        return True
    if node.name != "div":
        return False
    return bool(set(node.get("class") or []) & VERSE_CLASSES) or not node.find(list(BLOCKS | {"div"}))


def _cached_page(shared_cache: Path, title: str) -> tuple[bytes, dict[str, Any]]:
    """Read a proven local capture without creating any HTTP client."""
    state = json.loads((shared_cache / "state.json").read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("pages"), dict):
        raise ValueError("Malformed shared Wikisource page state")
    record = state["pages"].get(title)
    url = ws._url(title)
    source = {"id": "wsru-" + ws._sha(url.encode())[:32], "format": "html", "download_url": url}
    cached = cached_source(source, shared_cache / "raw")
    if not isinstance(record, dict) or record.get("status") != "verified" or cached is None:
        raise ValueError("Offline source page lacks a verified cached capture: " + title)
    data = cached.read_bytes()
    if record.get("raw_sha256") != ws._sha(data):
        raise ValueError("Offline source page and revision receipt SHA-256 differ")
    return data, record


def _blocks(core: Tag) -> list[Tag]:
    return [
        node
        for node in core.find_all(True)
        if _is_block(node)
        and not any(_is_block(p) for p in node.parents if isinstance(p, Tag) and p is not core)
        and node.get_text().strip()
    ]


def project_short_work(selection: ShortWork, data: bytes, review: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Project only a SHA-bound, individually reviewed original-work body.

    Reviews must account for *every* source reference ID. This is not an automatic
    inference that an unmarked note is editorial. Changed source bytes require a
    fresh review, even when the source title and original author are unchanged.
    """
    status = review.get("review_status")
    if review.get("raw_sha256") != ws._sha(data) or status not in {
        "verified_original_work",
        "verified_original_body_partial_notes",
    }:
        raise ValueError("A verified review bound to the exact raw source SHA-256 is required")
    for field in ("body_start", "body_end", "review_basis"):
        if not isinstance(review.get(field), str) or not review[field].strip():
            raise ValueError("Source review lacks " + field)
    original = selection.work
    rights_work = replace(original, author=selection.credited_author or original.author)
    rights = ws._rights(rights_work, data)
    core = ws._core(data)
    note_ids = [str(n.get("id", "")) for n in core.select("ol.references > li")]
    author_ids = review.get("author_note_ids", [])
    editorial_ids = review.get("editorial_note_ids", [])
    unverified_ids = review.get("unverified_note_ids", [])
    for ids in (author_ids, editorial_ids, unverified_ids):
        if not isinstance(ids, list) or any(not isinstance(n, str) or not n for n in ids):
            raise ValueError("Reviewed note identifiers must be nonempty strings")
    if (
        len(note_ids) != len(set(note_ids))
        or "" in note_ids
        or len(author_ids + editorial_ids + unverified_ids) != len(set(author_ids + editorial_ids + unverified_ids))
        or set(note_ids) != set(author_ids + editorial_ids + unverified_ids)
    ):
        raise ValueError("Every source note must be classified exactly once by the bound review")
    if unverified_ids and status == "verified_original_work":
        raise ValueError("Unverified source notes prohibit a full-original-work claim")
    coverage = "partial" if status == "verified_original_body_partial_notes" else "full"
    if coverage == "partial" and (not unverified_ids or not str(review.get("partial_reason", "")).strip()):
        raise ValueError("Partial-note coverage needs explicit unverified note IDs and a reason")
    source_printed_pages = [
        {"printed_page": str(node["id"]), "raw_html_id": str(node["id"]), "label": node.get_text()}
        for node in core.select("span.ws-noexport[id]")
        if str(node.get("id", "")).isdigit()
    ]
    notes = {str(n["id"]): n for n in core.select("ol.references > li")}
    # Administrative controls are outside source text; reference bodies are
    # separately accounted for. Do not delete generic tables or inline code.
    for bad in core.select(
        "script,style,nav,.mw-editsection,.mw-cite-backlink,.ws-noexport,.ws-noinclude,.noprint,ol.references"
    ):
        bad.extract()
    blocks = _blocks(core)
    start = [i for i, n in enumerate(blocks) if ws._norm(n.get_text()).startswith(ws._norm(review["body_start"]))]
    end = [i for i, n in enumerate(blocks) if ws._norm(n.get_text()).endswith(ws._norm(review["body_end"]))]
    if len(start) != 1 or len(end) != 1 or end[0] < start[0]:
        raise ValueError("Reviewed source body boundaries are missing, ambiguous or out of order")
    first = start[0]
    expected = tuple(ws._norm(marker) for marker in selection.expected_markers)
    while first > 0 and ws._norm(blocks[first - 1].get_text()) in expected:
        first -= 1
    selected = blocks[first : end[0] + 1]
    markers = [ws._norm(n.get_text()) for n in selected if ws._norm(n.get_text()) in expected]
    if tuple(markers) != expected:
        raise ValueError("Original chapter marker sequence differs from the reviewed complete sequence")
    chapter_bodies = []
    marker_positions = [i for i, node in enumerate(selected) if ws._norm(node.get_text()) in expected]
    for ordinal, position in enumerate(marker_positions):
        stop = marker_positions[ordinal + 1] if ordinal + 1 < len(marker_positions) else len(selected)
        body_texts = []
        for node in selected[position + 1 : stop]:
            if node.name in HEADINGS or set(node.get("class") or []) & VERSE_CLASSES:
                continue
            # A marker followed only by an epigraph, separator, another heading
            # or citation marker does not establish an original narrative body.
            fragment = BeautifulSoup(str(node), "html.parser")
            for reference in fragment.select(".reference"):
                reference.decompose()
            text = fragment.get_text(" ", strip=True)
            if any(character.isalnum() for character in text):
                body_texts.append(text)
        if not body_texts:
            raise ValueError("Original chapter marker has no narrative body: " + selection.expected_markers[ordinal])
        chapter_bodies.append(
            {
                "marker": selection.expected_markers[ordinal],
                "body_blocks": len(body_texts),
                "body_characters": sum(len(text) for text in body_texts),
            }
        )
    # Detect loose text between selected blocks that a tag-only projection would
    # silently drop. This also rejects an unhandled source layout conservatively.
    descendants = list(core.descendants)
    first_pos = next(i for i, n in enumerate(descendants) if n is selected[0])
    last_node = list(selected[-1].descendants)[-1]
    last_pos = next(i for i, n in enumerate(descendants) if n is last_node)
    selected_ids = {id(n) for node in selected for n in [node, *node.descendants]}
    loose = [
        str(n)
        for n in descendants[first_pos : last_pos + 1]
        if not isinstance(n, (Tag, Comment)) and str(n).strip() and id(n) not in selected_ids
    ]
    if loose:
        raise ValueError("Unrepresented literal source text between narrative blocks")
    prefix = original.slug + "-"
    rendered: list[str] = []
    for node in selected:
        fragment = BeautifulSoup(ws._sanitize(node, prefix), "html.parser")
        for line_break in fragment.find_all("br"):
            if line_break.find_parent(["pre", "code"]) is not None:
                continue
            # HTML source formatting after BR is whitespace, not another verse
            # line. Keep explicit BR/BR stanza breaks and all literal code bytes.
            previous, following = line_break.previous_sibling, line_break.next_sibling
            if isinstance(previous, NavigableString):
                previous.replace_with(re.sub(r"(?:[\t ]*(?:\r\n|\r|\n))+[\t ]*$", "", str(previous)))
            if isinstance(following, NavigableString):
                following.replace_with(re.sub(r"^(?:[\t ]*(?:\r\n|\r|\n))+[\t ]*", "", str(following)))
        outer = fragment.find(True)
        if outer is None:
            continue
        if ws._norm(node.get_text()) in expected:
            outer.name = "h2"
        elif outer.name == "center":
            # The captured Pushkin edition uses CENTER for all part headings
            # and the conclusion; other centered original prose still survives.
            outer.name = "div"
        elif outer.name == "div" and not (set(outer.get("class") or []) & VERSE_CLASSES):
            outer.name = "p"
        for link in fragment.select("a[href]"):
            href = str(link["href"])
            match = re.search(r"#" + re.escape(prefix) + r"(cite_note-[^#]+)$", href)
            if match and match.group(1) in editorial_ids + unverified_ids:
                reference = link.find_parent("sup", class_="reference")
                if reference is not None:
                    reference.decompose()
                else:
                    link.decompose()
        rendered.append(str(fragment))
    retained_notes = []
    for note_id in author_ids:
        note = notes[note_id]
        note_text = note.get_text(" ", strip=True)
        retained_notes.append(
            '<p id="' + html.escape(prefix + note_id, quote=True) + '">' + html.escape(note_text) + "</p>"
        )
    if retained_notes:
        rendered.append("<h2>Авторские примечания</h2>" + "\n".join(retained_notes))
    result = "\n".join(rendered)
    if len(BeautifulSoup(result, "html.parser").get_text().strip()) < 200:
        raise ValueError("Reviewed original narrative is unexpectedly short")
    return result, {
        "rights": {**rights, "author_source": selection.author_source, "credited_author": rights_work.author},
        "original_publication": selection.original_publication,
        "body_blocks": len(selected),
        "chapter_markers": list(selection.expected_markers),
        "chapter_bodies": chapter_bodies,
        "source_note_ids": note_ids,
        "author_note_ids": author_ids,
        "editorial_note_ids": editorial_ids,
        "unverified_note_ids": unverified_ids,
        "coverage": coverage,
        "partial_reason": review.get("partial_reason", ""),
        "source_printed_pages": source_printed_pages,
        "printed_page_mapping": "raw_html_anchors_only" if source_printed_pages else "unavailable",
        "review_basis": review["review_basis"],
        "body_text_sha256": ws._sha(BeautifulSoup(result, "html.parser").get_text().encode()),
        "modifications": (
            "Original-body boundaries and notes individually reviewed; "
            "web controls/metadata/late editorial apparatus excluded; "
            "source heading markers retained."
        ),
    }


def acquire_russian_short_works(
    destination: Path,
    *,
    shared_cache: Path,
    reviews_path: Path | None = None,
    cooldown_states: tuple[Path, ...] = (),
    limit: int = 10,
    offline: bool = False,
) -> dict[str, Any]:
    """Capture at most ten known canonical pages; publish only offline-audited works.

    ``shared_cache`` must be the established provider destination, not a fresh
    directory to evade its access state. Additional shared cooldown receipts are
    checked before *any* network request. No SQLite or existing manifest writes.
    An absent review captures source evidence but produces no importable book.
    """
    if not 1 <= limit <= len(SHORT_WORKS):
        raise ValueError("limit must be between one and ten")
    destination, shared_cache = destination.resolve(), shared_cache.resolve()
    if destination == shared_cache:
        raise ValueError("Use a separate output directory; preserve the original five-work manifest")
    if not (shared_cache / "state.json").is_file():
        raise ValueError("The existing shared Wikisource provider state is required")
    if not offline:
        _check_cooldowns((shared_cache / "state.json", *cooldown_states))
    reviews = json.loads(reviews_path.read_text(encoding="utf-8")) if reviews_path else {}
    if not isinstance(reviews, dict):
        raise ValueError("Source reviews must be an object keyed by curated work slug")
    manifest_path = destination / "manifest.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    if not isinstance(previous, list):
        raise ValueError("Existing short-work manifest is malformed; preserve it for inspection")
    allowed_work_ids = {"ru-" + item.work.slug for item in SHORT_WORKS}
    ready: dict[str, dict[str, Any]] = {}
    for entry in previous:
        if not isinstance(entry, dict) or entry.get("work_id") not in allowed_work_ids:
            raise ValueError("Existing output manifest contains an unrelated work; refusing replacement")
        artifact_name = entry.get("local_path")
        if not isinstance(artifact_name, str) or Path(artifact_name).name != artifact_name:
            raise ValueError("Existing output artifact must be a local filename")
        artifact = destination / artifact_name
        if not artifact.is_file() or ws._sha(artifact.read_bytes()) != entry.get("artifact_sha256"):
            raise ValueError("Existing output artifact failed its preserved manifest SHA-256")
        ready[entry["work_id"]] = entry

    def manifest_entries() -> list[dict[str, Any]]:
        return [ready["ru-" + item.work.slug] for item in SHORT_WORKS if "ru-" + item.work.slug in ready]

    acquisition = None if offline else ws._Acquisition(shared_cache)
    offline_cache_hits = 0
    report: dict[str, Any] = {
        "manifest": str(destination / "manifest.json"),
        "works_ready": 0,
        "captured": [],
        "failures": [],
        "deferred": False,
        "import_performed": False,
    }
    try:
        for selection in SHORT_WORKS[:limit]:
            work = selection.work
            try:
                if acquisition is None:
                    data, source = _cached_page(shared_cache, work.root)
                    offline_cache_hits += 1
                else:
                    _check_cooldowns(cooldown_states)
                    data, source = acquisition.pages([work.root])[work.root]
                # Preserve captured bytes even before a human review approves
                # packaging. A URL cache can later advance to another revision.
                archived_source = destination / "sources" / (source["raw_sha256"] + ".html")
                ws._atomic(archived_source, data)
                source = {
                    **source,
                    "provider_cache_path": str(shared_cache / source["raw_path"]),
                    "raw_path": str(archived_source.relative_to(destination)),
                }
                ws._save_json(archived_source.with_suffix(".receipt.json"), source)
                report["captured"].append({"work": work.slug, **source})
                review = reviews.get(work.slug)
                if not isinstance(review, dict):
                    report["failures"].append(
                        {
                            "work": work.slug,
                            "error": "raw source captured; exact-artifact review required",
                            "status": "review_required",
                        }
                    )
                    continue
                body, audit = project_short_work(selection, data, review)
                coverage = audit["coverage"]
                packaging_notice = (
                    "Book RAG выделил полный оригинальный текст и проверенные авторские примечания"
                    if coverage == "full"
                    else "Book RAG выделил проверенный основной текст; полнота авторских примечаний не подтверждена"
                )
                bundle = ws._sha(json.dumps({work.root: source["raw_sha256"]}, sort_keys=True).encode())
                history = (
                    ws.ORIGIN + "/w/index.php?title=" + quote(work.root.replace(" ", "_"), safe="") + "&action=history"
                )
                attribution = (
                    f"{work.title} — {work.author}. {work.edition}. "
                    f"Оригинал: {selection.original_publication}. Общественное достояние. "
                    f"Транскрипция: участники Викитеки, CC BY-SA 4.0; {source['source_url']}; "
                    f"закреплённая редакция {source['permanent_url']}; история участников {history}. "
                    + packaging_notice
                    + "; поздний редакционный и непроверенный аппарат исключён. "
                    "Изображения не включены."
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
                    + "</p><p>"
                    + LICENSE_URL
                    + "</p><p>"
                    + TERMS_URL
                    + "</p><h1>"
                    + html.escape(work.title)
                    + "</h1>"
                    + body
                    + "</body></html>\n"
                ).encode()
                digest = ws._sha(content)
                artifact = destination / f"{work.slug}-{digest[:16]}.html"
                ws._atomic(artifact, content)
                provenance = {
                    "root": source,
                    "audit": audit,
                    "review": review,
                    "artifact_sha256": digest,
                    "source_bundle_sha256": bundle,
                    "attribution": attribution,
                }
                ws._save_json(destination / f"{work.slug}-provenance.json", provenance)
                ready["ru-" + work.slug] = {
                    "id": f"wsru-{work.slug}-{bundle[:12]}",
                    "title": work.title,
                    "author": work.author,
                    "language": "ru",
                    "work_id": "ru-" + work.slug,
                    "work_identity_status": "curated",
                    "work_type": "book_or_text",
                    "edition": work.edition + "; captured wiki revision " + str(source["revision_id"]),
                    "source_url": source["permanent_url"],
                    "format": "html",
                    "local_path": artifact.name,
                    "rights": (
                        "Original work: public domain; Wikisource contributor transcription: CC BY-SA 4.0. "
                        "Source/history/modification credits retained."
                    ),
                    "rights_url": LICENSE_URL,
                    "rights_evidence": audit["rights"],
                    "original_publication": selection.original_publication,
                    "coverage": coverage,
                    "coverage_scope": "original_work_only"
                    if coverage == "full"
                    else "original_body_with_unverified_notes",
                    "illustrations_included": False,
                    "coverage_note": (
                        "All original-body blocks and chapter markers audited against one complete source page; "
                        "all source notes classified; later editorial apparatus omitted."
                        if coverage == "full"
                        else str(audit["partial_reason"])
                    ),
                    "notes_excluded": [
                        {"source_id": note, "reason": "individually_reviewed_later_editorial_note"}
                        for note in audit["editorial_note_ids"]
                    ]
                    + [
                        {"source_id": note, "reason": "unverified_original_note_authorship_or_scope"}
                        for note in audit["unverified_note_ids"]
                    ],
                    "retained_author_notes": len(audit["author_note_ids"]),
                    "attribution": attribution,
                    "retrieved_at": source["retrieved_at"],
                    "packaged_at": datetime.now(UTC).isoformat(),
                    "artifact_sha256": digest,
                    "source_artifact_sha256": digest,
                    "source_bundle_sha256": bundle,
                    "chapter_sources": [{**source, "extraction": audit}],
                    "section_attributions": [
                        {
                            "title_contains": PACKAGING_TITLE,
                            "author": "Book RAG",
                            "role": "editorial",
                            "source_artifact_sha256": digest,
                            "text_contains": packaging_notice,
                        }
                    ],
                }
                ws._save_json(manifest_path, manifest_entries())
            except (DownloadDeferred, ws.WikiDeferred) as error:
                assert acquisition is not None
                acquisition.state["host_deferred_until"] = error.retry_at.isoformat() if error.retry_at else None
                report.update(deferred=True, retry_at=acquisition.state["host_deferred_until"])
                report["failures"].append({"work": work.slug, "error": str(error), "status": "deferred"})
                break
            except httpx.HTTPError as error:
                assert acquisition is not None
                deferred = ws.WikiDeferred(type(error).__name__ + "; no automatic retry", 3600)
                acquisition.state["host_deferred_until"] = deferred.retry_at.isoformat()
                report.update(deferred=True, retry_at=deferred.retry_at.isoformat())
                report["failures"].append({"work": work.slug, "error": str(error), "status": "deferred"})
                break
            except (ValueError, OSError) as error:
                report["failures"].append({"work": work.slug, "error": str(error), "status": "failed"})
            finally:
                if acquisition is not None:
                    acquisition.save()
    finally:
        if acquisition is not None:
            acquisition.close()
            acquisition.save()
        ws._save_json(manifest_path, manifest_entries())
    report.update(
        works_ready=len(ready),
        network_requests=acquisition.network_requests if acquisition is not None else 0,
        cache_hits=acquisition.cache_hits if acquisition is not None else offline_cache_hits,
        offline=offline,
        coverage_counts={kind: sum(e.get("coverage") == kind for e in ready.values()) for kind in ("full", "partial")},
    )
    ws._save_json(destination / "acquisition-report.json", report)
    return report
