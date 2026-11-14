"""Verify five actual Russian works in the running full Book Atlas library.

No API responses are mocked and no source or annotation data is changed.
Screenshots are viewport/element captures, never entire long chapters.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import Page, expect, sync_playwright

WORKS = [
    ("underground", "Записки из подполья", "Я человек больной"),
    ("fathers-and-sons", "Отцы и дети", "Посвящается памяти"),
    ("ward-six", "Палата № 6", "В больничном дворе стоит небольшой флигель"),
    ("ivan-ilyich", "Смерть Ивана Ильича", "Прошедшая история жизни Ивана Ильича"),
    ("overcoat", "Шинель", "В департаменте"),
]


def get_work(page: Page, base: str, slug: str, title: str) -> dict[str, Any]:
    matches = page.request.get(f"{base}/api/books", params={"q": title, "status": "indexed", "limit": 50}).json()
    book = next((item for item in matches["books"] if item["id"].startswith(f"wsru-{slug}-")), None)
    assert book, f"Import the Russian pilot before this check: {title}"
    return page.request.get(f"{base}/api/books/{book['id']}").json()


def check_work(
    page: Page, base: str, slug: str, title: str, phrase: str, artifacts: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    book = get_work(page, base, slug, title)
    metadata = book["source_metadata"]
    page.get_by_role("navigation", name="Основная навигация").get_by_role("link", name="Библиотека").click()
    page.locator("#library-query").fill(title)
    card = page.locator("#library-books .book-card").filter(has=page.get_by_role("heading", name=title, exact=True))
    expect(card).to_be_visible()
    author_query = book["author"].split()[-1]
    page.locator("#library-query").fill(author_query)
    expect(card).to_be_visible()
    page.get_by_role("navigation", name="Основная навигация").get_by_role("link", name="Поиск", exact=True).click()
    page.locator('[data-mode="lexical"]').click()
    query = '"' + phrase + '"'
    page.locator("#query").fill(query)
    page.locator("#search-form").get_by_role("button", name="Найти").click()
    expect(page.locator("#search-results")).to_be_visible()
    page.locator("#language-filter").select_option("ru")
    page.locator("#book-scope-query").fill(author_query)
    expect(page.locator(f'#book-filter option[value="{book["id"]}"]')).to_have_count(1)
    with page.expect_response(
        lambda response: (
            urlsplit(response.url).path == "/api/search"
            and parse_qs(urlsplit(response.url).query).get("book_id") == [book["id"]]
        )
    ) as response:
        page.locator("#book-filter").select_option(book["id"])
    hit = response.value.json()["results"][0]
    first = page.locator("#fragments-panel .passage-card").first
    expect(first.locator(".passage-book")).to_contain_text(title)
    expect(first.locator("blockquote")).to_have_text(hit["text"])
    section = next(item for item in book["sections"] if item["id"] == hit["section_id"])
    assert hit["text"] == section["text"][hit["start"] : hit["end"]]
    assert hit["verified"] and all(book[field] in hit["citation"] for field in ("title", "author", "edition"))
    first.get_by_role("button", name="Открыть в контексте").click()
    expect(page.locator("#found-passage")).to_have_text(hit["text"])
    expect(page.locator("#reader-coverage")).to_have_text(
        "Оригинальное произведение · без позднего редакционного аппарата"
    )
    page.locator(".source-details > summary").click()
    expect(page.locator("#reader-source")).to_contain_text("CC BY-SA 4.0")
    expect(page.locator("#reader-source")).to_contain_text(book["edition"])
    expect(page.locator("#reader-source .source-revision-bundle")).to_contain_text(
        metadata["source_bundle_sha256"][:16]
    )
    expect(page.locator("#reader-source").get_by_role("link", name="Открыть первоисточник")).to_have_attribute(
        "href", book["source_url"]
    )
    expect(
        page.locator("#reader-source").get_by_role("link", name="Условия использования источника")
    ).to_have_attribute("href", book["rights_url"])
    page.locator(".source-revisions > summary").click()
    revisions = page.locator(".source-revisions li a")
    assert revisions.count() == len(metadata["chapter_sources"])
    assert all("oldid=" in str(revisions.nth(index).get_attribute("href")) for index in range(revisions.count()))
    expect(page.locator("#reader-source .source-variant-note")).to_have_count(0)
    if slug == "underground":
        page.locator(".source-details").screenshot(path=str(artifacts / "russian-source-revisions.png"))
    page.locator(".source-revisions > summary").click()
    page.locator(".source-details > summary").click()

    # Navigate real neighboring sections, then recover the original highlighted hit.
    index = next(index for index, item in enumerate(book["sections"]) if item["id"] == section["id"])
    if index < len(book["sections"]) - 1:
        page.locator("#next-chapter").click()
        expect(page.locator("#reader-section-title")).to_have_text(book["sections"][index + 1]["title"])
        page.locator("#prev-chapter").click()
    else:
        page.locator("#prev-chapter").click()
        expect(page.locator("#reader-section-title")).to_have_text(book["sections"][index - 1]["title"])
        page.locator("#next-chapter").click()
    expect(page.locator("#found-passage")).to_have_text(hit["text"])
    if slug == "fathers-and-sons":
        expect(page.locator("#reader-text")).to_contain_text("Посвящается памяти Виссариона Григорьевича Белинского")
        page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(artifacts / "russian-dedication.png"))
    if slug == "underground":
        epigraph = next(item for item in book["sections"] if "Из поэзии Н. А. Некрасова" in item["text"])
        page.locator("#reader-toc").get_by_role("button", name=epigraph["title"], exact=True).click()
        expect(page.locator("#reader-text")).to_contain_text("Когда из мрака заблужденья\n\nГорячим словом убеждения")
        expect(page.locator("#reader-text")).to_contain_text("Из поэзии Н. А. Некрасова")
        page.set_viewport_size({"width": 1440, "height": 1900})
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(artifacts / "russian-nekrasov-epigraph.png"))
        page.set_viewport_size({"width": 1440, "height": 1000})
    return book, {
        "book_id": book["id"],
        "title": title,
        "title_author_lookup": True,
        "exact_quote_reader": True,
        "chapter_navigation": True,
        "pinned_source_revisions": revisions.count(),
        "original_work_only_coverage": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/russian-browser"))
    parser.add_argument(
        "--illustration-book", help="Also verify a real indexed source variant explicitly lacking illustrations"
    )
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    base = args.base_url.rstrip("/")
    expect.set_options(timeout=30000)
    errors: list[str] = []
    reports, books = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base, wait_until="networkidle")
        for slug, title, phrase in WORKS:
            book, report = check_work(page, base, slug, title, phrase, args.artifacts)
            books.append(book)
            reports.append(report)
        long_book, long_section = max(
            ((book, section) for book in books for section in book["sections"]), key=lambda pair: len(pair[1]["text"])
        )
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(
            base + "/?" + urlencode({"book": long_book["id"], "section": long_section["id"]}), wait_until="networkidle"
        )
        expect(page.locator("#reader-text")).to_have_text(long_section["text"])
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
            "Russian mobile page overflows"
        )
        assert page.locator("#reader-text").evaluate("el => el.scrollWidth <= el.clientWidth"), "Russian text overflows"
        page.screenshot(path=str(args.artifacts / "russian-mobile-long-chapter.png"))
        illustration_check = None
        if args.illustration_book:
            variant = page.request.get(f"{base}/api/books/{args.illustration_book}").json()
            assert variant["status"] == "indexed", "The source-variant recovery must finish before this check"
            assert variant["source_metadata"].get("illustrations_included") is False
            page.goto(base + "/?" + urlencode({"book": variant["id"]}), wait_until="networkidle")
            expect(page.locator("#reader-text")).not_to_be_empty()
            page.locator(".source-details > summary").click()
            expect(page.locator("#reader-source .source-variant-note")).to_have_text(
                "В этой копии нет иллюстраций; оригинал доступен по ссылке источника"
            )
            page.locator(".source-details").screenshot(path=str(args.artifacts / "actual-illustration-variant.png"))
            illustration_check = {"book_id": variant["id"], "actual_metadata_flag": True, "notice_visible": True}
        assert not errors, errors
        browser.close()
    report = {
        "result": "passed",
        "works": reports,
        "epigraph_nekrasov_visible": True,
        "turgenev_dedication_visible": True,
        "mobile_long_chapter": {
            "book_id": long_book["id"],
            "section": long_section["title"],
            "characters": len(long_section["text"]),
            "no_horizontal_overflow": True,
        },
        "browser_errors": errors,
    }
    report["illustration_variant"] = illustration_check
    (args.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
