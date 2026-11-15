"""Check edition availability in a real isolated browser and optional read-only live servers.

Fixtures are original CC0 texts. All writes stay in the owned temporary library.
Live checks issue only GET requests and never alter notes, corpus or processes.
Run from the repository root: python scripts/browser_pipeline.py.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from browser_archive import fixture_server
from playwright.sync_api import Browser, Page, expect, sync_playwright

from book_rag.library import Library

ORIGINAL = "Amber bookmarks preserve this exact original CC0 fixture quotation."
REPLACEMENT = "Azure bookmarks identify the successfully refreshed original CC0 fixture edition."
TITLE = "Pipeline fixture · первое издание CC0"


def integer(text: str) -> int:
    return int(re.sub(r"\D", "", text))


def assert_counters(page: Page, stats: dict[str, Any]) -> None:
    details = page.locator(".library-state-details")
    if details.count() and not details.evaluate("element => element.open"):
        details.locator("summary").click()
    expect(page.get_by_role("heading", name="Доступность изданий", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Загрузка и обработка · последняя попытка")).to_be_visible()
    assert integer(page.locator('#pipeline-stats [data-status="indexed"] b').inner_text()) == stats.get(
        "searchable_books", stats["books"]
    )
    for status, count in stats.get("ingestion_status_counts", {}).items():
        assert integer(page.locator(f'#pipeline-stats [data-ingestion-status="{status}"] b').inner_text()) == count


def metadata(identity: str = "pipeline-current") -> dict[str, Any]:
    return {
        "id": identity,
        "title": TITLE,
        "author": "Book Atlas CC0 browser fixture",
        "language": "en",
        "edition": "Первая версия CC0",
        "rights": "CC0 1.0",
        "source_url": "https://example.org/pipeline-cc0",
        "work_id": "pipeline-curated-cc0-work",
        "work_identity_status": "curated",
        "coverage": "full",
    }


def check_fixture(browser: Browser, directory: Path, artifacts: Path) -> dict[str, Any]:
    library = Library(directory / "library")
    source = directory / "fixture.txt"
    source.write_text(ORIGINAL)
    library.import_book(source, metadata())
    translation = directory / "translation.txt"
    translation.write_text("Янтарная закладка сохраняет точную фразу оригинального теста CC0.")
    library.import_book(
        translation,
        metadata("pipeline-translation")
        | {
            "title": "Русская версия теста CC0",
            "language": "ru",
            "edition": "Русский перевод CC0",
            "translator": "Тестовый переводчик CC0",
        },
    )
    empty = directory / "new-attempt.txt"
    empty.write_text("")
    for identity in ("pipeline-current", "pipeline-unavailable"):
        try:
            library.import_book(empty, metadata(identity) | {"title": "Неопубликованный новый заголовок"})
        except ValueError:
            pass
        else:
            raise AssertionError("Empty real source must fail import")
    library.register(metadata("pipeline-pending") | {"title": "Ожидающая загрузки запись CC0"})
    errors: list[str] = []
    with fixture_server(library.data_dir) as base:
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{base}/?view=library", wait_until="networkidle")
        stats = page.request.get(f"{base}/api/stats").json()
        assert stats["books"] == 2 and stats["ingestion_status_counts"]["failed"] == 2
        assert_counters(page, stats)
        expect(page.locator("#library-scope")).to_contain_text("полных произведений — не менее 1")
        current = page.locator(".book-card").filter(has=page.get_by_role("heading", name=TITLE, exact=True))
        expect(current.locator(".book-status")).to_have_text("● В поиске")
        expect(current.locator(".ingestion-message")).to_contain_text("Обновление не удалось")
        expect(current.get_by_role("button", name=f"Читать: {TITLE}")).to_be_enabled()
        page.screenshot(path=str(artifacts / "isolated-availability-counters.png"))
        page.locator("#library-ingestion-status").select_option("failed")
        expect(page.locator("#library-books .book-card")).to_have_count(1)
        expect(page.locator("#library-count")).to_contain_text("из 1 изданий")
        page.locator("#library-status").select_option("failed")
        unavailable = page.locator("#library-books .book-card")
        expect(unavailable).to_have_count(1)
        expect(unavailable.locator(".ingestion-message")).to_have_text("Загрузка не удалась · текст пока недоступен")
        expect(unavailable.get_by_role("button")).to_be_disabled()

        page.goto(base, wait_until="networkidle")
        page.locator('[data-mode="lexical"]').click()
        page.locator("#query").fill("Amber")
        page.locator("#search-form").get_by_role("button", name="Найти").click()
        hit = page.locator("#fragments-panel .passage-card").first
        expect(hit.locator("blockquote")).to_have_text(ORIGINAL)
        expect(hit.locator(".ingestion-message")).to_contain_text("прежнее издание доступно")
        expect(page.locator("#search-warnings")).to_contain_text("последнее успешно проиндексированное издание")
        hit.get_by_role("button", name="Открыть в контексте").click()
        expect(page.locator("#found-passage")).to_have_text(ORIGINAL)
        warning = page.locator("#reader-ingestion-warning")
        expect(warning).to_contain_text("Обновление не удалось")
        expect(page.locator("#reader-book-meta")).to_contain_text("Первая версия CC0")
        warning.get_by_text("Причина последней попытки", exact=True).click()
        actual_error = page.request.get(f"{base}/api/books/pipeline-current").json()["ingestion_error"]
        expect(warning.locator(".ingestion-error p")).to_have_text(actual_error)
        page.locator(".source-details > summary").click()
        with page.expect_download() as download_info:
            page.locator("#reader-source .artifact-download").click()
        download = download_info.value.path()
        assert download is not None and Path(download).read_text() == ORIGINAL
        page.screenshot(path=str(artifacts / "isolated-failed-reader.png"))

        # Actual pipeline transitions in the isolated library, not mocked API fields.
        library.stage("pipeline-current", "deferred", "Controlled source retry cooldown")
        page.reload(wait_until="networkidle")
        expect(page.locator("#found-passage")).to_have_text(ORIGINAL)
        expect(warning).to_contain_text("Обновление отложено · прежнее издание доступно")
        page.set_viewport_size({"width": 390, "height": 844})
        warning.scroll_into_view_if_needed()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile warning overflows"
        page.screenshot(path=str(artifacts / "isolated-mobile-deferred-reader.png"))
        related = page.locator("#reader-related-editions")
        related.locator("summary").click()
        expect(related).to_contain_text("Русский перевод CC0")
        expect(related).to_contain_text("Перевод: Тестовый переводчик CC0")
        related.get_by_role("link", name="Русская версия теста CC0").click()
        expect(page.locator("#reader-sidebar-title")).to_have_text("Русская версия теста CC0")
        expect(page.locator("#reader-text")).to_contain_text("Янтарная закладка")
        expect(page.locator("#reader-ingestion-warning")).to_be_hidden()
        translated = page.request.get(f"{base}/api/books/pipeline-translation").json()
        params = parse_qs(urlsplit(page.url).query)
        assert params["section"] == [str(translated["sections"][0]["id"])] and "passage" not in params

        source.write_text(REPLACEMENT)
        library.import_book(source, metadata() | {"edition": "Вторая версия CC0"})
        page.goto(f"{base}/?book=pipeline-current", wait_until="networkidle")
        expect(page.locator("#reader-text")).to_have_text(REPLACEMENT)
        expect(page.locator("#reader-ingestion-warning")).to_be_hidden()
        expect(page.locator("#reader-book-meta")).to_contain_text("Вторая версия CC0")
        page.goto(f"{base}/?view=library", wait_until="networkidle")
        assert_counters(page, page.request.get(f"{base}/api/stats").json())
        expect(current.locator(".ingestion-notice")).to_have_count(0)
        page.get_by_role("button", name="Обновить счётчики", exact=True).click()
        expect(page.locator(".pipeline-refresh")).to_contain_text("Состояние на")
        assert not errors, errors
        context.close()
    return {
        "result": "passed",
        "source": "isolated actual API; original CC0 fixture; no mocks",
        "workflows": [
            "separate_exact_counters",
            "availability_attempt_filters",
            "failed_refresh_old_reader",
            "failed_new_book_unavailable",
            "exact_quote_and_original_download",
            "deferred_old_reader_mobile",
            "successful_retry_removes_warning",
            "curated_translation_navigation_without_alignment",
            "confirmed_full_work_lower_bound",
        ],
        "browser_errors": errors,
    }


def check_live(browser: Browser, base: str, artifacts: Path) -> dict[str, Any]:
    """Navigate the actual running library while asserting GET-only browser traffic."""
    base = base.rstrip("/")
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    errors: list[str] = []
    writes: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: writes.append(request.method) if request.method != "GET" else None)
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/stats") as stats_response:
        page.goto(f"{base}/?view=library", wait_until="networkidle")
    stats = stats_response.value.json()
    assert_counters(page, stats)
    card = page.locator("#library-books .book-card").first
    expect(card).to_be_visible()
    title = card.get_by_role("heading").inner_text()
    port = urlsplit(base).port or 80
    page.screenshot(path=str(artifacts / f"live-{port}-library.png"))
    card.get_by_role("button", name=f"Читать: {title}", exact=True).click()
    expect(page.locator("#reader-sidebar-title")).to_have_text(title)
    expect(page.locator("#reader-text")).not_to_be_empty()
    expect(page.locator("#reader-book-meta")).to_contain_text(title)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Live mobile reader overflows"
    assert not errors and not writes, {"browser_errors": errors, "unexpected_write_requests": writes}
    context.close()
    return {
        "result": "passed",
        "base": base,
        "read_only": True,
        "indexed_at_stats_response": stats["books"],
        "reader_title": title,
        "browser_errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/pipeline-browser"))
    parser.add_argument("--live-base-url", action="append", default=[])
    arguments = parser.parse_args()
    arguments.artifacts.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="book-atlas-pipeline-browser-") as temporary,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        fixture = check_fixture(browser, Path(temporary), arguments.artifacts)
        live = [check_live(browser, base, arguments.artifacts) for base in arguments.live_base_url]
        browser.close()
    report = {"fixture": fixture, "live": live}
    (arguments.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
