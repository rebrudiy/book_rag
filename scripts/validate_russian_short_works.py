#!/usr/bin/env python3
"""Independently compare projected canonical text with captured source DOM order.

Chapter headings are checked separately. NFC and whitespace-only differences
are allowed in the whole-body check; verse-line fidelity is reported separately.
No source downloads or library writes are performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from bs4 import Comment, Tag

from book_rag import russian_short_sources as short
from book_rag import russian_sources as ws
from book_rag.parsers import _html


def _flat(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", value))


def validate_projection(selection: short.ShortWork, raw: bytes, review: dict[str, Any], body: str) -> dict[str, Any]:
    """Check every non-whitespace source character in the reviewed body once."""
    if review.get("raw_sha256") != ws._sha(raw):
        raise ValueError("Review and raw source differ")
    core = ws._core(raw)
    for comment in core.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    for control in core.select(
        "script,style,nav,.mw-editsection,.mw-cite-backlink,.ws-noexport,.ws-noinclude,.noprint"
    ):
        control.decompose()
    author_notes = []
    for identifier in review["author_note_ids"]:
        note = core.find("li", id=identifier)
        if note is None:
            raise ValueError("Verified author note missing from original source")
        author_notes.append(note.get_text())
    for reference in list(core.select(".reference")):
        links = reference.find_all("a", href=True)
        excluded = review["editorial_note_ids"] + review.get("unverified_note_ids", [])
        if any(str(link["href"]).split("#")[-1] in excluded for link in links):
            reference.decompose()
    expected_markers = {ws._norm(marker) for marker in selection.expected_markers}
    # This source-DOM walk does not use the connector's selected-block list.
    marker_nodes = [node for node in core.find_all(True) if ws._norm(node.get_text()) in expected_markers]
    for node in marker_nodes:
        if node.parent is None:
            continue
        if any(ws._norm(parent.get_text()) in expected_markers for parent in node.parents if isinstance(parent, Tag)):
            continue
        node.decompose()
    source_text = _flat(core.get_text())
    start, end = _flat(review["body_start"]), _flat(review["body_end"])
    if source_text.count(start) != 1 or source_text.count(end) != 1:
        raise ValueError("Independent DOM boundary anchors are not unique")
    begin = source_text.index(start)
    stop = source_text.index(end) + len(end)
    if stop <= begin:
        raise ValueError("Independent DOM source boundaries are reversed")
    expected = source_text[begin:stop] + "".join(_flat(note) for note in author_notes)
    parsed = _html("<html><body>" + body + "</body></html>", "Source proof")
    actual = _flat("".join(section.text for section in parsed.sections))
    if expected != actual:
        offset = next(
            (i for i, (left, right) in enumerate(zip(expected, actual, strict=False)) if left != right),
            min(len(expected), len(actual)),
        )
        raise ValueError(
            f"Source/canonical character stream differs at {offset}; "
            f"source={expected[max(0, offset - 20) : offset + 40]!r}; "
            f"canonical={actual[max(0, offset - 20) : offset + 40]!r}"
        )
    observed = [section.title for section in parsed.sections if ws._norm(section.title) in expected_markers]
    if [ws._norm(title) for title in observed] != [ws._norm(title) for title in selection.expected_markers]:
        raise ValueError("Canonical heading sequence differs from original reviewed markers")
    return {
        "source_character_stream_sha256": hashlib.sha256(expected.encode()).hexdigest(),
        "canonical_character_stream_sha256": hashlib.sha256(actual.encode()).hexdigest(),
        "matched_nonwhitespace_characters": len(actual),
        "canonical_sections": len(parsed.sections),
        "original_chapter_markers": len(observed),
        "retained_author_notes": len(author_notes),
        "proof_scope": (
            "All reviewed original-body and retained-note non-whitespace characters in exact order, "
            "plus complete chapter headings; web controls/printed page labels excluded"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-cache", type=Path, default=Path("data/russian/wikisource"))
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reviews = json.loads(args.reviews.read_text())
    records = []
    for selected in short.SHORT_WORKS:
        bound = reviews.get(selected.work.slug, {})
        try:
            raw, _ = short._cached_page(args.shared_cache, selected.work.root)
            body, audit = short.project_short_work(selected, raw, bound)
            records.append(
                {
                    "work": selected.work.slug,
                    "status": "verified",
                    "coverage": audit["coverage"],
                    **validate_projection(selected, raw, bound, body),
                }
            )
        except ValueError as error:
            records.append({"work": selected.work.slug, "status": "not_verified", "error": str(error)})
    result = {
        "network_requests": 0,
        "library_mutations": 0,
        "verified": sum(r["status"] == "verified" for r in records),
        "coverage_counts": {kind: sum(r.get("coverage") == kind for r in records) for kind in ("full", "partial")},
        "works": records,
    }
    ws._save_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
