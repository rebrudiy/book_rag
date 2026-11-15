"""Offline Gutenberg catalog discovery with explicit per-edition rights filtering.

No website crawling or network requests happen during discovery. Official RDF
archives carry rights data; the ordinary official CSV does not. Missing rights
are reported and skipped, never inferred from a provider's general reputation.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as ET

RDF_CATALOG_URL = "https://www.gutenberg.org/cache/epub/feeds/rdf-files.tar.bz2"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DC_NS = "http://purl.org/dc/terms/"
PG_NS = "http://www.gutenberg.org/2009/pgterms/"
NS = {"rdf": RDF_NS, "dc": DC_NS, "pg": PG_NS}
MAX_METADATA_BYTES = 2 * 1024 * 1024
# Only manually verified work/edition relationships are merged. Similar titles
# are deliberately not enough to merge translations or unrelated works.
CURATED_WORK_IDS = {
    "11": "pg11-alice",
    "1342": "pg1342-pride",
    "1228": "pg1228-origin",
    "2009": "pg1228-origin",
    "2680": "pg2680-meditations",
    "4650": "pg4650-candide",
    "22367": "pg22367-metamorphosis",
    "5316": "pg5316-pushkin",
    "3300": "pg3300-wealth",
    "1661": "pg1661",
    "84": "pg84",
    "2701": "pg2701",
    "2600": "pg2600",
    "1232": "pg1232",
    "1497": "pg1497",
    "3207": "pg3207",
    "4363": "pg4363",
    "5001": "pg5001",
    "61": "pg61",
    "35": "pg35",
}


def _value(node: Element | None) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def _parse_rdf(data: bytes) -> Iterator[dict[str, Any]]:
    root = ET.fromstring(data)
    for book in root.findall("pg:ebook", NS):
        identity = book.get(f"{{{RDF_NS}}}about", "").rstrip("/").split("/")[-1]
        languages = [_value(n) for n in book.findall("dc:language/rdf:Description/rdf:value", NS)]
        kinds = [_value(n) for n in book.findall("dc:type/rdf:Description/rdf:value", NS)]
        formats: list[tuple[str, str]] = []
        for resource in book.findall("dc:hasFormat/pg:file", NS):
            url = resource.get(f"{{{RDF_NS}}}about", "")
            media_types = [_value(n) for n in resource.findall("dc:format/rdf:Description/rdf:value", NS)]
            formats.extend((url, media_type) for media_type in media_types)
        yield {
            "number": identity,
            "title": _value(book.find("dc:title", NS)),
            "author": "; ".join(_value(n) for n in book.findall("dc:creator/pg:agent/pg:name", NS)) or "Anonymous",
            "languages": languages,
            "type": "Text" if "Text" in kinds else ";".join(kinds),
            "rights": _value(book.find("dc:rights", NS)),
            "issued": _value(book.find("dc:issued", NS)),
            "subjects": [_value(n) for n in book.findall("dc:subject/rdf:Description/rdf:value", NS)],
            "formats": formats,
            "format_extents": {
                str(resource.get(f"{{{RDF_NS}}}about", "")): int(size)
                for resource in book.findall("dc:hasFormat/pg:file", NS)
                if (size := _value(resource.find("dc:extent", NS))).isdigit() and int(size) > 0
            },
        }


def _rows(path: Path) -> Iterator[dict[str, Any]]:
    if path.suffix.lower() in {".rdf", ".xml"}:
        if path.stat().st_size > MAX_METADATA_BYTES:
            raise ValueError("Individual RDF metadata exceeds 2 MiB")
        yield from _parse_rdf(path.read_bytes())
    elif path.name.endswith((".tar.bz2", ".tar.gz", ".tar", ".tgz")):
        # Stream compressed catalog without extracting archive paths to disk.
        with tarfile.open(path, "r|*") as archive:
            for member in archive:
                if not member.isfile() or not member.name.endswith(".rdf"):
                    continue
                if member.size > MAX_METADATA_BYTES:
                    raise ValueError("Individual RDF metadata exceeds 2 MiB")
                stream = archive.extractfile(member)
                if stream is not None:
                    with stream:
                        yield from _parse_rdf(stream.read(MAX_METADATA_BYTES + 1))
    elif path.name.endswith((".csv", ".csv.gz")):
        opener = gzip.open if path.name.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or not {"Text#", "Title", "Language", "Type"}.issubset(reader.fieldnames):
                raise ValueError("CSV must use the official Gutenberg catalog schema")
            for row in reader:
                yield {
                    "number": row.get("Text#", ""),
                    "title": row.get("Title", ""),
                    "author": row.get("Authors", "") or "Anonymous",
                    "type": row.get("Type", ""),
                    "languages": [s.strip() for s in row.get("Language", "").split(";") if s.strip()],
                    "rights": row.get("Rights", row.get("rights", "")),
                    "issued": row.get("Issued", ""),
                    "subjects": [s.strip() for s in row.get("Subjects", "").split(";") if s.strip()],
                    # Enriched CSV may provide a verified file link. The ordinary
                    # official CSV has neither links nor rights and is skipped.
                    "formats": [(row.get("Download URL", ""), row.get("Media Type", "application/epub+zip"))]
                    if row.get("Download URL")
                    else [],
                }
    else:
        raise ValueError("Expected official .rdf, RDF .tar.bz2/.tar.gz, .csv or .csv.gz catalog")


def _work_type(row: dict[str, Any]) -> str:
    """Conservatively flag explicit issue metadata without asserting monographs."""
    if any(re.search(r"\bperiodicals\b", subject, re.I) for subject in row.get("subjects", [])):
        return "periodical_issue"
    title = row.get("title", "")
    volume_issue = re.search(
        r"\b(?:volume|vol\.?)\s+[0-9ivxlcdm]+[,.]?\s+(?:no\.?|number|issue)\s*\d+",
        title,
        re.I,
    )
    magazine_issue = re.search(r"\b(?:journal|magazine|gazette|monthly|weekly|review)\b", title, re.I) and re.search(
        r"\b(?:no\.?|number|issue)\s*\d+",
        title,
        re.I,
    )
    return "periodical_issue" if volume_issue or magazine_issue else "book_or_text"


def _download_format(row: dict[str, Any]) -> tuple[str, str] | None:
    number = row["number"]
    candidates: list[tuple[int, str, str]] = []
    for url, media_type in row["formats"]:
        parsed = urlparse(url)
        if parsed.hostname not in {"www.gutenberg.org", "gutenberg.org"} or parsed.scheme not in {"http", "https"}:
            continue
        path = parsed.path
        if "application/epub+zip" in media_type:
            kind, rank = "epub", 0 if ".epub3." in path else 1
            # Resolve documented generated-format aliases without crawling the
            # website or relying on frontend redirect requests per book.
            if path.startswith("/ebooks/"):
                suffixes = {
                    ".epub3.images": "-images-3.epub",
                    ".epub.images": "-images.epub",
                    ".epub.noimages": ".epub",
                }
                suffix = next((v for k, v in suffixes.items() if path.endswith(k)), None)
                if suffix is None:
                    continue
                path = f"/cache/epub/{number}/pg{number}{suffix}"
        elif "text/html" in media_type and not path.startswith("/ebooks/"):
            kind, rank = "html", 3
        elif "text/plain" in media_type and "utf-8" in media_type.lower() and not path.startswith("/ebooks/"):
            kind, rank = "txt", 4
        else:
            continue
        candidates.append((rank, f"https://www.gutenberg.org{path}", kind))
    if not candidates:
        return None
    _, url, kind = min(candidates)
    return url, kind


def discover_gutenberg(
    catalog_path: Path,
    output: Path,
    limit: int = 100,
    languages: list[str] | None = None,
) -> dict[str, Any]:
    """Append up to ``limit`` eligible editions to a resumable manifest.

    Reads only local metadata. Repeated runs retain existing IDs and continue
    through eligible records; no book is downloaded or marked searchable. Only
    explicit ``Public domain in the USA`` text records qualify. Translation and
    edition rights are evaluated per record. Catalog rows are not unique works.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    existing = json.loads(output.read_text(encoding="utf-8")) if output.exists() else []
    if not isinstance(existing, list) or any(not isinstance(e, dict) or "id" not in e for e in existing):
        raise ValueError("Existing output must be a source manifest list")
    seen = {e["id"] for e in existing}
    by_id = {e["id"]: e for e in existing}
    if len(seen) != len(existing):
        raise ValueError("Existing output contains duplicate source IDs")
    accepted_languages = {lang.lower() for lang in languages} if languages else None
    report: dict[str, Any] = {
        "catalog": str(catalog_path),
        "output": str(output),
        "retained": len(existing),
        "scanned": 0,
        "added": 0,
        "already_present": 0,
        "skipped_rights": 0,
        "skipped_language": 0,
        "skipped_type": 0,
        "skipped_format": 0,
        "skipped_invalid": 0,
    }
    digest = hashlib.sha256()
    with catalog_path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    catalog_sha = digest.hexdigest()
    report["catalog_sha256"] = catalog_sha
    for row in _rows(catalog_path):
        report["scanned"] += 1
        number = row["number"]
        if not re.fullmatch(r"[1-9][0-9]{0,8}", number) or not row["title"].strip():
            report["skipped_invalid"] += 1
            continue
        identity = f"pg{number}-full"
        identity_status = "curated" if number in CURATED_WORK_IDS else "provisional"
        work_type = _work_type(row)
        if identity in seen:
            by_id[identity].update(work_identity_status=identity_status, work_type=work_type)
            report["already_present"] += 1
            continue
        if row["type"] != "Text":
            report["skipped_type"] += 1
            continue
        rights = row["rights"].strip().rstrip(".").lower()
        if rights != "public domain in the usa":
            report["skipped_rights"] += 1
            continue
        row_languages = [lang.lower() for lang in row["languages"]]
        if accepted_languages and not accepted_languages.intersection(row_languages):
            report["skipped_language"] += 1
            continue
        download = _download_format(row)
        if download is None:
            report["skipped_format"] += 1
            continue
        download_url, kind = download
        edition = f"Project Gutenberg #{number}, electronic edition; issued {row['issued'] or 'unknown'}"
        existing.append(
            {
                "id": identity,
                "title": row["title"],
                "author": row["author"],
                "language": row_languages[0] if row_languages else "und",
                "languages": row_languages,
                "work_id": CURATED_WORK_IDS.get(number, f"pg{number}"),
                "work_identity_status": identity_status,
                "work_type": work_type,
                "edition": edition,
                "source_url": f"https://www.gutenberg.org/ebooks/{number}",
                "download_url": download_url,
                "format": kind,
                "rights": (
                    "Public domain in the USA. Check local law outside the USA; Gutenberg trademark license applies."
                ),
                "rights_url": "https://www.gutenberg.org/policy/license.html",
                "coverage": "full",
                "catalog_rights_checked": True,
                "catalog_sha256": catalog_sha,
                "catalog_record": f"pg{number}.rdf",
                "subjects": row["subjects"],
            }
        )
        by_id[identity] = existing[-1]
        seen.add(identity)
        report["added"] += 1
        if report["added"] >= limit:
            break
    report["total"] = len(existing)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    return report
