"""GET-only browser probe of a real large library with installed subset semantics.

One global semantic request is followed by one early lexical fallback request
for an uncovered reference. No API responses are mocked and no notes are changed.
Run model probes serially with other evaluations to keep timings interpretable.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, Route, expect, sync_playwright

QUERY = "Девочка следует за белым кроликом и падает в его нору"
OUTSIDE_BOOK = "ruwikibooks-regex-266423"


def get_json(page: Page, url: str) -> dict[str, Any]:
    response = page.request.get(url)
    assert response.ok, f"GET {url}: {response.status}"
    return response.json()


def probe(base: str, output: Path, covered: int, searchable: int) -> dict[str, Any]:
    errors: list[str] = []
    console_errors: list[str] = []
    forbidden: list[str] = []
    searches: list[dict[str, list[str]]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})

        def only_get(route: Route) -> None:
            if route.request.method != "GET":
                forbidden.append(f"{route.request.method} {route.request.url}")
                route.abort()
                return
            if urlsplit(route.request.url).path == "/api/search":
                searches.append(parse_qs(urlsplit(route.request.url).query))
            route.continue_()

        context.route("**/*", only_get)
        page = context.new_page()
        page.set_default_timeout(60_000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.goto(base, wait_until="networkidle")
        page.locator('[data-mode="semantic"]').click()
        page.locator("#query").fill(QUERY)
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/search") as response:
            page.locator("#search-form").get_by_role("button", name="Найти", exact=True).click()
        result = response.value.json()
        assert result["mode"] == "semantic", result.get("warnings")
        assert result["results"] and result["results"][0]["book_id"] == "pg11-full"
        coverage = result["semantic_coverage"]
        assert (
            coverage["active"] and coverage["indexed_books"] == covered and coverage["searchable_books"] == searchable
        )
        expect(page.locator("#semantic-coverage")).to_be_visible()
        badge = re.sub(r"\s", "", page.locator("#semantic-coverage").inner_text())
        assert f"{covered}из{searchable}изданий" in badge
        first = page.locator("#fragments-panel .passage-card").first
        hit = result["results"][0]
        expect(first.locator("blockquote")).to_have_text(hit["text"])
        assert hit["verified"]
        passage = get_json(page, f"{base}/api/passages/{hit['id']}")
        book = get_json(page, f"{base}/api/books/{hit['book_id']}")
        chapter = next(section for section in book["sections"] if section["id"] == hit["section_id"])
        assert chapter["text"][hit["start"] : hit["end"]] == hit["text"] == passage["text"]
        assert all(book[key] in hit["citation"] for key in ("title", "author", "edition"))
        page.locator("#semantic-coverage").scroll_into_view_if_needed()
        page.screenshot(path=str(output / "desktop-semantic-results.png"))
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#semantic-coverage").scroll_into_view_if_needed()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile results overflow"
        page.screenshot(path=str(output / "mobile-semantic-coverage.png"))
        first.get_by_role("button", name="Открыть в контексте").click()
        expect(page.locator("#found-passage")).to_have_text(hit["text"])
        expect(page.locator("#reader-citation")).to_contain_text(hit["citation"])
        expect(page.locator("#reader-book-meta")).to_contain_text(book["title"])
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile reader overflow"
        page.locator("#found-passage").scroll_into_view_if_needed()
        page.screenshot(path=str(output / "mobile-exact-reader.png"))
        reader_url = page.url
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator("#found-passage").scroll_into_view_if_needed()
        page.screenshot(path=str(output / "desktop-exact-reader.png"))

        # Keep semantic mode and use the existing results view. Change the input
        # before selecting scope: its change handler issues exactly one request,
        # already scoped to the uncovered book, so no second global encode runs.
        page.get_by_role("navigation", name="Основная навигация").get_by_role("link", name="Поиск", exact=True).click()
        page.locator("#query").fill("выражения")
        page.locator("#book-scope-query").fill("Регулярные выражения")
        expect(page.locator(f'#book-filter option[value="{OUTSIDE_BOOK}"]')).to_have_count(1)
        with page.expect_response(
            lambda response: (
                urlsplit(response.url).path == "/api/search"
                and parse_qs(urlsplit(response.url).query).get("book_id") == [OUTSIDE_BOOK]
            )
        ) as response:
            page.locator("#book-filter").select_option(OUTSIDE_BOOK)
        fallback = response.value.json()
        assert fallback["mode"] == "lexical" and fallback["semantic_coverage"]["requested_book_covered"] is False
        assert fallback["results"] and all(
            item["book_id"] == OUTSIDE_BOOK and item["verified"] for item in fallback["results"]
        )
        expect(page.locator("#search-warnings")).to_be_visible()
        expect(page.locator("#search-warnings")).to_contain_text("по словам")
        expect(page.locator("#results-meta")).to_contain_text("по словам")
        page.locator("#semantic-coverage").scroll_into_view_if_needed()
        page.screenshot(path=str(output / "outside-reference-fallback.png"))
        assert len(searches) == 2 and "book_id" not in searches[0] and searches[1]["book_id"] == [OUTSIDE_BOOK]
        assert not forbidden, forbidden
        assert not errors and not console_errors, {"page_errors": errors, "console_errors": console_errors}
        browser.close()
    return {
        "result": "passed",
        "base_url": base,
        "query": QUERY,
        "semantic": {
            "mode": result["mode"],
            "top_book": hit["book_id"],
            "elapsed_ms": result["elapsed_ms"],
            "coverage": coverage,
            "passage_id": hit["id"],
            "reader_url": reader_url,
            "canonical_quote_exact": True,
        },
        "outside": {
            "book_id": OUTSIDE_BOOK,
            "mode": fallback["mode"],
            "elapsed_ms": fallback["elapsed_ms"],
            "verified_results": len(fallback["results"]),
        },
        "search_requests": len(searches),
        "global_semantic_requests": 1,
        "outside_early_fallback_requests": 1,
        "mobile_width": 390,
        "page_errors": errors,
        "console_errors": console_errors,
        "non_get_requests": forbidden,
        "limitations": ["One known situation probe checks availability and UI, not general retrieval quality."],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--output", type=Path, default=Path("artifacts/large-semantic-browser"))
    parser.add_argument("--expected-covered", type=int, default=1308)
    parser.add_argument("--expected-searchable", type=int, default=3991)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = probe(args.base_url.rstrip("/"), args.output, args.expected_covered, args.expected_searchable)
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
