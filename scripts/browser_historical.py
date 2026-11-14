"""Verify historical saved-edition reading in a real isolated API and Chromium.

The original CC0 two-chapter fixture, annotations and fault cases stay inside an
owned temporary library. No live server, corpus or user annotation is changed.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from browser_archive import check_download, fixture_server
from playwright.sync_api import Page, expect, sync_playwright

from book_rag.canonical_versions import read_canonical_manifest
from book_rag.library import Library

BOOK_ID = "historical-browser-cc0"
OLD_TITLE = "Сохранённая книга CC0"
NOTE_TITLE = "Historical browser: original quotation"
NOTE = "Заметка исследователя: сохранить первоначальный контекст."


def original_bytes() -> bytes:
    preface = "🦉 Сначала читатель знакомится с обстоятельствами истории. " * 42
    return (
        '<html lang="ru"><head><meta charset="utf-8"><title>Оригинальный тест CC0</title></head><body>'
        "<h1>Первая глава</h1><p>Начало старой версии. 🦉 Этот оригинальный текст передан в CC0.</p>"
        f"<h2>Вторая глава</h2><p>{preface}</p>"
        "<p>Память янтарной закладки хранит первоначальную мысль. Читатель вновь открывает старое издание.</p>"
        "<p>Последняя строка второй главы сохраняет завершение рассказа.</p></body></html>"
    ).encode()


def metadata() -> dict[str, Any]:
    return {
        "id": BOOK_ID,
        "title": OLD_TITLE,
        "author": "Автор оригинального теста CC0",
        "language": "ru",
        "edition": "Первое издание CC0",
        "source_url": "https://example.org/historical-original",
        "rights": "CC0 1.0",
        "rights_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "section_attributions": [
            {
                "title_contains": "Вторая глава",
                "author": "Редактор теста CC0",
                "role": "editorial",
                "translator": "Переводчик теста CC0",
            }
        ],
    }


def assert_history(page: Page, quote: str, item_id: int, chapter: int) -> None:
    expect(page.locator("#found-passage")).to_have_text(quote)
    expect(page.locator("#reader-historical-label")).to_contain_text("Сохранённое издание · сохранено")
    expect(page.locator("#reader-book-meta")).to_contain_text("Первое издание CC0")
    expect(page.locator("#reader-citation").get_by_role("button", name="Сохранить фрагмент")).to_have_count(0)
    expect(page.locator("#reader-citation").get_by_role("button", name="Копировать цитату")).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["saved"] == [str(item_id)] and params.get("chapter", [str(chapter)]) == [str(chapter)]
    assert "book" not in params and "passage" not in params


def assert_empty_error(page: Page, message: str) -> None:
    expect(page.locator("#reader-text .notice")).to_contain_text(message)
    expect(page.locator("#found-passage")).to_have_count(0)
    expect(page.locator("#reader-citation")).to_be_hidden()
    expect(page.locator("#reader-book-meta")).to_be_empty()
    expect(page.locator("#reader-sidebar-title")).to_be_empty()
    expect(page.locator("#reader-source")).to_be_empty()
    expect(page.locator("#reader-contributor")).to_be_empty()
    expect(page.locator("#reader-toc button")).to_have_count(0)


def run_fixture(directory: Path, artifacts: Path) -> dict[str, Any]:
    library = Library(directory / "library")
    source = directory / "original.html"
    original = original_bytes()
    source.write_bytes(original)
    library.import_book(source, metadata())
    errors: list[str] = []
    context_bodies: list[dict[str, Any]] = []
    historical_requests: list[str] = []
    with fixture_server(library.data_dir) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        context.on("page", lambda opened_page: opened_page.on("pageerror", lambda error: errors.append(str(error))))
        page = context.new_page()
        page.goto(base, wait_until="networkidle")
        page.locator('[data-mode="lexical"]').click()
        page.locator("#query").fill('"Память янтарной закладки"')
        page.locator("#search-form").get_by_role("button", name="Найти").click()
        hit = page.locator("#fragments-panel .passage-card").first
        expect(hit).to_be_visible()
        quote = hit.locator("blockquote").inner_text()
        evidence = library.search('"Память янтарной закладки"')["results"][0]
        original_book = library.get_book(BOOK_ID)
        assert original_book is not None
        assert evidence["start"] > 0 and "🦉" in original_book["sections"][1]["text"][: evidence["start"]]
        hit.get_by_role("button", name="Открыть в контексте").click()
        page.locator("#reader-citation").get_by_role("button", name="Сохранить фрагмент").click()
        page.locator("#save-title").fill(NOTE_TITLE)
        page.locator("#save-collection").fill("Historical CC0 context")
        page.locator("#save-note").fill(NOTE)
        page.locator("#save-form").get_by_role("button", name="Сохранить", exact=True).click()
        expect(page.locator("#save-dialog")).not_to_be_visible()
        saved = library.saved()[0]
        item_id = saved["id"]
        first_hit = library.search('"Начало старой версии"')["results"][0]
        other = library.save({"kind": "passage", "passage_id": first_hit["id"], "title": "Другая глава CC0"})
        assert other["provenance"]["canonical_version_id"] == saved["provenance"]["canonical_version_id"]
        legacy = library.save(
            {
                "kind": "passage",
                "passage_id": first_hit["id"],
                "title": "Legacy context CC0",
                "note": "Legacy quotation and note stay readable.",
            }
        )
        # Seed the legacy/failure metadata shape in this owned fixture only.
        legacy_metadata = legacy["provenance"]
        legacy_metadata.pop("canonical_version_id")
        legacy_metadata["historical_context_error"] = "Controlled missing historical context"
        with library.connect() as connection:
            connection.execute(
                "UPDATE saved_provenance SET metadata=? WHERE item_id=?", (json.dumps(legacy_metadata), legacy["id"])
            )

        # Replace only our own input; current reader and frozen reader must diverge.
        source.write_text("<h1>Новая глава</h1><p>Лазурная закладка принадлежит новой версии CC0.</p>")
        library.import_book(
            source,
            metadata()
            | {
                "title": "Новая редакция CC0",
                "edition": "Второе издание CC0",
                "source_url": "https://example.org/historical-new",
            },
        )
        page.goto(f"{base}/?book={BOOK_ID}", wait_until="networkidle")
        expect(page.locator("#reader-text")).to_contain_text("Лазурная закладка")
        expect(page.locator("#reader-historical-label")).to_be_hidden()
        expect(page.locator("#reader-book-meta")).to_contain_text("Второе издание CC0")
        page.goto(f"{base}/?view=saved", wait_until="networkidle")
        legacy_card = page.locator(".saved-item").filter(
            has=page.get_by_role("heading", name="Legacy context CC0", exact=True)
        )
        expect(legacy_card.locator(".historical-reader-link")).to_have_count(0)
        expect(legacy_card.locator("blockquote")).to_have_text(first_hit["text"])
        expect(legacy_card.locator(".saved-note")).to_have_text("Legacy quotation and note stay readable.")
        expect(legacy_card.locator(".saved-context-note")).to_contain_text("Контекст издания сохранить не удалось")
        expect(legacy_card.locator(".artifact-download")).to_be_visible()
        card = page.locator(".saved-item").filter(has=page.get_by_role("heading", name=NOTE_TITLE, exact=True))
        expect(card.locator(".saved-note")).to_have_text(NOTE)
        link = card.get_by_role("link", name="Читать сохранённое издание", exact=True)
        expect(link).to_have_attribute("href", f"/?saved={item_id}")
        page.on("request", lambda request: historical_requests.append(urlsplit(request.url).path))
        with page.expect_response(
            lambda response: urlsplit(response.url).path == f"/api/saved/{item_id}/context"
        ) as opened:
            link.click()
        body = opened.value.json()
        context_bodies.append(body)
        assert body["section"]["text"][evidence["start"] : evidence["end"]] == quote
        assert_history(page, quote, item_id, 1)
        expect(page.locator("#reader-contributor")).to_contain_text("Редактор теста CC0")
        expect(page.locator("#reader-contributor")).to_contain_text("Переводчик теста CC0")
        page.locator(".source-details > summary").click()
        expect(page.locator("#reader-source")).to_contain_text("CC0 1.0")
        expect(page.locator("#reader-source").get_by_role("link", name="Открыть первоисточник")).to_have_attribute(
            "href", "https://example.org/historical-original"
        )
        check_download(page, page.locator("#reader-source .artifact-download"), original)
        page.screenshot(path=str(artifacts / "historical-quote-source.png"))
        page.locator("#prev-chapter").click()
        expect(page.locator("#reader-section-title")).to_have_text("Первая глава")
        expect(page.locator("#found-passage")).to_have_count(0)
        expect(page.locator("#reader-citation")).to_be_hidden()
        expect(page.locator("#prev-chapter")).to_be_disabled()
        page.locator("#next-chapter").click()
        assert_history(page, quote, item_id, 1)
        page.go_back(wait_until="networkidle")
        expect(page.locator("#reader-section-title")).to_have_text("Первая глава")
        page.go_forward(wait_until="networkidle")
        assert_history(page, quote, item_id, 1)
        page.reload(wait_until="networkidle")
        assert_history(page, quote, item_id, 1)
        page.locator("#reader-toc").get_by_role("button", name="Первая глава", exact=True).click()
        expect(page.locator("#reader-section-title")).to_have_text("Первая глава")
        with page.expect_response(lambda response: "ordinal=1" in response.url) as selected:
            page.locator("#reader-toc").get_by_role("button", name="Вторая глава", exact=True).click()
        context_bodies.append(selected.value.json())
        assert_history(page, quote, item_id, 1)
        assert all(sum("text" in section for section in body["book"]["sections"]) == 1 for body in context_bodies)
        assert not any(
            path.startswith(f"/api/books/{BOOK_ID}") or path.startswith("/api/passages/")
            for path in historical_requests
        )

        page.locator("#reader-back").click()
        expect(card).to_be_visible()
        with context.expect_page() as new_tab:
            link.click(button="middle")
        popup = new_tab.value
        popup.wait_for_load_state("networkidle")
        assert_history(popup, quote, item_id, 1)
        popup.close()
        page.goto(f"{base}/?saved={other['id']}", wait_until="networkidle")
        assert_history(page, first_hit["text"], other["id"], 0)
        page.locator("#next-chapter").click()
        expect(page.locator("#reader-citation")).to_be_hidden()
        page.locator("#prev-chapter").click()
        assert_history(page, first_hit["text"], other["id"], 0)

        # Deleting the current catalog entry must not remove saved context.
        with library.connect() as connection:
            library._remove_index(connection, BOOK_ID)
            connection.execute("DELETE FROM books WHERE id=?", (BOOK_ID,))
        page.goto(f"{base}/?saved={item_id}&chapter=1", wait_until="networkidle")
        assert_history(page, quote, item_id, 1)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator("#reader-historical-label")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Historical mobile reader overflows"
        page.locator("#reader-historical-label").scroll_into_view_if_needed()
        page.screenshot(path=str(artifacts / "historical-mobile.png"))

        # Actual network latency, without mocking a response: leaving a pending
        # reader must prevent its late response from replacing the library route.
        network = context.new_cdp_session(page)
        network.send("Network.enable")
        network.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 250, "downloadThroughput": -1, "uploadThroughput": -1},
        )
        page.locator("#prev-chapter").click()
        page.get_by_role("navigation", name="Основная навигация").get_by_role("link", name="Библиотека").click()
        page.wait_for_load_state("networkidle")
        expect(page.locator("#library-view")).to_be_visible()
        assert parse_qs(urlsplit(page.url).query).get("view") == ["library"]
        network.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1},
        )
        network.detach()

        # Corrupt only our owned canonical chapter: the actual endpoint returns409.
        manifest = read_canonical_manifest(library.data_dir, saved["provenance"]["canonical_version_id"])
        digest = manifest["sections"][1]["sha256"]
        (library.data_dir / "canonical" / "sections" / digest[:2] / f"{digest}.json.gz").write_bytes(b"corrupt fixture")
        page.goto(f"{base}/?saved={item_id}", wait_until="networkidle")
        assert_empty_error(page, "не прошёл проверку целостности")
        page.screenshot(path=str(artifacts / "historical-integrity-error.png"))
        page.goto(f"{base}/?saved={item_id}&chapter=999", wait_until="networkidle")
        assert_empty_error(page, "глава недоступна")
        page.goto(f"{base}/?saved=999999", wait_until="networkidle")
        assert_empty_error(page, "фрагмент не найден")
        page.goto(f"{base}/?saved={item_id}&chapter=NaN", wait_until="networkidle")
        assert_empty_error(page, "Некорректный номер")
        page.get_by_role("button", name="К сохранённым фрагментам", exact=True).click()
        expect(card.locator("blockquote")).to_have_text(quote)
        expect(card.locator(".saved-note")).to_have_text(NOTE)
        assert not errors, errors
        context.close()
        browser.close()
    return {
        "result": "passed",
        "fixture": "original CC0; isolated real API and Chromium; no API mocks",
        "workflows": [
            "save_reindex_historical_exact_unicode_quote",
            "frozen_contributor_translator_source_download",
            "lazy_chapters_prev_next_toc",
            "two_saved_chapter_defaults",
            "back_forward_reload_native_new_tab",
            "current_book_removed_history_retained",
            "mobile_historical_reader",
            "late_response_cancelled",
            "corrupt_missing_invalid_context_clears_reader",
            "notes_and_excerpt_retained",
            "legacy_context_missing_preserves_quote_note_and_original",
        ],
        "browser_errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/historical-browser"))
    arguments = parser.parse_args()
    arguments.artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="book-atlas-historical-browser-") as temporary:
        report = run_fixture(Path(temporary), arguments.artifacts)
    (arguments.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
