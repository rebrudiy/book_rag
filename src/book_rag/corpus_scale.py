"""Deterministic, resource-aware growth from the official offline Gutenberg RDF.

One normalized-author/title candidate is queued per group. These groups remain
provisional: translations, retitlings and undisclosed collections need authority
review. No catalog edition is promoted to a verified unique work by this planner.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from book_rag.catalog import _work_type
from book_rag.corpus_planning import _entry, _text_download, _text_rows, classify_subjects
from book_rag.source_variants import _safe_format_url

LANGUAGES = ("ru", "de", "fr", "es", "it", "pt", "nl", "fi", "sv", "da", "en")
AREAS = ("fiction", "philosophy", "history", "science", "economics", "biography", "technical", "other")
ORDINAL = r"(?:\d+|[ivxlcdm]+|first|second|third|fourth|fifth|premier|deuxi[eè]me|erste[rsn]?|zweite[rsn]?)"
COMPONENT = re.compile(
    rf"\b(?:vol(?:ume)?s?\.?|tomes?|tomo|b[äa]nde?|bd\.?|deel|part|parte|teil|book|chapter|chapitre|kapitel)\s*{ORDINAL}\b|"
    rf"\b(?:{ORDINAL})\s+(?:volume|band|teil)\b|(?:том|часть|глава)\s*{ORDINAL}\b",
    re.I,
)
COLLECTION = re.compile(
    r"\b(?:(?:collected|complete)\s+(?:works|poems|stories|tales|essays|letters)|anthology|miscellanies|"
    r"short stories|and other (?:stories|tales|poems)|gesammelte|s[aä]mtliche|oeuvres compl[eè]tes)\b|"
    r"^(?:poems|tales|stories|essays|letters|gedichte|contes|nouvelles|novellen|racconti|poesie|run[oå]ja|cuentos|contos)\b",
    re.I,
)


def normalized_title(title: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", title).casefold()))


def normalized_author(author: str) -> str:
    parts = []
    for name in author.split(";"):
        name = re.sub(r"\b\d{3,4}\s*(?:[-–]\s*\d{0,4})?\b", " ", name)
        parts.append(normalized_title(name))
    return ";".join(sorted(set(parts)))


def work_key(author: str, title: str) -> str:
    return hashlib.sha256((normalized_author(author) + "\0" + normalized_title(title)).encode()).hexdigest()


def rejection_reason(row: dict[str, Any]) -> str | None:
    if row["type"] != "Text":
        return "not_text"
    if row["rights"].strip().rstrip(".").casefold() != "public domain in the usa":
        return "rights_unverified"
    if _work_type(row) == "periodical_issue":
        return "periodical_issue"
    if COMPONENT.search(row["title"]):
        return "explicit_volume_or_component"
    if COLLECTION.search(row["title"]):
        return "explicit_collection"
    if normalized_author(row["author"]) in {"various", "unknown"}:
        return "unresolved_collective_author"
    return None


def _txt(row: dict[str, Any]) -> tuple[str, int] | None:
    options = []
    for url, media in row["formats"]:
        path = urlparse(url).path.lower()
        if (
            not _safe_format_url(url, row["number"])
            or not media.lower().startswith("text/plain")
            or not re.search(r"charset\s*=\s*utf-8\b", media, re.I)
            or path.endswith((".zip", ".gz", ".bz2"))
        ):
            continue
        extent = row.get("format_extents", {}).get(url)
        if not isinstance(extent, int) or extent <= 0:
            continue
        options.append((0 if path.startswith(("/cache/", "/files/")) else 1, extent, url))
    if not options:
        return None
    _, extent, url = min(options)
    return url, extent


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def plan_scale_corpus(
    catalog_path: Path,
    library_db: Path,
    output: Path,
    *,
    limit: int = 7000,
    languages: tuple[str, ...] = LANGUAGES,
    index_bytes_per_text_byte: float = 6.0,
    reserve_gib: float = 4.5,
    min_text_bytes: int = 40000,
) -> dict[str, Any]:
    """Stream metadata into an owned planning DB; select an affordable queue."""
    report_path = output.with_suffix(".report.json")
    if output.exists() and report_path.exists():
        report = json.loads(report_path.read_text())
        if hashlib.sha256(output.read_bytes()).hexdigest() != report["queue_sha256"]:
            raise ValueError("Existing scale queue changed; preserve original planning evidence")
        return {**report, "reused_existing_queue": True}
    if limit < 1 or reserve_gib < 4 or index_bytes_per_text_byte < 1:
        raise ValueError("Invalid scale limit/reserve/forecast multiplier")
    output.parent.mkdir(parents=True, exist_ok=True)
    planning = output.with_suffix(".planning.sqlite3")
    if planning.exists():
        raise FileExistsError("Preserve previous incomplete planning evidence; choose a fresh queue")
    with catalog_path.open("rb") as stream:
        catalog_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    with sqlite3.connect("file:" + str(library_db.resolve()) + "?mode=ro", uri=True) as live:
        existing = live.execute("SELECT id,title,author,work_id FROM books").fetchall()
        baseline = live.execute("SELECT COUNT(*),COUNT(DISTINCT work_id) FROM books WHERE status='indexed'").fetchone()
    existing_ids = {row[0] for row in existing}
    numbers = {m.group(1) for row in existing if (m := re.fullmatch(r"pg([0-9]+)-full", row[0]))}
    free = shutil.disk_usage(library_db.parent).free
    budget = max(0, free - int(reserve_gib * 1024**3))
    counters: Counter[str] = Counter()
    with sqlite3.connect(planning) as db:
        db.executescript("""CREATE TABLE existing_keys(k TEXT PRIMARY KEY,id TEXT);
        CREATE TABLE candidates(k TEXT PRIMARY KEY,id TEXT UNIQUE,lang TEXT,area TEXT,txt_bytes INTEGER,
         projected_bytes INTEGER,entry TEXT,selected INTEGER DEFAULT 0);
        CREATE TABLE dedup_evidence(k TEXT,id TEXT,reason TEXT,representative TEXT);
        CREATE INDEX buckets ON candidates(lang,area,selected,txt_bytes,id);""")
        db.executemany(
            "INSERT OR IGNORE INTO existing_keys VALUES (?,?)", ((work_key(r[2], r[1]), r[0]) for r in existing)
        )
        for row in _text_rows(catalog_path):
            counters["scanned"] += 1
            number = row["number"]
            if not re.fullmatch(r"[1-9][0-9]{0,8}", number) or not row["title"].strip():
                counters["invalid"] += 1
                continue
            key = work_key(row["author"], row["title"])
            identity = f"pg{number}-full"
            if number in numbers:
                db.execute("INSERT OR IGNORE INTO existing_keys VALUES (?,?)", (key, identity))
                counters["existing_id"] += 1
                continue
            reason = rejection_reason(row)
            if reason:
                counters[reason] += 1
                continue
            lang = next((v for v in row["languages"] if v in languages), None)
            if lang is None:
                counters["language"] += 1
                continue
            text = _txt(row)
            if text is None:
                counters["missing_explicit_utf8_size"] += 1
                continue
            txt_url, txt_bytes = text
            if not min_text_bytes <= txt_bytes <= 3 * 1024**2:
                counters["text_size_outside_screen"] += 1
                continue
            variant = _text_download(row)
            if variant is None:
                counters["format"] += 1
                continue
            if variant["format"] != "epub":
                variant = {
                    **variant,
                    "download_url": txt_url,
                    "format": "txt",
                    "format_family": "txt_utf8",
                    "catalog_format_url": txt_url,
                    "url_resolution": "exact_catalog_url",
                }
            extent = row["format_extents"].get(variant["catalog_format_url"], txt_bytes)
            projected = int(txt_bytes * index_bytes_per_text_byte) + int(extent) + 65536
            classification = classify_subjects(row["subjects"])
            entry = _entry(row, (variant["download_url"], variant["format"]), classification, catalog_sha)
            entry.update(
                work_id="candidate-author-title-" + key[:24],
                work_identity_status="provisional",
                language=lang,
                coverage_scope="text_only_without_illustrations",
                illustrations_included=False,
                catalog_record_sha256=row["catalog_record_sha256"],
                catalog_record=row["catalog_member"],
                single_work_candidate_status="provisional_metadata_screened",
                work_identity_basis={
                    "method": "exact_normalized_author_title_v1",
                    "key_sha256": key,
                    "normalized_author": normalized_author(row["author"]),
                    "normalized_title": normalized_title(row["title"]),
                    "limitations": (
                        "Translations/retitlings/hidden collections remain unresolved; not verified unique works"
                    ),
                },
                source_variant={
                    **variant,
                    "reason": "resource_aware_scale_no_images",
                    "catalog_rights": row["rights"],
                    "same_gutenberg_id": number,
                    "illustrations_included": False,
                },
                resource_forecast={
                    "rdf_utf8_text_bytes": txt_bytes,
                    "rdf_utf8_text_url": txt_url,
                    "rdf_artifact_bytes": extent,
                    "estimated_added_bytes": projected,
                    "index_multiplier": index_bytes_per_text_byte,
                },
            )
            if found := db.execute("SELECT id,txt_bytes FROM candidates WHERE k=?", (key,)).fetchone():
                winner = min((found[1], int(found[0][2:-5])), (txt_bytes, int(number)))
                counters["duplicate_candidate_author_title"] += 1
                winner_is_new = winner == (txt_bytes, int(number))
                db.execute(
                    "INSERT INTO dedup_evidence VALUES (?,?,?,?)",
                    (
                        key,
                        found[0] if winner_is_new else identity,
                        "same_normalized_author_title",
                        identity if winner_is_new else found[0],
                    ),
                )
                if not winner_is_new:
                    continue
                db.execute("DELETE FROM candidates WHERE k=?", (key,))
            db.execute(
                "INSERT INTO candidates(k,id,lang,area,txt_bytes,projected_bytes,entry) VALUES (?,?,?,?,?,?,?)",
                (
                    key,
                    identity,
                    lang,
                    classification["primary"],
                    txt_bytes,
                    projected,
                    json.dumps(entry, ensure_ascii=False),
                ),
            )
        duplicates = db.execute("SELECT c.k,c.id,e.id FROM candidates c JOIN existing_keys e ON c.k=e.k").fetchall()
        counters["duplicate_existing_author_title"] = len(duplicates)
        db.executemany(
            "INSERT INTO dedup_evidence VALUES (?,?,?,?)",
            ((k, i, "same_normalized_author_title_as_existing", r) for k, i, r in duplicates),
        )
        db.execute("DELETE FROM candidates WHERE k IN (SELECT k FROM existing_keys)")
        eligible = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        available = {
            lang: db.execute("SELECT COUNT(*) FROM candidates WHERE lang=?", (lang,)).fetchone()[0]
            for lang in languages
        }
        available_buckets = {
            (lang, area): n
            for lang, area, n in db.execute("SELECT lang,area,COUNT(*) FROM candidates GROUP BY lang,area")
        }
        chosen = []
        counts: Counter[tuple[str, str]] = Counter()
        lang_counts: Counter[str] = Counter()
        spent = 0
        while len(chosen) < limit and available_buckets:
            lang, area = min(
                available_buckets,
                key=lambda k: (lang_counts[k[0]], counts[k], languages.index(k[0]), AREAS.index(k[1])),
            )
            found = db.execute(
                "SELECT k,entry,projected_bytes FROM candidates WHERE lang=? AND area=? AND selected=0 "
                "ORDER BY txt_bytes,id LIMIT 1",
                (lang, area),
            ).fetchone()
            if found is None:
                del available_buckets[(lang, area)]
                continue
            key, raw, cost = found
            db.execute("UPDATE candidates SET selected=1 WHERE k=?", (key,))
            if spent + cost > budget:
                counters["budget_omitted"] += 1
                continue
            entry = json.loads(raw)
            chosen.append(entry)
            spent += cost
            counts[(lang, area)] += 1
            lang_counts[lang] += 1
        db.commit()
        with output.with_suffix(".dedup.jsonl").open("w", encoding="utf-8") as evidence:
            for k, identity, reason, representative in db.execute(
                "SELECT d.k,d.id,d.reason,COALESCE(e.id,c.id,d.representative) FROM dedup_evidence d "
                "LEFT JOIN existing_keys e ON e.k=d.k LEFT JOIN candidates c ON c.k=d.k ORDER BY d.k,d.id"
            ):
                evidence.write(
                    json.dumps(
                        {"key_sha256": k, "source_id": identity, "reason": reason, "representative": representative},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    assert not existing_ids.intersection(e["id"] for e in chosen)
    assert len({e["work_id"] for e in chosen}) == len(chosen)
    _save(output, chosen)
    report = {
        "catalog_sha256": catalog_sha,
        "records": dict(counters),
        "eligible_candidate_groups": eligible,
        "available_by_language": available,
        "selected": len(chosen),
        "selected_by_language": dict(lang_counts),
        "selected_by_subject": dict(Counter(e["primary_subject_area"] for e in chosen)),
        "selected_by_format": dict(Counter(e["format"] for e in chosen)),
        "baseline_searchable_editions": baseline[0],
        "baseline_workgroups": baseline[1],
        "free_bytes_before": free,
        "reserve_bytes": int(reserve_gib * 1024**3),
        "budget_bytes": budget,
        "projected_added_bytes": spent,
        "index_bytes_per_text_byte": index_bytes_per_text_byte,
        "rdf_utf8_text_bytes": sum(e["resource_forecast"]["rdf_utf8_text_bytes"] for e in chosen),
        "queue_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "planning_db": str(planning),
        "queue": str(output),
        "scope": "Metadata-screened provisional candidate workgroups, not authority-verified unique works",
        "canonical_duplicates": (
            "Library.import_book rejects exact canonical-content SHA duplicates before searchable insertion"
        ),
        "old_1308_policy": "All existing IDs excluded; no updates to existing covered books",
    }
    _save(report_path, report)
    return report
