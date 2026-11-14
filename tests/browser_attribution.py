"""Verify real Republic editorial attribution against an imported full corpus.

Prepare the documented curated manifest into data/full and run its server on
port 8001. This scenario never mocks responses or changes corpus metadata.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    """Follow a real Jowett introduction hit into an exact, attributed reader span."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/attribution"))
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    base_url = args.base_url.rstrip("/")
    query = "Republic of Plato longest"
    errors: list[str] = []
    expect.set_options(timeout=30000)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base_url, wait_until="networkidle")
        expect(page.locator("#featured-books .book-card").first).to_be_visible()
        page.locator('[data-mode="lexical"]').click()
        page.locator("#query").fill(query)
        page.locator("#search-form").get_by_role("button", name="Найти").click()
        expect(page.locator("#search-results")).to_be_visible()
        page.locator("#book-scope-query").fill("Republic")
        expect(page.locator('#book-filter option[value="pg1497-full"]')).to_have_count(1)
        page.locator("#book-filter").select_option("pg1497-full")
        first = page.locator("#fragments-panel .passage-card").first
        expect(first.locator(".contributor-label")).to_contain_text("Автор раздела: Benjamin Jowett")
        expect(first.locator(".contributor-label")).to_contain_text("Редакционный раздел")
        expect(first.locator(".passage-citation")).to_contain_text("Benjamin Jowett")
        expect(first.locator(".passage-citation")).to_contain_text("Plato")
        quote = first.locator("blockquote").inner_text()
        assert quote.startswith("The Republic of Plato is the longest of his works")
        first.scroll_into_view_if_needed()
        page.screenshot(path=str(args.artifacts / "republic-editorial-search.png"))

        # Independently verify the stored source location, not only the API flag.
        hit = page.request.get(
            f"{base_url}/api/search", params={"q": query, "mode": "lexical", "book_id": "pg1497-full", "limit": 1}
        ).json()["results"][0]
        book = page.request.get(f"{base_url}/api/books/pg1497-full").json()
        section = next(item for item in book["sections"] if item["id"] == hit["section_id"])
        assert hit["text"] == quote == section["text"][hit["start"] : hit["end"]]
        assert hit["section_author"] == section["section_author"] == "Benjamin Jowett"
        assert hit["section_role"] == section["section_role"] == "editorial"
        assert "Plato" in hit["author"] and "Benjamin Jowett" in hit["citation"]

        first.get_by_role("button", name="Открыть в контексте").click()
        expect(page.locator("#found-passage")).to_have_text(quote)
        expect(page.locator("#reader-contributor")).to_have_text("Автор раздела: Benjamin Jowett · Редакционный раздел")
        expect(page.locator("#reader-citation")).to_contain_text("Benjamin Jowett (editorial). В: Plato")
        page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(args.artifacts / "republic-editorial-reader.png"))
        assert not errors, f"Browser errors: {errors}"
        browser.close()
    print(
        json.dumps(
            {
                "result": "passed",
                "book_id": "pg1497-full",
                "section_author": "Benjamin Jowett",
                "book_author": hit["author"],
                "section_role": "editorial",
                "source_location_exact": True,
                "reader_highlight_exact": True,
                "citation_preserves_contributor_and_book_author": True,
                "browser_errors": errors,
                "screenshots": 2,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
