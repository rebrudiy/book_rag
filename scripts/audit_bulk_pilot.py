#!/usr/bin/env python3
"""Read-only source/index/reader checks of the actually acquired bulk pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
from playwright.sync_api import expect, sync_playwright


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def audit(plan: Path, data: Path, origin: str, output: Path) -> dict:
    expected_mirror = json.loads(plan.read_text())["origin"]
    before = json.loads(plan.with_name("protected-before-pilot.json").read_text())
    pilot = plan.with_name("pilot.local-manifest.json")
    entries = json.loads((pilot if pilot.exists() else plan.with_suffix(".local-manifest.json")).read_text())
    assert len(entries) == 20, "Expected exactly twenty actually acquired pilot books"
    checks = []
    passages = 0
    with sqlite3.connect((data / "library.sqlite3").resolve().as_uri() + "?mode=ro", uri=True, timeout=30) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        old = [
            tuple(r)
            for r in db.execute(
                "SELECT * FROM books WHERE id IN(SELECT value FROM json_each(?)) ORDER BY id",
                (json.dumps(before["existing_ids"]),),
            )
        ]
        notes = [tuple(r) for r in db.execute("SELECT * FROM saved ORDER BY id")]
        assert digest(json.dumps(old, ensure_ascii=False)) == before["existing_rows_sha256"]
        assert digest(json.dumps(notes, ensure_ascii=False)) == before["notes_rows_sha256"]
        for e in entries:
            row = db.execute("SELECT * FROM books WHERE id=?", (e["id"],)).fetchone()
            raw, archived = Path(e["local_path"]), Path(row["local_path"])
            expected = e["source_artifact_sha256"]
            assert row["status"] == "indexed" and row["artifact_sha256"] == expected
            assert digest(raw.read_bytes()) == digest(archived.read_bytes()) == expected
            assert raw.stat().st_ino == archived.stat().st_ino and raw.stat().st_dev == archived.stat().st_dev
            payload = json.loads(
                db.execute("SELECT metadata FROM book_metadata WHERE book_id=?", (e["id"],)).fetchone()[0]
            )
            assert payload["bulk_acquisition"]["source_url"] == e["bulk_acquisition"]["source_url"]
            assert (
                payload["illustrations_included"] is False
                and payload["coverage_scope"] == "text_only_without_illustrations"
            )
            sections = db.execute(
                "SELECT COUNT(*),SUM(length(text)) FROM sections WHERE book_id=?", (e["id"],)
            ).fetchone()
            for p in db.execute(
                "SELECT p.text,p.start,p.end,p.sha256,s.text AS section_text,s.book_id "
                "FROM passages p JOIN sections s ON s.id=p.section_id WHERE p.book_id=?",
                (e["id"],),
            ):
                assert p["book_id"] == e["id"] and p["text"] == p["section_text"][p["start"] : p["end"]]
                assert 0 <= p["start"] < p["end"] <= len(p["section_text"]) and digest(p["text"]) == p["sha256"]
                passages += 1
            checks.append(
                {
                    "id": e["id"],
                    "language": e["language"],
                    "sha256": expected,
                    "source_bytes": raw.stat().st_size,
                    "same_inode": True,
                    "sections": sections[0],
                    "canonical_characters": sections[1],
                }
            )
        db.rollback()
    reader_checks = []
    with httpx.Client(base_url=origin, timeout=30) as client:
        for e in entries:
            book = client.get("/api/books/" + e["id"])
            book.raise_for_status()
            book = book.json()
            original = client.get(book["artifact_url"])
            original.raise_for_status()
            assert digest(original.content) == e["source_artifact_sha256"]
            assert book["source_metadata"]["bulk_acquisition"]["source_url"].startswith(expected_mirror)
        sampled = []
        languages = set()
        for e in entries:
            if e["language"] not in languages:
                sampled.append(e)
                languages.add(e["language"])
            if len(sampled) == 3:
                break
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1365, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            for e in sampled:
                book = client.get("/api/books/" + e["id"]).json()
                candidates = [s for s in book["sections"] if len(s["text"]) > 500]
                section = candidates[len(candidates) // 2]
                url = origin + "/?" + urlencode({"book": e["id"], "section": section["id"]})
                page.goto(url, wait_until="networkidle")
                expect(page.locator("#reader-section-title")).to_have_text(section["title"])
                expect(page.locator("#reader-text")).not_to_be_empty()
                assert page.locator("#reader-text").evaluate("el => el.textContent") == section["text"]
                details = page.locator(".source-details")
                if details.get_attribute("open") is None:
                    details.locator(":scope > summary").click()
                expect(page.locator("#reader-source")).to_contain_text("нет иллюстраций")
                output.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output.parent / (e["id"] + ".png")), full_page=False)
                reader_checks.append(
                    {
                        "book_id": e["id"],
                        "language": e["language"],
                        "section_ordinal": section["ordinal"],
                        "exact_dom_text": True,
                        "title_visible": True,
                        "noimages_notice_visible": True,
                    }
                )
            assert not errors, errors
            browser.close()
    result = {
        "observed_at": datetime.now(UTC).isoformat(),
        "books": len(entries),
        "passages_verified": passages,
        "source_bytes": sum(c["source_bytes"] for c in checks),
        "by_language": dict(Counter(c["language"] for c in checks)),
        "protected_old_book_rows_unchanged": len(before["existing_ids"]),
        "saved_notes_unchanged": before["notes_count"],
        "all_original_download_hashes_verified": True,
        "all_cache_archive_pairs_same_inode": True,
        "actual_reader_chromium": reader_checks,
        "books_detail": checks,
        "database_writes": 0,
        "generator_calls": 0,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=Path("data/catalog/bulk-generated-v1/plan.json"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/full"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--output", type=Path, default=Path("data/catalog/bulk-generated-v1/pilot-audit.json"))
    args = parser.parse_args()
    result = audit(args.plan, args.data_dir, args.base_url, args.output)
    print(json.dumps({k: v for k, v in result.items() if k != "books_detail"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
