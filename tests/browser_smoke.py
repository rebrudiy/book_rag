"""Run real browser workflows against a running Book Atlas server.

Usage: uv run python tests/browser_smoke.py --base-url http://127.0.0.1:8000
Requires: uv run playwright install chromium
"""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright


def check_workflows(page: Page, base_url: str, artifacts: Path, *, semantic: bool = False) -> dict[str, object]:
    """Verify citation navigation, saving, research, empty states and responsiveness."""
    browser_errors: list[str] = []
    page.on("pageerror", lambda error: browser_errors.append(str(error)))
    for item in page.request.get(f"{base_url}/api/saved").json().get("items", []):
        if item.get("title") in {"Browser smoke: exact citation", "Browser smoke: saved query"}:
            page.request.delete(f"{base_url}/api/saved/{item['id']}")
    page.goto(base_url, wait_until="networkidle")
    expect(page.locator("#featured-books .book-card").first).to_be_visible()
    expect(page.locator("#stat-books")).not_to_have_text("—")
    page.screenshot(path=str(artifacts / "desktop-home.png"), full_page=True)
    stats = page.request.get(f"{base_url}/api/stats").json()
    expect(page.locator("#stat-works")).to_have_text(str(stats["works"]))
    expect(page.locator(".library-strip")).to_contain_text("изданий в поиске")
    expect(page.locator(".library-strip")).to_contain_text("групп произведений")
    if stats.get("work_count_is_provisional") is not False:
        expect(page.locator("#corpus-scope")).to_contain_text("не подтверждают число уникальных произведений")

    page.locator('[data-mode="lexical"]').click()
    page.locator("#query").fill("rabbit")
    page.locator("#search-form").get_by_role("button", name="Найти").click()
    expect(page.locator("#fragments-panel .passage-card").first).to_be_visible()
    first = page.locator("#fragments-panel .passage-card").first
    expected_quote = first.locator("blockquote").inner_text()
    first.get_by_role("button", name="Открыть в контексте").click()
    expect(page.locator("#reader-view")).to_be_visible()
    expect(page.locator("#found-passage")).to_have_text(expected_quote)
    assert page.locator("#reader-citation").is_visible(), "Citation is missing in reader"
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(artifacts / "desktop-reader.png"), full_page=True)
    page.locator("#reader-citation").get_by_role("button", name="Сохранить фрагмент").click()
    expect(page.locator("#save-dialog")).to_be_visible()
    page.locator("#save-title").fill("Browser smoke: exact citation")
    page.locator("#save-collection").fill("Browser verification")
    page.locator("#save-note").fill("Verified against the reader's highlighted source text.")
    page.locator("#save-form").get_by_role("button", name="Сохранить", exact=True).click()
    expect(page.locator("#save-dialog")).not_to_be_visible()
    page.get_by_role("navigation", name="Основная навигация").get_by_text("Сохранённое").click()
    expect(page.locator("#saved-items").get_by_role("heading", name="Browser smoke: exact citation")).to_be_visible()
    page.locator("#collection-filter").select_option(label="Browser verification")
    saved = page.locator(".saved-item").filter(has=page.get_by_role("heading", name="Browser smoke: exact citation"))
    expect(saved.locator("blockquote")).to_have_text(expected_quote)
    page.reload(wait_until="networkidle")
    expect(page.get_by_role("heading", name="Browser smoke: exact citation")).to_be_visible()
    page.locator("#collection-filter").select_option(label="Browser verification")
    with page.expect_download() as download_info:
        page.locator("#export-markdown").click()
    export_path = download_info.value.path()
    assert export_path is not None
    markdown = Path(export_path).read_text(encoding="utf-8")
    quoted = "\n".join(line[2:] for line in markdown.splitlines() if line.startswith("> "))
    unescaped = html.unescape(re.sub(r"\\([\\`*_{}\[\]()#+.!|\-])", r"\1", quoted))
    assert expected_quote in unescaped, "Export must preserve the exact literal quotation"
    assert "Browser verification" in markdown
    assert "gutenberg.org/ebooks/11" in markdown, "Export must retain original source provenance"

    page.get_by_role("navigation", name="Основная навигация").get_by_role("link", name="Поиск", exact=True).click()
    page.locator("#query").fill("rabbit")
    page.locator("#search-form").get_by_role("button", name="Найти").click()
    expect(page.locator("#fragments-panel .passage-card").first).to_be_visible()
    page.locator("#save-query").click()
    page.locator("#save-title").fill("Browser smoke: saved query")
    page.locator("#save-collection").fill("Browser verification")
    page.locator("#save-form").get_by_role("button", name="Сохранить", exact=True).click()
    expect(page.locator("#save-dialog")).not_to_be_visible()
    page.locator("#research-tab").click()
    page.locator("#run-research").click()
    expect(page.locator("#research-output .research-method")).to_be_visible()
    assert page.locator("#research-output .passage-card").count() > 0
    page.screenshot(path=str(artifacts / "desktop-research.png"), full_page=True)

    page.locator("#query").fill("zzzxxyy92841noreference")
    page.locator("#search-form").get_by_role("button", name="Найти").click()
    expect(page.locator("#fragments-panel").get_by_role("heading", name="Точного следа пока нет")).to_be_visible()
    page.get_by_role("navigation", name="Основная навигация").get_by_role("link", name="Библиотека").click()
    page.locator("#library-language").select_option("ru")
    assert page.locator("#library-books .book-card").count() >= 1
    expect(page.locator("#library-books .book-language").first).to_contain_text("Русский")

    # Follow a vocabulary theme to real passages, then inspect a related-book edge.
    page.get_by_role("navigation", name="Основная навигация").get_by_role("link", name="Карта тем").click()
    expect(page.locator("#theme-cloud .theme-chip").first).to_be_visible()
    theme = page.locator("#theme-cloud .theme-chip").first
    term = theme.locator("span").inner_text()
    theme.click()
    expect(page.locator("#map-evidence h2")).to_have_text(term)
    page.locator("#map-evidence").get_by_role("button", name="Найти фрагменты").click()
    expect(page.locator("#fragments-panel .passage-card").first).to_be_visible()
    expect(page.locator("#query")).to_have_value(term)
    page.get_by_role("navigation", name="Основная навигация").get_by_role("link", name="Карта тем").click()
    map_data = page.request.get(f"{base_url}/api/explore").json()
    assert map_data["edges"], "Demo corpus must offer at least one evidence-based relation"
    source_id = map_data["edges"][0]["source"]
    page.locator("#map-book").select_option(source_id)
    expect(page.locator("#map-evidence .map-relation").first).to_be_visible()
    page.screenshot(path=str(artifacts / "desktop-map.png"), full_page=True)
    page.locator("#map-evidence .map-shared-terms .map-term-link").first.click()
    expect(page.locator("#fragments-panel .passage-card").first).to_be_visible()

    if semantic:
        page.locator("#language-filter").select_option("")
        page.locator("#book-filter").select_option("")
        page.locator("#query").fill("девочка следует за белым кроликом")
        page.locator('[data-mode="hybrid"]').click()
        first_crosslingual = page.locator("#fragments-panel .passage-card").first
        expect(first_crosslingual.locator(".passage-book")).to_contain_text("Alice")
        crosslingual_quote = first_crosslingual.locator("blockquote").inner_text()
        first_crosslingual.get_by_role("button", name="Открыть в контексте").click()
        expect(page.locator("#found-passage")).to_have_text(crosslingual_quote)
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(artifacts / "crosslingual-reader.png"), full_page=True)
        page.get_by_role("navigation", name="Основная навигация").get_by_role("link", name="Поиск", exact=True).click()

    # Use a narrow viewport for both landing-page and interactive search checks.
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("navigation", name="Основная навигация").get_by_role("link", name="Поиск", exact=True).click()
    page.goto(base_url, wait_until="networkidle")
    expect(page.locator("#featured-books .book-card").first).to_be_visible()
    page.screenshot(path=str(artifacts / "mobile-home.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "Mobile page overflows horizontally"
    )
    page.locator("#query").fill("rabbit")
    page.locator("#search-form").get_by_role("button", name="Найти").click()
    expect(page.locator("#fragments-panel .passage-card").first).to_be_visible()
    page.screenshot(path=str(artifacts / "mobile-search.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "Mobile results overflow horizontally"
    )
    page.locator("#fragments-panel .passage-card").first.get_by_role("button", name="Открыть в контексте").click()
    expect(page.locator("#found-passage")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "Mobile reader overflows horizontally"
    )
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(artifacts / "mobile-reader.png"), full_page=True)

    page.get_by_role("navigation", name="Основная навигация").get_by_role("link", name="Карта тем").click()
    expect(page.locator("#theme-cloud .theme-chip").first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile map overflows"
    page.screenshot(path=str(artifacts / "mobile-map.png"), full_page=True)

    # Exercise optional contributor fields without inventing attribution in real corpus data.
    fixture_hit = page.request.get(f"{base_url}/api/search?q=rabbit&mode=lexical&limit=1").json()["results"][0]
    fixture_hit["section_author"] = "UI attribution fixture"
    fixture_hit["section_role"] = "editorial"
    fixture_book = page.request.get(f"{base_url}/api/books/{fixture_hit['book_id']}").json()
    for section in fixture_book["sections"]:
        if section["id"] == fixture_hit["section_id"]:
            section["section_author"] = "UI attribution fixture"
            section["section_role"] = "editorial"
    page.route(
        "**/api/search?**",
        lambda route: route.fulfill(
            json={"results": [fixture_hit], "mode": "lexical", "warnings": [], "elapsed_ms": 0}
        ),
    )
    page.route(f"**/api/books/{fixture_hit['book_id']}", lambda route: route.fulfill(json=fixture_book))
    page.get_by_role("navigation", name="Основная навигация").get_by_role("link", name="Поиск", exact=True).click()
    page.locator("#query").fill("rabbit")
    page.locator("#search-form").get_by_role("button", name="Найти").click()
    expect(page.locator("#fragments-panel .contributor-label")).to_contain_text("Автор раздела: UI attribution fixture")
    expect(page.locator("#fragments-panel .contributor-label")).to_contain_text("Редакционный раздел")
    expect(page.locator("#fragments-panel .passage-citation")).to_have_text(fixture_hit["citation"])
    page.locator("#fragments-panel .passage-card").get_by_role("button", name="Открыть в контексте").click()
    expect(page.locator("#reader-contributor")).to_contain_text("Автор раздела: UI attribution fixture")
    expect(page.locator("#reader-contributor")).to_contain_text("Редакционный раздел")
    page.unroute("**/api/search?**")
    page.unroute(f"**/api/books/{fixture_hit['book_id']}")

    # Remove only the fixtures created by this run; leave real user notes untouched.
    saved_response = page.request.get(f"{base_url}/api/saved")
    for item in saved_response.json().get("items", []):
        if item.get("title") in {"Browser smoke: exact citation", "Browser smoke: saved query"}:
            page.request.delete(f"{base_url}/api/saved/{item['id']}")
    assert not browser_errors, f"Browser errors: {browser_errors}"
    return {
        "result": "passed",
        "workflows": [
            "search",
            "exact_citation_reader",
            "save_fragment_collection_note",
            "saved_after_reload",
            "save_query",
            "collection_markdown_export_exact_quote_source",
            "research_evidence",
            "no_answer",
            "language_filter",
            "mobile_search_reader",
            "theme_to_verified_passage",
            "related_book_to_shared_word",
            "mobile_map",
            "edition_and_provisional_work_group_counts",
            "optional_contributor_display_contract",
        ],
        "semantic_crosslanguage": "passed" if semantic else "not requested",
        "screenshots": len(list(artifacts.glob("*.png"))),
        "browser_errors": browser_errors,
    }


def main() -> None:
    """Launch Chromium and execute browser acceptance workflows."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/browser"))
    parser.add_argument("--semantic", action="store_true", help="Require the local multilingual model and index")
    args = parser.parse_args()
    expect.set_options(timeout=30000)
    args.artifacts.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        result = check_workflows(page, args.base_url.rstrip("/"), args.artifacts, semantic=args.semantic)
        browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
