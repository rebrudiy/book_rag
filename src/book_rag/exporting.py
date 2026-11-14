"""Portable Markdown exports from saved research snapshots, without re-fetching books."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+.!|\-])")


def _escape(value: Any, single_line: bool = False) -> str:
    text = str(value if value is not None else "")
    if single_line:
        text = " ".join(text.split())
    text = html.escape(text, quote=False)
    return _MARKDOWN.sub(r"\\\1", text)


def _external_url(value: Any) -> str | None:
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    return quote(value, safe=":/?#[]@!$&'*+,;=%-._~")


def _provenance(item: Mapping[str, Any]) -> dict[str, Any]:
    value = item.get("provenance", item.get("provenance_json", {}))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _reader_url(item: Mapping[str, Any], provenance: Mapping[str, Any]) -> str | None:
    # Only the library can determine whether a saved snapshot still has a live
    # reader location. Never construct a URL from stale provenance identifiers.
    value = item.get("reader_url")
    if item.get("passage_id") is None or not isinstance(value, str):
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    parameters = parse_qs(parsed.query)
    if parsed.scheme or parsed.netloc or parsed.path != "/" or parsed.fragment:
        return None
    if parameters.get("passage") != [str(item["passage_id"])]:
        return None
    for key, field in (("book", "book_id"), ("section", "section_id")):
        if len(parameters.get(key, [])) != 1:
            return None
        if provenance.get(field) is not None and parameters[key] != [str(provenance[field])]:
            return None
    return quote(value, safe=":/?#[]@!$&'*+,;=%-._~")


def _quoted(value: Any) -> list[str]:
    return ["> " + _escape(line) for line in str(value).splitlines()]


def _historical_reader_url(item: Mapping[str, Any], provenance: Mapping[str, Any]) -> str | None:
    version = provenance.get("canonical_version_id")
    identity = item.get("id")
    value = item.get("historical_reader_url")
    if type(identity) is not int or identity < 1 or not isinstance(version, str):
        return None
    if not re.fullmatch(r"cv1-[a-f0-9]{64}", version) or not isinstance(value, str):
        return None
    # The saved record, rather than old passage/section row IDs, owns this URL.
    if value != f"/?saved={identity}":
        return None
    return value


def export_collection(items: list[dict[str, Any]], collection: str | None = None) -> str:
    """Export saved queries, notes and exact quotation snapshots as Markdown.

    Collection filtering is exact (including the unnamed collection ``""``).
    Preserve the supplied order. Sources and provenance survive passage deletion;
    reader links appear only when the caller supplies a currently validated URL.
    All user text is escaped as literal content, including HTML and Markdown.
    """
    selected = [item for item in items if collection is None or item.get("collection", "") == collection]
    title = "Сохранённые исследования" if collection is None else f"Коллекция: {collection or 'Без названия'}"
    lines = ["# " + _escape(title, single_line=True), "", f"Записей: {len(selected)}", ""]
    if not selected:
        return "\n".join(lines + ["В этой коллекции пока нет записей.", ""])
    for ordinal, item in enumerate(selected, 1):
        kind = item.get("kind", "passage")
        heading = item.get("title") or ("Сохранённый запрос" if kind == "query" else "Сохранённый фрагмент")
        lines.extend([rf"## {ordinal}\. " + _escape(heading, single_line=True), ""])
        if item.get("collection"):
            lines.extend(["Коллекция: " + _escape(item["collection"], single_line=True), ""])
        if item.get("created_at"):
            lines.extend(["Сохранено: " + _escape(item["created_at"], single_line=True), ""])
        if item.get("query"):
            lines.extend(["Запрос:", "", *_quoted(item["query"]), ""])
        if item.get("text"):
            lines.extend(["Фрагмент:", "", *_quoted(item["text"]), ""])
        if item.get("citation"):
            lines.extend(["Источник: " + _escape(item["citation"], single_line=True), ""])
        provenance = _provenance(item)
        source_url = _external_url(provenance.get("source_url", item.get("source_url")))
        if source_url:
            lines.extend([f"[Открыть оригинальный источник](<{source_url}>)", ""])
        historical_url = _historical_reader_url(item, provenance)
        if historical_url:
            lines.extend([f"[Читать сохранённое издание](<{historical_url}>)", ""])
        reader_url = _reader_url(item, provenance)
        if reader_url:
            lines.extend([f"[Открыть в локальном читателе](<{reader_url}>)", ""])
        elif kind == "passage" and not historical_url:
            lines.extend(["Сохранён снимок фрагмента; текущая ссылка в читателе недоступна.", ""])
        if provenance.get("rights"):
            lines.extend(["Условия использования: " + _escape(provenance["rights"], single_line=True), ""])
        rights_url = _external_url(provenance.get("rights_url"))
        if rights_url:
            lines.extend([f"[Условия источника](<{rights_url}>)", ""])
        if item.get("note"):
            lines.extend(["Заметка:", "", *_quoted(item["note"]), ""])
        if provenance:
            payload = json.dumps(provenance, ensure_ascii=False, sort_keys=True, indent=2)
            payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            fence = "`" * max(3, 1 + max((len(match.group()) for match in re.finditer(r"`+", payload)), default=0))
            lines.extend(["Метаданные снимка:", "", fence + "json", payload, fence, ""])
    return "\n".join(lines)
