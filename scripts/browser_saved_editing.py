"""Edit saved annotations in a real isolated API/Chromium without changing evidence.

Original CC0 fixture, reindexing and deletion fault stay in the owned temporary
library. No API responses are mocked and no live annotations or servers change.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from browser_archive import fixture_server
from playwright.sync_api import Page, expect, sync_playwright

from book_rag.library import Library

ORIGINAL = "Historical amber 🦉 preserves the original CC0 quotation."
QUERY = "зачем философы спорят о свободе?"
NOTE = "Пересмотрено после замены издания.\nВторая строка заметки. <script>window.EDIT_XSS=true</script>"


def submit(page: Page) -> dict[str, Any]:
    with page.expect_response(
        lambda response: response.request.method == "PATCH" and urlsplit(response.url).path.startswith("/api/saved/")
    ) as response:
        page.get_by_role("button", name="Сохранить изменения", exact=True).click()
    assert response.value.status == 200
    expect(page.locator("#save-dialog")).not_to_be_visible()
    return response.value.json()


def run_fixture(directory: Path, artifacts: Path) -> dict[str, Any]:
    library = Library(directory / "library")
    source = directory / "original.html"
    source.write_text(f"<h1>First chapter</h1><p>{ORIGINAL}</p><h2>Second chapter</h2><p>Original context follows.</p>")
    library.import_book(
        source,
        {
            "id": "book",
            "title": "Original CC0 book",
            "author": "CC0 browser fixture",
            "edition": "First edition CC0",
            "language": "en",
            "rights": "CC0 1.0",
            "source_url": "https://example.org/saved-editing-original",
        },
    )
    hit = library.search("amber")["results"][0]
    saved = library.save(
        {
            "kind": "passage",
            "passage_id": hit["id"],
            "title": "Исходная заметка",
            "note": "Первая мысль",
            "collection": "Уходящая коллекция",
        }
    )
    query = library.save(
        {
            "kind": "query",
            "query": QUERY,
            "title": "Исходный запрос",
            "note": "Первый вопрос",
            "collection": "Существующая коллекция",
        }
    )
    disposable = library.save({"kind": "query", "query": "temporary CC0 query", "title": "Удаляемая фикстура"})
    source.write_text("<h1>Replacement chapter</h1><p>The current CC0 edition is different.</p>")
    library.import_book(source, {"id": "book", "edition": "Second edition CC0"})
    before = library.get_saved(saved["id"])
    assert before is not None and before["passage_id"] is None
    historical_before = library.saved_context(saved["id"])
    errors: list[str] = []
    patches: list[dict[str, Any]] = []
    with fixture_server(library.data_dir) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: patches.append(request.post_data_json or {}) if request.method == "PATCH" else None,
        )
        page.goto(f"{base}/?view=saved", wait_until="networkidle")
        card = page.locator(f'.saved-item[data-saved-id="{saved["id"]}"]')
        editor = card.get_by_role("button", name="Редактировать", exact=True)
        editor.click()
        dialog = page.get_by_role("dialog", name="Редактировать запись", exact=True)
        expect(dialog).to_be_visible()
        expect(page.locator("#save-title")).to_be_focused()
        expect(page.locator("#save-title")).to_have_value("Исходная заметка")
        expect(page.locator("#save-note")).to_have_value("Первая мысль")
        expect(page.locator("#save-collection")).to_have_value("Уходящая коллекция")
        expect(page.locator("#save-preview")).to_have_text(ORIGINAL)
        expect(page.locator("#save-preview input, #save-preview textarea")).to_have_count(0)
        page.locator("#save-title").fill("Отменённое изменение")
        page.locator("#save-note").fill("Этот черновик отменён")
        page.get_by_role("button", name="Отмена", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(editor).to_be_focused()
        assert not patches and library.get_saved(saved["id"]) == before
        editor.click()
        page.locator("#save-collection").fill("Отменённая коллекция")
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(editor).to_be_focused()
        assert not patches and library.get_saved(saved["id"]) == before
        editor.click()
        page.get_by_role("button", name="Сохранить изменения", exact=True).click()
        expect(dialog).not_to_be_visible()
        assert not patches, "An unchanged dialog must not submit an empty PATCH"

        editor.click()
        page.locator("#save-title").fill("Пересмотренная заметка")
        page.locator("#save-note").fill(NOTE)
        page.locator("#save-collection").fill("Новая коллекция")
        page.screenshot(path=str(artifacts / "saved-editing-dialog.png"))
        network = context.new_cdp_session(page)
        network.send("Network.enable")
        network.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 700, "downloadThroughput": -1, "uploadThroughput": -1},
        )
        with page.expect_response(lambda response: response.request.method == "PATCH") as submitted:
            page.get_by_role("button", name="Сохранить изменения", exact=True).click()
            expect(dialog).to_have_attribute("aria-busy", "true")
            expect(page.locator("#save-note")).to_be_disabled()
            expect(page.locator("#save-status")).to_have_text("Сохраняем изменения…")
            page.keyboard.press("Escape")
            expect(dialog).to_be_visible()
        assert submitted.value.status == 200
        expect(dialog).not_to_be_visible()
        network.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1},
        )
        network.detach()
        updated = submitted.value.json()
        assert patches[-1] == {"title": "Пересмотренная заметка", "collection": "Новая коллекция", "note": NOTE}
        for field in (
            "text",
            "citation",
            "provenance",
            "created_at",
            "artifact_url",
            "historical_reader_url",
            "passage_id",
            "query",
            "kind",
            "id",
        ):
            assert updated[field] == before[field], field
        assert library.saved_context(saved["id"]) == historical_before
        expect(editor).to_be_focused()
        expect(card.get_by_role("heading")).to_have_text("Пересмотренная заметка")
        expect(card.locator(".saved-note")).to_have_text(NOTE)
        expect(page.locator("#collection-filter")).to_have_value("Новая коллекция")
        expect(page.locator('#collection-filter option[value="Уходящая коллекция"]')).to_have_count(0)
        expect(page.locator('#collection-options option[value="Новая коллекция"]')).to_have_count(1)
        expect(page.locator("#saved-count")).to_have_text("3")
        assert page.evaluate("typeof window.EDIT_XSS") == "undefined"
        with page.expect_download() as markdown_info:
            page.locator("#export-markdown").click()
        markdown_path = markdown_info.value.path()
        assert markdown_path is not None
        markdown = html.unescape(re.sub(r"\\([\\`*_{}\[\]()#+.!|\-])", r"\1", Path(markdown_path).read_text()))
        assert "Пересмотренная заметка" in markdown and "Вторая строка заметки" in markdown and ORIGINAL in markdown
        with page.expect_download() as json_info:
            page.locator("#export-saved").click()
        json_path = json_info.value.path()
        assert json_path is not None
        exported = json.loads(Path(json_path).read_text())["items"]
        assert len(exported) == 1 and exported[0]["note"] == NOTE and exported[0]["provenance"] == before["provenance"]

        # Choose an existing collection, then explicitly clear collection/note.
        editor.click()
        page.locator("#save-collection").fill("Существующая коллекция")
        submit(page)
        assert patches[-1] == {"collection": "Существующая коллекция"}
        expect(page.locator("#collection-filter")).to_have_value("Существующая коллекция")
        expect(page.locator('#collection-filter option[value="Новая коллекция"]')).to_have_count(0)
        editor.click()
        page.locator("#save-note").fill("")
        page.locator("#save-collection").fill("")
        submit(page)
        assert patches[-1] == {"note": "", "collection": ""}
        expect(page.locator("#collection-filter")).to_have_value("")
        expect(card.locator(".saved-note")).to_have_count(0)
        card.get_by_role("link", name="Читать сохранённое издание", exact=True).click()
        expect(page.locator("#found-passage")).to_have_text(ORIGINAL)
        expect(page.locator("#reader-book-meta")).to_contain_text("First edition CC0")
        expect(page.locator("#reader-historical-label")).to_be_visible()
        page.locator("#reader-back").click()

        query_card = page.locator(f'.saved-item[data-saved-id="{query["id"]}"]')
        query_card.get_by_role("button", name="Редактировать", exact=True).click()
        expect(page.locator("#save-preview")).to_have_text(QUERY)
        expect(page.locator("#save-mode-description")).to_contain_text("Исходный сохранённый запрос")
        page.locator("#save-title").fill("Новый заголовок запроса")
        page.locator("#save-note").fill("Уточнённая заметка к запросу")
        edited_query = submit(page)
        assert edited_query["query"] == QUERY and set(patches[-1]) == {"title", "note"}
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/search") as searched:
            query_card.get_by_role("button", name="Продолжить поиск").click()
        assert parse_qs(urlsplit(searched.value.url).query)["q"] == [QUERY]
        expect(page.locator("#query")).to_have_value(QUERY)

        page.get_by_role("navigation", name="Основная навигация").get_by_text("Сохранённое").click()
        page.set_viewport_size({"width": 390, "height": 844})
        query_card.get_by_role("button", name="Редактировать", exact=True).click()
        expect(page.locator("#save-title")).to_be_focused()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile editing overflows"
        dialog_bounds = dialog.bounding_box()
        assert dialog_bounds is not None and dialog_bounds["width"] <= 390
        page.screenshot(path=str(artifacts / "saved-editing-mobile.png"))
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(query_card.get_by_role("button", name="Редактировать", exact=True)).to_be_focused()

        # Actual concurrent deletion makes PATCH return404; preserve the typed draft.
        doomed = page.locator(f'.saved-item[data-saved-id="{disposable["id"]}"]')
        doomed.get_by_role("button", name="Редактировать", exact=True).click()
        page.locator("#save-note").fill("Сохранить черновик после ошибки")
        assert page.request.delete(f"{base}/api/saved/{disposable['id']}").ok
        with page.expect_response(lambda response: response.request.method == "PATCH") as failed:
            page.get_by_role("button", name="Сохранить изменения", exact=True).click()
        assert failed.value.status == 404
        expect(dialog).to_be_visible()
        expect(page.locator("#save-error")).to_contain_text("Запись не найдена. Введённый текст остался в диалоге.")
        expect(page.locator("#save-note")).to_have_value("Сохранить черновик после ошибки")
        expect(page.locator("#save-note")).to_be_enabled()
        expect(page.get_by_role("button", name="Сохранить изменения", exact=True)).to_be_enabled()
        page.screenshot(path=str(artifacts / "saved-editing-error-draft.png"))
        page.get_by_role("button", name="Отмена", exact=True).click()
        expect(dialog).not_to_be_visible()
        # The UI prevents empty PATCH; verify the real server rejects one unchanged.
        unchanged = library.get_saved(query["id"])
        assert page.request.patch(f"{base}/api/saved/{query['id']}", data={}).status == 400
        assert library.get_saved(query["id"]) == unchanged
        page.reload(wait_until="networkidle")
        expect(page.locator("#saved-count")).to_have_text("2")
        assert library.saved_context(saved["id"]) == historical_before
        assert not errors, errors
        context.close()
        browser.close()
    return {
        "result": "passed",
        "fixture": "isolated original CC0; real API and Chromium; no API mocks",
        "workflows": [
            "prefilled_edit_dialog",
            "cancel_escape_and_unchanged_without_patch",
            "changed_fields_only",
            "busy_status_and_submit_guard",
            "new_existing_and_cleared_collection",
            "filter_count_updates",
            "markdown_json_updated_annotations",
            "immutable_old_quote_provenance_and_historical_reader",
            "query_text_readonly_resume_original",
            "mobile_keyboard_focus_no_overflow",
            "real_404_keeps_draft",
            "real_empty_patch_400_preserves_item",
        ],
        "browser_errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/saved-editing-browser"))
    arguments = parser.parse_args()
    arguments.artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="book-atlas-saved-editing-") as temporary:
        report = run_fixture(Path(temporary), arguments.artifacts)
    (arguments.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
