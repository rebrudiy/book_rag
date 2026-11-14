"""Verify frozen original downloads in an isolated real API and Chromium browser.

The two short original fixture editions are dedicated to CC0. Only a temporary
directory owned by this process is changed; live libraries and notes are untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import tempfile
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn
from playwright.sync_api import Locator, Page, expect, sync_playwright

from book_rag.api import create_app
from book_rag.library import Library

BOOK_ID = "archive-browser-cc0"
PROVIDER = "https://example.org/book-atlas/archive-browser-fixture"
DOWNLOAD_LABEL = "Скачать зафиксированный файл"
FIXTURE_TITLE = "Архивный опыт · оригинальный тест CC0"
SAVED_TITLE = "Archive browser: frozen first edition"


def edition_bytes(color: str) -> bytes:
    """Generate transparent, original CC0 source bytes rather than fetched content."""
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f"<title>{FIXTURE_TITLE}</title></head><body><h1>{FIXTURE_TITLE}</h1>"
        "<h2>Глава первая</h2>"
        f"<p>На столе лежала {color} закладка. Читатель сохранил эту фразу, "
        "чтобы проверить связь заметки с полученным файлом. "
        "Автор: Book Atlas browser fixture. Этот оригинальный тестовый текст передан в CC0.</p>"
        "</body></html>"
    ).encode()


@contextmanager
def fixture_server(directory: Path) -> Generator[str]:
    """Run the actual application on an OS-assigned private loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(directory), log_level="error", access_log=False))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not worker.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("Isolated archive fixture server failed to start")
                time.sleep(0.02)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            worker.join(timeout=10)
            if worker.is_alive():
                server.force_exit = True
                worker.join(timeout=2)
                raise RuntimeError("Isolated archive fixture server did not stop")


def check_download(page: Page, link: Locator, expected: bytes) -> str:
    """Read the real browser attachment and independently hash the acquired bytes."""
    digest = hashlib.sha256(expected).hexdigest()
    expect(link).to_have_attribute("href", f"/api/artifacts/{digest}")
    expect(link).to_have_attribute("download", "")
    with page.expect_download() as download_info:
        link.click()
    download = download_info.value
    path = download.path()
    assert path is not None and download.failure() is None
    actual = Path(path).read_bytes()
    assert actual == expected and hashlib.sha256(actual).hexdigest() == digest
    assert download.suggested_filename == f"{digest}.html", "Retain the acquired format and SHA filename"
    return digest


def run_fixture(directory: Path, artifacts: Path) -> dict[str, Any]:
    library = Library(directory / "library")
    source = directory / "original-fixture.html"
    original = edition_bytes("янтарная")
    replacement = edition_bytes("лазурная")
    source.write_bytes(original)
    metadata = {
        "id": BOOK_ID,
        "title": FIXTURE_TITLE,
        "author": "Book Atlas browser fixture",
        "language": "ru",
        "edition": "Оригинальный тест CC0 · версия 1",
        "source_url": PROVIDER,
        "rights": "CC0 1.0 · original Book Atlas browser fixture",
        "rights_url": "https://creativecommons.org/publicdomain/zero/1.0/",
    }
    assert library.import_book(source, metadata)["status"] == "indexed"
    library.register(metadata | {"id": "archive-not-acquired", "title": "Ещё не полученный тест CC0"})
    browser_errors: list[str] = []
    with fixture_server(library.data_dir) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.goto(base, wait_until="networkidle")
        page.locator('[data-mode="lexical"]').click()
        page.locator("#query").fill("янтарная закладка")
        page.locator("#search-form").get_by_role("button", name="Найти").click()
        hit = page.locator("#fragments-panel .passage-card").first
        expect(hit).to_be_visible()
        expected_quote = hit.locator("blockquote").inner_text()
        hit.get_by_role("button", name="Открыть в контексте").click()
        expect(page.locator("#found-passage")).to_have_text(expected_quote)
        page.locator(".source-details > summary").click()
        old_sha = check_download(page, page.locator("#reader-source .artifact-download"), original)
        expect(page.locator("#reader-source").get_by_role("link", name="Открыть первоисточник")).to_have_attribute(
            "href", PROVIDER
        )
        expect(page.locator("#reader-source")).to_contain_text(metadata["rights"])
        page.locator("#reader-citation").get_by_role("button", name="Сохранить фрагмент").click()
        page.locator("#save-title").fill(SAVED_TITLE)
        page.locator("#save-collection").fill("Archive integrity CC0")
        page.locator("#save-note").fill("Файл первой версии должен сохраниться после замены источника.")
        page.locator("#save-form").get_by_role("button", name="Сохранить", exact=True).click()
        expect(page.locator("#save-dialog")).not_to_be_visible()

        # Deliberately overwrite only this process's mutable local input in place.
        # The archival copy and the saved provenance must remain version one.
        source.write_bytes(replacement)
        assert (
            library.import_book(source, metadata | {"edition": "Оригинальный тест CC0 · версия 2"})["status"]
            == "indexed"
        )
        page.goto(f"{base}/?view=saved", wait_until="networkidle")
        saved = page.locator(".saved-item").filter(has=page.get_by_role("heading", name=SAVED_TITLE))
        expect(saved.locator("blockquote")).to_have_text(expected_quote)
        expect(saved.locator(".passage-citation")).to_contain_text("версия 1")
        assert check_download(page, saved.get_by_role("link", name=DOWNLOAD_LABEL), original) == old_sha
        frozen = page.request.get(f"{base}/api/saved").json()["items"][0]
        assert frozen["artifact_url"] == f"/api/artifacts/{old_sha}"
        assert frozen["provenance"]["artifact_sha256"] == old_sha
        expect(saved.get_by_role("button", name="Открыть в контексте")).to_have_count(0)
        page.screenshot(path=str(artifacts / "saved-frozen-original.png"))

        page.goto(f"{base}/?book={BOOK_ID}", wait_until="networkidle")
        expect(page.locator("#reader-text")).to_contain_text("лазурная закладка")
        expect(page.locator("#reader-book-meta")).to_contain_text("версия 2")
        page.locator(".source-details > summary").click()
        new_sha = check_download(page, page.locator("#reader-source .artifact-download"), replacement)
        assert new_sha != old_sha
        for sha, expected in ((old_sha, original), (new_sha, replacement)):
            response = page.request.get(f"{base}/api/artifacts/{sha}")
            assert response.ok and response.body() == expected
            assert response.headers["content-type"] == "application/octet-stream"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert "sandbox" in response.headers["content-security-policy"]
            assert f'{sha}.html"' in response.headers["content-disposition"]
        page.screenshot(path=str(artifacts / "reader-current-original.png"))
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator("#reader-source .artifact-download")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile source panel overflows"
        page.locator(".source-details").screenshot(path=str(artifacts / "mobile-original-download.png"))

        # Missing archive metadata must not produce an invented download locator.
        page.goto(f"{base}/?book=archive-not-acquired", wait_until="networkidle")
        expect(page.locator("#reader-sidebar-title")).to_have_text("Ещё не полученный тест CC0")
        expect(page.locator("#reader-source .artifact-download")).to_have_count(0)
        assert not browser_errors, browser_errors
        context.close()
        browser.close()
    return {
        "result": "passed",
        "fixture": "original CC0 two-edition HTML; temporary isolated library; no API mocks",
        "old_sha256": old_sha,
        "new_sha256": new_sha,
        "workflows": [
            "exact_quote_reader_save",
            "in_place_source_replacement",
            "saved_frozen_original_download",
            "current_reader_new_original_download",
            "source_provider_and_rights_separate",
            "verified_attachment_headers_and_format",
            "mobile_source_panel",
            "no_download_without_archive",
        ],
        "browser_errors": browser_errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/archive-browser"))
    arguments = parser.parse_args()
    arguments.artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="book-atlas-archive-browser-") as temporary:
        report = run_fixture(Path(temporary), arguments.artifacts)
    (arguments.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
