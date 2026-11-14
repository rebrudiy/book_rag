"""Plan a balanced book-edition queue from an existing offline rights catalog.

No network access or book download occurs. Subject labels are transparent keyword
matches against explicit catalog subjects; unknown content remains ``other``.
Selection is deterministic and bounded by language/subject quota reservoirs.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from book_rag.catalog import CURATED_WORK_IDS, _download_format, _rows, _work_type

SUBJECT_AREAS = ("fiction", "philosophy", "history", "science", "economics", "biography", "technical", "other")
# Primary-label priority resolves overlapping explicit subjects consistently. It
# is an acquisition heuristic, not an assertion about the text's central argument.
PRIMARY_PRIORITY = ("fiction", "biography", "philosophy", "economics", "technical", "science", "history")
SUBJECT_PATTERNS = {
    "fiction": r"\b(?:fiction|novels?|short stories|fairy tales|poetry|drama)\b",
    "philosophy": r"\b(?:philosophy|philosophical|ethics|metaphysics|epistemology|stoicism|logic)\b",
    "history": r"\b(?:history|historical|historiography|antiquities|archaeology)\b",
    "science": r"\b(?:physics|chemistry|biology|geology|astronomy|mathematics|botany|zoology|anatomy|physiology|"
    r"medicine|natural history|natural sciences?|natural selection|evolution|scientific)\b|^science\b",
    "economics": r"\b(?:economics|economic|political economy|commerce|finance|banking|taxation|business)\b",
    "biography": r"\b(?:biography|biographies|autobiography|autobiographies|biographical)\b",
    "technical": (
        r"\b(?:engineering|technology|technical|computers?|programming|machinery|"
        r"telecommunications?|industrial arts)\b"
    ),
}


def classify_subjects(subjects: list[str]) -> dict[str, Any]:
    """Return explicit matches and a coarse primary area; never inspect titles."""
    matches: dict[str, list[str]] = {}
    for area, pattern in SUBJECT_PATTERNS.items():
        hits = []
        for subject in subjects:
            if area == "fiction" and re.search(
                r"non[- ]?fiction|history and criticism|criticism and interpretation", subject, re.I
            ):
                continue
            if area == "science" and re.search(r"science fiction", subject, re.I):
                continue
            if re.search(pattern, subject, re.I):
                hits.append(subject)
        if hits:
            matches[area] = hits
    primary = next((area for area in PRIMARY_PRIORITY if area in matches), "other")
    return {
        "primary": primary,
        "areas": list(matches) or ["other"],
        "basis": matches,
        "method": "explicit_subject_keyword_rules_v1",
    }


def _entry(row: dict[str, Any], download: tuple[str, str], classification: dict[str, Any], sha: str) -> dict[str, Any]:
    number = row["number"]
    url, kind = download
    return {
        "id": f"pg{number}-full",
        "title": row["title"],
        "author": row["author"],
        "language": row["languages"][0] if row["languages"] else "und",
        "languages": row["languages"],
        "work_id": CURATED_WORK_IDS.get(number, f"pg{number}"),
        "work_identity_status": "curated" if number in CURATED_WORK_IDS else "provisional",
        "work_type": "book_or_text",
        "edition": f"Project Gutenberg #{number}; electronic edition issued {row['issued'] or 'unknown'}",
        "source_url": f"https://www.gutenberg.org/ebooks/{number}",
        "download_url": url,
        "format": kind,
        "rights": "Public domain in the USA. Check local law outside the USA; Gutenberg trademark license applies.",
        "rights_url": "https://www.gutenberg.org/policy/license.html",
        "coverage": "full",
        "catalog_rights_checked": True,
        "catalog_sha256": sha,
        "catalog_record": f"pg{number}.rdf",
        "subjects": row["subjects"],
        "primary_subject_area": classification["primary"],
        "subject_areas": classification["areas"],
        "subject_classification": classification,
    }


def plan_balanced_corpus(
    catalog_path: Path,
    output: Path,
    per_language: int = 100,
    languages: list[str] | None = None,
) -> dict[str, Any]:
    """Select a deterministic bounded queue, with honest language shortages.

    Each eligible row contributes to a bounded top-K stable-hash reservoir for
    each matching requested language and its primary subject. Selection assigns
    scarce languages first and fills available subjects round-robin, ensuring
    a source ID occurs only once. Counts are catalog editions, not a verified
    count of unique works. Existing output is deterministically replaced.
    """
    if per_language < 1:
        raise ValueError("per_language must be positive")
    requested = list(
        dict.fromkeys(lang.lower() for lang in (languages if languages is not None else ["en", "ru", "fr", "de"]))
    )
    if not requested or any(not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", lang) for lang in requested):
        raise ValueError("languages must contain ISO-style language codes")
    digest = hashlib.sha256()
    with catalog_path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    catalog_sha = digest.hexdigest()
    buckets: dict[tuple[str, str], list[tuple[int, str, dict[str, Any]]]] = {}
    bucket_ids: dict[tuple[str, str], set[str]] = {}
    available = {lang: Counter(dict.fromkeys(SUBJECT_AREAS, 0)) for lang in requested}
    report: dict[str, Any] = {
        "catalog": str(catalog_path),
        "output": str(output),
        "catalog_sha256": catalog_sha,
        "per_language": per_language,
        "requested_languages": requested,
        "records_scanned": 0,
        "eligible_records": 0,
        "skipped_invalid": 0,
        "skipped_rights": 0,
        "skipped_type": 0,
        "skipped_periodical_issue": 0,
        "skipped_language": 0,
        "skipped_format": 0,
    }
    for row in _rows(catalog_path):
        report["records_scanned"] += 1
        if not re.fullmatch(r"[1-9][0-9]{0,8}", row["number"]) or not row["title"].strip():
            report["skipped_invalid"] += 1
            continue
        if row["type"] != "Text":
            report["skipped_type"] += 1
            continue
        if row["rights"].strip().rstrip(".").lower() != "public domain in the usa":
            report["skipped_rights"] += 1
            continue
        if _work_type(row) == "periodical_issue":
            report["skipped_periodical_issue"] += 1
            continue
        matched_languages = [lang for lang in requested if lang in row["languages"]]
        if not matched_languages:
            report["skipped_language"] += 1
            continue
        # Restrict the common format selector to EPUB/UTF-8 TXT: an HTML
        # alternative cannot accidentally win against a usable text candidate.
        formats = [
            (url, media) for url, media in row["formats"] if "application/epub+zip" in media or "text/plain" in media
        ]
        download = _download_format({**row, "formats": formats})
        if download is None:
            report["skipped_format"] += 1
            continue
        report["eligible_records"] += 1
        classification = classify_subjects(row["subjects"])
        area = classification["primary"]
        entry = _entry(row, download, classification, catalog_sha)
        identity = entry["id"]
        priority = int.from_bytes(hashlib.sha256(("bookrag-balanced-v1:" + identity).encode()).digest(), "big")
        for lang in matched_languages:
            available[lang][area] += 1
            key = (lang, area)
            bucket = buckets.setdefault(key, [])
            identities = bucket_ids.setdefault(key, set())
            if identity in identities:
                continue
            item = (-priority, identity, entry)
            if len(bucket) < per_language:
                heapq.heappush(bucket, item)
                identities.add(identity)
            elif priority < -bucket[0][0]:
                removed = heapq.heapreplace(bucket, item)
                identities.remove(removed[1])
                identities.add(identity)
    report["candidate_capacity"] = len(requested) * len(SUBJECT_AREAS) * per_language
    report["candidate_slots_retained"] = sum(len(bucket) for bucket in buckets.values())
    candidates = {
        key: [item[2] for item in sorted(bucket, key=lambda item: (-item[0], item[1]))]
        for key, bucket in buckets.items()
    }
    order = sorted(requested, key=lambda lang: (sum(available[lang].values()), requested.index(lang)))
    selected_ids: set[str] = set()
    selected: list[dict[str, Any]] = []
    selected_counts = {lang: Counter(dict.fromkeys(SUBJECT_AREAS, 0)) for lang in requested}
    positions = Counter()
    for lang in order:
        while sum(selected_counts[lang].values()) < per_language:
            choices = []
            for area in SUBJECT_AREAS:
                key = (lang, area)
                entries = candidates.get(key, [])
                while positions[key] < len(entries) and entries[positions[key]]["id"] in selected_ids:
                    positions[key] += 1
                if positions[key] < len(entries):
                    choices.append(area)
            if not choices:
                break
            area = min(choices, key=lambda area: (selected_counts[lang][area], SUBJECT_AREAS.index(area)))
            key = (lang, area)
            entry = dict(candidates[key][positions[key]])
            positions[key] += 1
            entry.update(language=lang, planning_language=lang)
            selected.append(entry)
            selected_ids.add(entry["id"])
            selected_counts[lang][area] += 1
    report.update(
        {
            "selection_language_order": order,
            "catalog_editions_selected": len(selected),
            "distinct_catalog_work_ids": len({e["work_id"] for e in selected}),
            "work_identity_note": "Provisional catalog identities are not a verified unique-book or unique-work count.",
            "available_records_by_language": {lang: sum(available[lang].values()) for lang in requested},
            "available_by_language_and_subject": {lang: dict(available[lang]) for lang in requested},
            "selected_by_language": {lang: sum(selected_counts[lang].values()) for lang in requested},
            "selected_by_language_and_subject": {lang: dict(selected_counts[lang]) for lang in requested},
            "language_shortages": {
                lang: max(0, per_language - sum(selected_counts[lang].values())) for lang in requested
            },
            "classification_method": "explicit_subject_keyword_rules_v1; unmatched subjects remain other",
            "selection_method": (
                "Stable SHA-256 top-K per language/primary subject; scarce languages first, subject round-robin."
            ),
            "availability_note": (
                "Availability counts eligible catalog records; multilingual records may occur "
                "in multiple language counts. Output IDs are deduplicated."
            ),
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(selected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    report_path = output.with_suffix(".report.json")
    report["report"] = str(report_path)
    temporary = report_path.with_name(report_path.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(report_path)
    return report
