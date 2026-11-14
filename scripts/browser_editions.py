"""Check real curated edition links in Chromium, without changing library data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import expect, sync_playwright

PAIRS = [
    ("pg1228-full", "pg2009-full"),
    ("wsru-underground-4d592d8e2fd1", "pg600-full"),
    ("wsru-fathers-and-sons-492401072288", "pg47935-full"),
    ("wsru-fathers-and-sons-492401072288", "pg30723-full"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/editions-browser"))
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    reports = []
    translators = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        for original_id, edition_id in PAIRS:
            original = page.request.get(f"{base}/api/books/{original_id}").json()
            edition = page.request.get(f"{base}/api/books/{edition_id}").json()
            assert original["status"] == edition["status"] == "indexed"
            assert original["work_id"] == edition["work_id"]
            assert original["source_metadata"]["work_identity_status"] == "curated"
            assert edition["source_metadata"]["work_identity_status"] == "curated"
            assert edition_id in {item["id"] for item in original["related_editions"]}
            assert original_id in {item["id"] for item in edition["related_editions"]}
            page.goto(base + "/?" + urlencode({"book": original_id}), wait_until="networkidle")
            related = page.locator("#reader-related-editions")
            expect(related).to_be_visible()
            if related.get_attribute("open") is None:
                related.locator("summary").click()
            link = related.get_by_role("link", name=edition["title"], exact=True)
            expect(link).to_have_attribute("href", "/?" + urlencode({"book": edition_id}))
            link.click()
            expect(page.locator("#reader-sidebar-title")).to_have_text(edition["title"])
            expect(page.locator("#reader-section-title")).to_have_text(edition["sections"][0]["title"])
            expect(page.locator("#reader-text")).to_have_text(edition["sections"][0]["text"])
            # Edition navigation promises the beginning, never inferred alignment.
            expect(page.locator("#found-passage")).to_have_count(0)
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(base + "/?" + urlencode({"book": original_id}), wait_until="networkidle")
            related = page.locator("#reader-related-editions")
            assert related.get_attribute("open") is None, "Mobile edition list should start collapsed"
            related.locator("summary").click()
            expect(related.get_by_role("link", name=edition["title"], exact=True)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            if edition_id == "pg47935-full":
                related.screenshot(path=str(args.artifacts / "russian-english-editions-mobile.png"))
            page.set_viewport_size({"width": 1440, "height": 1000})
            reports.append({"original": original_id, "edition": edition_id, "first_section_verified": True})
        for book_id, query, translator in [
            ("pg600-full", "Soskice", "Juliet Soskice"),
            ("pg30723-full", "Edmands", "J. L. Edmands"),
        ]:
            response = page.request.get(
                f"{base}/api/search", params={"q": query, "book_id": book_id, "mode": "lexical", "limit": 20}
            )
            hits = response.json()["results"]
            hit = next(item for item in hits if item.get("section_translator") == translator)
            assert hit["verified"] and translator in hit["citation"]
            page.goto(
                base + "/?" + urlencode({"book": book_id, "section": hit["section_id"], "passage": hit["id"]}),
                wait_until="networkidle",
            )
            expect(page.locator("#reader-contributor")).to_contain_text(translator)
            expect(page.locator("#found-passage")).to_have_text(hit["text"])
            expect(page.locator("#reader-citation")).to_contain_text("Перевод фрагмента: " + translator)
            translators.append({"book_id": book_id, "section_translator": translator, "exact_quote": True})
        assert not errors, errors
        browser.close()
    report = {
        "result": "passed",
        "pairs": reports,
        "section_translators": translators,
        "browser_errors": errors,
        "library_writes": 0,
    }
    (args.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
