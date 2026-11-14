"""Check starter shelves against a real isolated catalogue API and Chromium.

The checked-in licensed demo supplies authentic RU/EN/FR/DE excerpts. An original
CC0 catalogue decoy and an unacquired metadata record test false matches. All
writes and the HTTP server belong to temporary directories; no live writes,
model loads, embeddings or API response mocks are used.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from browser_archive import fixture_server
from playwright.sync_api import Page, Request, expect, sync_playwright

from book_rag.library import Library

ROOT = Path(__file__).resolve().parents[1]
POEM = "Красавице, которая нюхала табак"
DECOY = "Alice in Wonderland · original CC0 catalogue decoy"


def seed(directory: Path) -> Library:
    """Import the authentic licensed demo and explicitly separate synthetic records."""
    library = Library(directory / "library")
    manifest = ROOT / "corpus" / "demo" / "manifest.json"
    for entry in json.loads(manifest.read_text(encoding="utf-8")):
        result = library.import_book(manifest.parent / entry["local_path"], entry)
        assert result["status"] == "indexed", result
    decoy = directory / "cc0-catalogue-decoy.txt"
    decoy.write_text(
        "This is an original CC0 Book Atlas catalogue fixture, not a work by Lewis Carroll. "
        "The synthetic title tests that a title match with the wrong author stays off the starter shelf.",
        encoding="utf-8",
    )
    library.import_book(
        decoy,
        {
            "id": "shelf-decoy",
            "title": DECOY,
            "author": "Book Atlas CC0 browser fixture",
            "language": "en",
            "rights": "CC0 1.0 · original catalogue test text",
            "edition": "Synthetic catalogue record; no historical authorship claimed",
        },
    )
    library.register(
        {
            "id": "shelf-unacquired",
            "title": "Записки из подполья",
            "author": "Фёдор Михайлович Достоевский",
            "language": "ru",
            "edition": "Unacquired metadata-only browser fixture",
        }
    )
    return library


def no_overflow(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile page overflows"


def assert_cards(page: Page, library: Library, language: str, count: int) -> list[str]:
    cards = page.locator("#starter-books .starter-card")
    expect(cards).to_have_count(count)
    ids = cards.evaluate_all("cards => cards.map(card => card.dataset.bookId)")
    assert len(ids) == len(set(ids))
    for identity in ids:
        book = library.get_book(identity)
        assert book is not None and book["status"] == "indexed" and book["language"] == language
        card = page.locator(f'#starter-books [data-book-id="{identity}"]')
        expect(card.locator("h3")).to_have_text(book["title"])
        expect(card.locator(".book-author")).to_have_text(book["author"])
        expect(card.locator(".starter-edition")).to_have_text(book["edition"])
        expect(card.get_by_role("link", name=f"Читать: {book['title']}", exact=True)).to_have_attribute(
            "href", f"/?book={identity}"
        )
    return ids


def run_fixture(directory: Path, artifacts: Path) -> dict[str, Any]:
    library = seed(directory)
    errors: list[str] = []
    requests: list[tuple[str, dict[str, list[str]]]] = []

    def record(request: Request) -> None:
        path = urlsplit(request.url).path
        if path.startswith("/api/"):
            assert request.method == "GET", "This UI verification makes no annotation writes"
            requests.append((path, parse_qs(urlsplit(request.url).query)))

    def lookups() -> Counter[str]:
        return Counter(
            params["language"][0]
            for path, params in requests
            if path == "/api/books" and params.get("q") and params.get("limit") == ["8"]
        )

    with fixture_server(library.data_dir) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", record)
        page.goto(base, wait_until="networkidle")
        expect(page.locator("#starter-shelf")).to_be_visible()
        expect(page.get_by_role("button", name="На русском", exact=True)).to_have_attribute("aria-pressed", "true")
        ru = assert_cards(page, library, "ru", 1)
        assert ru == ["pg5316-pushkin"]
        expect(page.locator("#starter-context")).to_contain_text("Русские произведения на русском языке")
        expect(page.locator("#starter-books")).not_to_contain_text("Записки из подполья")
        assert lookups() == {"ru": 6}, "Other languages must remain unloaded until selected"
        assert not any(path == "/api/search" for path, _ in requests), "Opening home must never load embeddings"
        page.locator("#starter-shelf").scroll_into_view_if_needed()
        page.screenshot(path=str(artifacts / "russian-shelf.png"))

        # Native links preserve new-tab/deep-link behavior and exact edition text.
        poem_link = page.locator("#starter-books").get_by_role("link", name=f"Читать: {POEM}", exact=True)
        href = poem_link.get_attribute("href")
        assert href
        reader = context.new_page()
        reader.on("pageerror", lambda error: errors.append(str(error)))
        reader.goto(base + href, wait_until="networkidle")
        expect(reader.locator("#reader-sidebar-title")).to_have_text(POEM)
        source = library.get_book("pg5316-pushkin")
        assert source is not None
        expect(reader.locator("#reader-text")).to_have_text(source["sections"][0]["text"])
        reader.close()
        poem_link.click()
        expect(page.locator("#reader-view")).to_be_visible()
        expect(page.locator("#reader-text")).to_contain_text("Ах, отчего я не табак!")
        page.locator("#reader-back").click()
        expect(page.locator("#starter-shelf")).to_be_visible()
        assert lookups() == {"ru": 6}, "Reader return should reuse a language shelf"

        page.get_by_role("button", name="English", exact=True).click()
        expect(page.locator("#starter-books")).to_have_attribute("aria-busy", "false")
        en = assert_cards(page, library, "en", 5)
        assert "shelf-decoy" not in en
        expect(page.locator("#starter-books")).not_to_contain_text(DECOY)
        expect(page.locator("#starter-books .book-excerpt-label")).to_have_count(5)
        expect(page.locator("#starter-context")).to_contain_text("включая переводы")
        assert lookups() == {"ru": 6, "en": 6}
        assert not any(path == "/api/search" for path, _ in requests)
        page.locator("#starter-shelf").scroll_into_view_if_needed()
        page.screenshot(path=str(artifacts / "english-shelf.png"))

        # A scope action clears stale query/results and waits for explicit submit.
        darwin = page.locator('#starter-books [data-book-id="pg1228-origin"]')
        darwin.get_by_role("button", name="Искать в книге: On the Origin of Species", exact=True).click()
        expect(page.locator("#query")).to_be_focused()
        expect(page.locator("#query")).to_have_value("")
        expect(page.locator("#book-filter")).to_have_value("pg1228-origin")
        expect(page.locator("#language-filter")).to_have_value("en")
        expect(page.locator("#book-scope-selection")).to_contain_text("Charles Darwin")
        expect(page.locator("#fragments-panel")).to_contain_text("Что найти в этой книге?")
        assert not any(path == "/api/search" for path, _ in requests)
        page.locator('[data-mode="lexical"]').click()
        page.locator("#query").fill("struggle for existence")
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/search") as response:
            page.locator("#search-form").get_by_role("button", name="Найти", exact=True).click()
        data = response.value.json()
        assert response.value.ok and data["results"]
        assert all(hit["book_id"] == "pg1228-origin" and hit["verified"] for hit in data["results"])
        assert response.value.request.url.endswith("language=en&book_id=pg1228-origin")
        first = page.locator("#fragments-panel .passage-card").first
        quote = first.locator("blockquote").inner_text()
        first.get_by_role("button", name="Открыть в контексте").click()
        expect(page.locator("#found-passage")).to_have_text(quote)
        expect(page.locator("#reader-sidebar-title")).to_have_text("On the Origin of Species")

        # A fresh home page defaults to RU; other language shelves are demand-loaded.
        page.goto(base, wait_until="networkidle")
        assert_cards(page, library, "ru", 1)
        page.get_by_role("button", name="Français", exact=True).click()
        expect(page.locator("#starter-books")).to_have_attribute("aria-busy", "false")
        fr = assert_cards(page, library, "fr", 1)
        assert fr == ["pg4650-candide"]
        page.get_by_role("button", name="Deutsch", exact=True).click()
        expect(page.locator("#starter-books")).to_have_attribute("aria-busy", "false")
        de = assert_cards(page, library, "de", 1)
        assert de == ["pg22367-metamorphosis"]
        previous = lookups()
        page.get_by_role("button", name="Français", exact=True).click()
        assert_cards(page, library, "fr", 1)
        assert lookups() == previous, "Repeated language selection must not repeat catalogue lookups"
        catalog_href = page.locator("#starter-catalog").get_attribute("href")
        assert catalog_href == "/?view=library&language=fr"
        page.locator("#starter-catalog").click()
        expect(page.locator("#library-language")).to_have_value("fr")
        expect(page.locator("#library-status")).to_have_value("indexed")
        expect(page.locator("#library-query")).to_have_value("")
        expect(page.locator("#library-books .book-card")).to_have_count(1)
        expect(page.locator("#library-books")).to_contain_text("Candide")
        page.reload(wait_until="networkidle")
        expect(page.locator("#library-language")).to_have_value("fr")
        expect(page.locator("#library-books .book-card")).to_have_count(1)

        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(base, wait_until="networkidle")
        russian = page.get_by_role("button", name="На русском", exact=True)
        russian.focus()
        page.keyboard.press("Tab")
        expect(page.get_by_role("button", name="English", exact=True)).to_be_focused()
        page.keyboard.press("Enter")
        assert_cards(page, library, "en", 5)
        page.locator("#starter-shelf").scroll_into_view_if_needed()
        no_overflow(page)
        page.screenshot(path=str(artifacts / "mobile-shelf.png"), full_page=True)
        assert all(
            params.get("status") == ["indexed"] and params.get("offset") == ["0"]
            for path, params in requests
            if path == "/api/books" and params.get("limit") == ["8"]
        )
        assert all(params.get("mode") == ["lexical"] for path, params in requests if path == "/api/search")
        assert not errors, errors
        context.close()
        browser.close()
    return {
        "result": "passed",
        "fixture": "authentic checked-in licensed demo + explicit CC0 decoy; real API/Chromium; no response mocks",
        "editions_by_language": {"ru": ru, "en": en, "fr": fr, "de": de},
        "initial_catalogue_requests": {"landing": 1, "selected_ru_shelf": 6, "other_shelves": 0},
        "workflows": [
            "real_searchable_language_title_author_matches_only",
            "missing_unacquired_and_wrong_author_not_promoted",
            "partial_coverage_and_translation_language_labels",
            "native_deep_link_and_exact_russian_reader",
            "scope_without_model_call_then_verified_lexical_quote_reader",
            "lazy_language_requests_bounded_and_cached",
            "all_language_catalogue_click_and_reload",
            "mobile_keyboard_focus_and_no_overflow",
        ],
        "browser_errors": errors,
    }


def run_empty_shelf(directory: Path, artifacts: Path) -> dict[str, Any]:
    """A language with other books must not be described as an empty catalogue."""
    directory.mkdir()
    library = Library(directory / "library")
    source = directory / "original.txt"
    source.write_text("An original CC0 fixture about keeping a small reading diary.", encoding="utf-8")
    library.import_book(
        source,
        {"id": "diary", "title": "Original reading diary CC0", "author": "Book Atlas fixture", "language": "en"},
    )
    errors: list[str] = []
    with fixture_server(library.data_dir) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base, wait_until="networkidle")
        expect(page.get_by_role("button", name="English", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(page.get_by_role("button", name="На русском", exact=True)).to_be_disabled()
        expect(page.locator("#starter-books .starter-card")).to_have_count(0)
        expect(page.locator("#starter-books")).to_contain_text("На этой полке пока нет знакомых названий")
        expect(page.locator("#starter-books")).to_contain_text("Все доступные книги на этом языке")
        page.locator("#starter-catalog").click()
        expect(page.locator("#library-books .book-card")).to_have_count(1)
        expect(page.locator("#library-books")).to_contain_text("Original reading diary CC0")
        no_overflow(page)
        assert not errors, errors
        browser.close()
    return {"result": "passed", "workflows": ["no_familiar_matches_preserves_language_catalogue"], "errors": errors}


def check_live(base: str, artifacts: Path) -> dict[str, Any]:
    """Inspect existing catalogue responses and reader text with GET requests only."""
    errors: list[str] = []
    methods: list[str] = []
    lookups: dict[str, list[dict[str, Any]]] = {}
    timings: dict[str, float] = {}
    shown: dict[str, list[dict[str, Any]]] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: methods.append(request.method))

        def capture(response: Any) -> None:
            if urlsplit(response.url).path != "/api/books":
                return
            params = parse_qs(urlsplit(response.url).query)
            if params.get("limit") == ["8"]:
                assert response.ok and params.get("status") == ["indexed"]
                language = params["language"][0]
                lookups.setdefault(language, []).extend(response.json()["books"])

        page.on("response", capture)
        started = time.perf_counter()
        page.goto(base, wait_until="networkidle")
        initial_language = page.locator('[data-shelf-language][aria-pressed="true"]').get_attribute(
            "data-shelf-language"
        )
        assert initial_language
        timings[initial_language] = round(time.perf_counter() - started, 3)
        for language in ("ru", "en", "fr", "de"):
            control = page.locator(f'[data-shelf-language="{language}"]')
            if control.is_disabled():
                continue
            started = time.perf_counter()
            control.click()
            expect(page.locator("#starter-books")).to_have_attribute("aria-busy", "false")
            timings.setdefault(language, round(time.perf_counter() - started, 3))
            books_by_id = {book["id"]: book for book in lookups[language]}
            ids = page.locator("#starter-books .starter-card").evaluate_all(
                "cards => cards.map(card => card.dataset.bookId)"
            )
            assert len(ids) <= 6 and len(ids) == len(set(ids))
            for identity in ids:
                book = books_by_id[identity]
                assert book["status"] == "indexed" and book["language"] == language
                card = page.locator(f'#starter-books [data-book-id="{identity}"]')
                expect(card.locator("h3")).to_have_text(book["title"])
                expect(card.locator(".book-author")).to_have_text(book["author"])
            shown[language] = [
                {key: books_by_id[identity][key] for key in ("id", "title", "author", "language")} for identity in ids
            ]
        assert all(method == "GET" for method in methods)
        assert not errors, errors
        page.locator('[data-shelf-language="ru"]').click()
        expect(page.locator("#starter-books")).to_have_attribute("aria-busy", "false")
        page.locator("#starter-shelf").scroll_into_view_if_needed()
        port = urlsplit(base).port
        page.screenshot(path=str(artifacts / f"live-{port}-shelf.png"))
        if page.locator("#starter-books .starter-card").count():
            card = page.locator("#starter-books .starter-card").first
            identity = card.get_attribute("data-book-id")
            book = page.request.get(f"{base}/api/books/{identity}").json()
            card.get_by_role("link", name="Читать:", exact=False).click()
            expect(page.locator("#reader-text")).to_have_text(book["sections"][0]["text"])
            expect(page.locator("#reader-sidebar-title")).to_have_text(book["title"])
        page.set_viewport_size({"width": 390, "height": 844})
        no_overflow(page)
        assert all(method == "GET" for method in methods)
        assert not errors, errors
        browser.close()
    return {"base_url": base, "result": "passed", "shelves": shown, "load_seconds": timings, "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/starter-shelf-browser"))
    parser.add_argument("--live-base-url", action="append", default=[])
    arguments = parser.parse_args()
    arguments.artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="book-atlas-starter-shelf-") as temporary:
        report = run_fixture(Path(temporary), arguments.artifacts)
        report["empty_shelf"] = run_empty_shelf(Path(temporary) / "empty", arguments.artifacts)
    report["live_get_only"] = [check_live(base, arguments.artifacts) for base in arguments.live_base_url]
    (arguments.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
