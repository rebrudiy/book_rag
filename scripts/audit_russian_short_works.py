#!/usr/bin/env python3
"""Inspect locally captured short works; make no HTTP or library requests.

The generated report deliberately contains unapproved review templates. It is
source evidence for an individual audit, not an automatically importable corpus.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from book_rag import russian_short_sources as short
from book_rag import russian_sources as ws


def inspect_short_sources(shared_cache: Path, output: Path) -> dict[str, Any]:
    """Write ignored runtime evidence without changing the provider state."""
    shared_cache, output = shared_cache.resolve(), output.resolve()
    if shared_cache == output or shared_cache in output.parents:
        raise ValueError("Audit output must be separate from provider source/cache state")
    for filename in ("source-audit-packet.json", "reviews-template.json"):
        if (output / filename).exists():
            raise FileExistsError("Preserve existing audit/review evidence; choose a fresh output directory")
    records = []
    missing = []
    reviews: dict[str, Any] = {}
    for selected in short.SHORT_WORKS:
        work = selected.work
        try:
            data, receipt = short._cached_page(shared_cache, work.root)
        except (ValueError, OSError) as error:
            missing.append({"work": work.slug, "error": str(error)})
            continue
        core = ws._core(data)
        notes = [
            {"source_id": str(node.get("id", "")), "text": node.get_text(" ", strip=True)}
            for node in core.select("ol.references > li")
        ]
        for control in core.select("script,style,nav,.mw-editsection,.mw-cite-backlink"):
            control.decompose()
        blocks = [
            {
                "index": index,
                "tag": node.name,
                "classes": node.get("class") or [],
                "text": node.get_text(" ", strip=True),
            }
            for index, node in enumerate(short._blocks(core))
        ]
        headings = [
            {"tag": node.name, "text": node.get_text(" ", strip=True)}
            for node in core.find_all(list(short.HEADINGS))
            if node.get_text().strip()
        ]
        raw_soup = BeautifulSoup(data, "html.parser")
        rights_links = sorted(
            {
                str(a["href"])
                for a in raw_soup.find_all("a", href=True)
                if "creativecommons.org/licenses/" in str(a["href"])
            }
        )
        records.append(
            {
                "work": work.slug,
                "title": work.title,
                "author": work.author,
                "source_receipt": receipt,
                "bytes": len(data),
                "expected_markers": list(selected.expected_markers),
                "headings": headings,
                "notes": notes,
                "blocks": blocks,
                "transcription_license_links": rights_links,
                "completion_claim": "not_verified_by_this_script",
            }
        )
        reviews[work.slug] = {
            "raw_sha256": ws._sha(data),
            "review_status": "review_required",
            "body_start": "",
            "body_end": "",
            "review_basis": "",
            "author_note_ids": [],
            "editorial_note_ids": [],
            "unclassified_note_ids": [n["source_id"] for n in notes],
        }
    result = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "network_requests": 0,
        "library_mutations": 0,
        "captured_pages": len(records),
        "missing": missing,
        "works": records,
        "instructions": "Read original body and every note. Confirm whole sequence and source edition. "
        "Only then edit a separate reviews.json; this script cannot verify original-work completion.",
    }
    output.mkdir(parents=True, exist_ok=True)
    ws._save_json(output / "source-audit-packet.json", result)
    template = output / "reviews-template.json"
    ws._save_json(template, reviews)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-cache", type=Path, default=Path("data/russian/wikisource"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = inspect_short_sources(args.shared_cache, args.output)
    print(
        json.dumps(
            {
                "captured_pages": result["captured_pages"],
                "missing": len(result["missing"]),
                "network_requests": result["network_requests"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
