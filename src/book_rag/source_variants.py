"""Propose bounded text-format fallbacks from the same verified Gutenberg record.

This module performs no HTTP requests or downloads. The global artifact size
limit remains unchanged. Proposed alternatives require download, parsing and
citation validation by the caller before they can become searchable editions.
"""

from __future__ import annotations

import hashlib
import re
import tarfile
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from book_rag.catalog import MAX_METADATA_BYTES, _parse_rdf

APPROVED_HOSTS = {"www.gutenberg.org", "gutenberg.org"}


def _provider_number(url: str) -> str:
    parsed = urlparse(url)
    match = re.fullmatch(r"/ebooks/([1-9][0-9]{0,8})/?", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in APPROVED_HOSTS
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        raise ValueError("Source URL must identify an official HTTPS Gutenberg ebook record")
    return match.group(1)


def _record(catalog_path: Path, number: str) -> tuple[dict[str, Any], bytes, str] | None:
    """Stream an archive, parsing only the exact requested per-book RDF member."""
    expected = f"pg{number}.rdf"
    if catalog_path.name.endswith((".tar", ".tar.bz2", ".tar.gz", ".tgz")):
        with tarfile.open(catalog_path, "r|*") as archive:
            for member in archive:
                if not member.isfile() or member.name.rsplit("/", 1)[-1] != expected:
                    continue
                if member.size > MAX_METADATA_BYTES:
                    raise ValueError("Gutenberg RDF record exceeds the metadata size limit")
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                with stream:
                    data = stream.read(MAX_METADATA_BYTES + 1)
                for row in _parse_rdf(data):
                    if row["number"] == number:
                        return row, data, member.name
    elif catalog_path.suffix.lower() in {".rdf", ".xml"}:
        if catalog_path.stat().st_size > MAX_METADATA_BYTES:
            raise ValueError("Gutenberg RDF record exceeds the metadata size limit")
        data = catalog_path.read_bytes()
        for row in _parse_rdf(data):
            if row["number"] == number:
                return row, data, catalog_path.name
    else:
        raise ValueError("Variants require official per-book RDF or an RDF catalog archive")
    return None


def _safe_format_url(url: str, number: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in APPROVED_HOSTS or parsed.query or parsed.fragment:
        return False
    path = unquote(parsed.path)
    if (
        any(segment in {".", ".."} for segment in path.split("/"))
        or "\\" in path
        or re.search(r"%(?:2e|2f|5c)", path, re.I)
    ):
        return False
    # Every usable path must identify the same catalog number. A file hosted by
    # Gutenberg is not sufficient proof that it belongs to the requested edition.
    return bool(
        re.fullmatch(rf"/ebooks/{number}\.[A-Za-z0-9.-]+", path)
        or path.startswith(f"/cache/epub/{number}/")
        or path.startswith(f"/files/{number}/")
    )


def _kind(url: str, media: str, number: str) -> tuple[str, str, int] | None:
    path = urlparse(url).path.lower()
    media = media.lower()
    if media == "application/epub+zip" and (
        path.endswith(".epub.noimages") or path.endswith(f"/pg{number}.epub") or path.endswith("-noimages.epub")
    ):
        return "epub_noimages", "epub", 0
    if media.startswith("text/plain") and re.search(r"charset\s*=\s*utf-8\b", media):
        return "txt_utf8", "txt", 1
    if media.startswith("text/html") and path.endswith((".htm", ".html", ".xhtml", ".html.images", ".html.noimages")):
        return "html_text", "html", 2
    return None


def _artifact_family(url: str, number: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith((".epub.noimages", f"/pg{number}.epub", "-noimages.epub")):
        return "epub_noimages"
    if path.endswith((".txt.utf-8", ".txt")):
        return "txt_utf8"
    if path.endswith((".htm", ".html", ".xhtml", ".html.images", ".html.noimages")):
        return "html_text"
    return "other"


def propose_variants(entry: dict[str, Any], catalog_path: Path) -> list[dict[str, Any]]:
    """Return verified same-ebook fallbacks, preserving caller identity/curation.

    No-image EPUB is preferred, then explicitly UTF-8 text, then a textual HTML
    page. URLs are returned exactly as listed in RDF; no new download path is
    guessed. Equivalent existing format families are omitted. Input and catalog
    must both explicitly support US public-domain reuse. Illustrations are not
    included by any proposed text-only acquisition.
    """
    number = _provider_number(str(entry.get("source_url", "")))
    identity = str(entry.get("id", ""))
    if not identity:
        raise ValueError("A source variant needs the existing stable book ID")
    numbered_id = re.fullmatch(r"pg([0-9]+)(?:[-_.].*)?", identity)
    if numbered_id and numbered_id.group(1) != number:
        raise ValueError("Book ID and source Gutenberg record disagree")
    if "public domain in the usa" not in str(entry.get("rights", "")).lower():
        return []
    found = _record(catalog_path, number)
    if found is None:
        return []
    row, rdf_bytes, member = found
    if row["type"] != "Text" or row["rights"].strip().rstrip(".").lower() != "public domain in the usa":
        return []
    with catalog_path.open("rb") as stream:
        catalog_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    current_url = str(entry.get("download_url", ""))
    current_family = _artifact_family(current_url, number)
    reason = (
        "image-heavy artifact too big"
        if re.search(r"(?:\.epub\d?\.images|-images(?:-3)?\.epub)$", current_url)
        else "artifact too big"
    )
    # Keep one best RDF-listed URL per format family. Direct cache/files links
    # win over aliases, but no replacement URL is synthesized.
    candidates: dict[str, tuple[int, int, str, str, str]] = {}
    for url, media in row["formats"]:
        if not _safe_format_url(url, number) or url == current_url:
            continue
        classified = _kind(url, media, number)
        if classified is None:
            continue
        family, kind, rank = classified
        if family == current_family:
            continue
        path = urlparse(url).path
        route_rank = 2 if path.startswith("/ebooks/") else 0 if path.startswith("/cache/") else 1
        candidate = (rank, route_rank, url, kind, media)
        if family not in candidates or candidate < candidates[family]:
            candidates[family] = candidate
    variants = []
    for family, (rank, _, url, kind, media) in sorted(candidates.items(), key=lambda item: item[1]):
        variant = deepcopy(entry)
        original_artifact = {
            key: variant.pop(key)
            for key in ("local_path", "artifact_sha256", "content_sha256", "source_sha256", "status", "error")
            if key in variant
        }
        variant.update(
            download_url=url,
            format=kind,
            illustrations_included=False,
            coverage_scope="text_only_without_illustrations",
        )
        variant["source_variant"] = {
            "status": "proposed",
            "reason": reason,
            "format_family": family,
            "priority": rank,
            "same_gutenberg_id": number,
            "catalog_format_url": url,
            "catalog_media_type": media,
            "catalog_member": member,
            "catalog_record_sha256": hashlib.sha256(rdf_bytes).hexdigest(),
            "catalog_sha256": catalog_sha,
            "catalog_rights": row["rights"],
            "original_download_url": current_url,
            "original_artifact": original_artifact,
            "illustrations_included": False,
            "omitted_content": ["illustrations", "image-embedded labels and visual information"],
            "structure_note": {
                "epub_noimages": "EPUB chapter/spine and footnote structure available; illustrations excluded.",
                "txt_utf8": "Plain UTF-8 text; layout, links and illustration information are unavailable.",
                "html_text": "HTML headings/footnotes available; external images and styles are not downloaded.",
            }[family],
            "validation_required": (
                "Download within the unchanged size cap, parse, then verify chapter/citation integrity."
            ),
        }
        variants.append(variant)
    return variants
