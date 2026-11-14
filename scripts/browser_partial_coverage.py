"""Check semantic-coverage presentation using an isolated tiny ready generation.

Default mode adds a clearly declared in-process coverage-contract endpoint to a
real API; it enriches lexical evidence and does not claim semantic relevance.
--real-backend exercises the integrated partial-search endpoint unchanged.
Both modes use a deterministic fake encoder/tokenizer and only owned temp data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import numpy as np
from browser_archive import fixture_server
from fastapi.routing import APIRoute
from playwright.sync_api import Page, expect, sync_playwright

from book_rag.api import create_app
from book_rag.index_coverage import validate_coverage
from book_rag.index_installation import install_partial_index, read_installation_receipt
from book_rag.library import Library
from book_rag.semantic import build_index


class Tokenizer:
    def encode(self, text: str, **kwargs: Any) -> list[str]:
        return text.split()

    def decode(self, values: list[str], **kwargs: Any) -> str:
        return " ".join(values)

    def __call__(self, text: str, **kwargs: Any) -> dict[str, list[tuple[int, int]]]:
        return {"offset_mapping": [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]}


class Encoder:
    max_seq_length = 64
    tokenizer = Tokenizer()

    def get_embedding_dimension(self) -> int:
        return 4

    def encode(self, texts: list[str], **kwargs: Any) -> np.ndarray:
        values = np.array(
            [[part + 1 for part in hashlib.sha256(text.encode()).digest()[:4]] for text in texts], dtype=np.float32
        )
        return values / np.linalg.norm(values, axis=1, keepdims=True)


def search(page: Page, query: str) -> dict[str, Any]:
    page.locator("#query").fill(query)
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/search") as response:
        page.locator("#search-form").get_by_role("button", name="Найти", exact=True).click()
    assert response.value.ok
    return response.value.json()


def assert_coverage(page: Page, *, outside: bool = False) -> None:
    badge = page.locator("#semantic-coverage")
    expect(badge).to_be_visible()
    expect(badge.locator(".semantic-coverage-title")).to_have_text("Поиск по смыслу: 1 из 3 изданий")
    expect(badge).to_contain_text("Поиск по словам доступен по всей библиотеке")
    expect(badge.locator(".semantic-coverage-scope")).to_have_count(1 if outside else 0)
    if outside:
        expect(badge).to_contain_text("Для выбранной книги доступен поиск по словам")


def run_fixture(directory: Path, artifacts: Path, *, real_backend: bool) -> dict[str, Any]:
    source = Library(directory / "source")
    text = directory / "covered.txt"
    text.write_text("Amber bookmarks preserve a verified original CC0 fixture quotation.")
    source.import_book(text, {"id": "covered", "title": "Книга с индексом", "author": "CC0 fixture", "language": "en"})
    model = Encoder()
    with (
        patch("book_rag.semantic._model", return_value=model),
        patch("book_rag.index_installation._load_tokenizer", return_value=model.tokenizer),
    ):
        build_index(source, batch_size=3)
        target_dir = directory / "target"
        target_dir.mkdir()
        with source.connect() as origin, sqlite3.connect(target_dir / "library.sqlite3") as target_db:
            origin.backup(target_db)
        target = Library(target_dir)
        for identity, title, content in (
            ("outside", "Книга вне индекса", "Bronze bookmarks occur in this separate CC0 edition."),
            ("third", "Третья книга", "Third original CC0 fixture chapter remains searchable by words."),
        ):
            path = directory / f"{identity}.txt"
            path.write_text(content)
            target.import_book(path, {"id": identity, "title": title, "author": "CC0 fixture", "language": "en"})
        install_partial_index(source.data_dir, target_dir)
        app = create_app(target_dir)
        legacy_contract = {"omit": False}

        if not real_backend:

            def contract_search(
                q: str, mode: str = "hybrid", book_id: str | None = None, language: str | None = None, limit: int = 20
            ) -> dict[str, Any]:
                result = target.search(q, "lexical", book_id, language, limit)
                if legacy_contract["omit"]:
                    return result
                metadata = json.loads((target_dir / "vectors/active.json").read_text())
                active = False
                try:
                    receipt = read_installation_receipt(target_dir, metadata)
                    active = validate_coverage(target, receipt["coverage"], receipt["coverage_binding"])["valid"]
                except (ValueError, OSError, KeyError):
                    pass
                requested = (
                    None if book_id is None else book_id in metadata.get("coverage", {}).get("covered_book_ids", [])
                )
                result["semantic_coverage"] = {
                    "active": active,
                    "indexed_books": metadata.get("coverage", {}).get("books", 0),
                    "searchable_books": target.stats()["books"],
                    "requested_book_covered": requested,
                }
                if not active:
                    result["warnings"].append("Семантический индекс недоступен; показан поиск по словам.")
                elif requested is False:
                    result["warnings"].append("Для выбранной книги доступен поиск по словам.")
                return result

            app.router.routes.insert(0, APIRoute("/api/search", contract_search, methods=["GET"]))
        errors: list[str] = []
        with (
            patch("browser_archive.create_app", return_value=app),
            fixture_server(target_dir) as base,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base, wait_until="networkidle")
            expect(page.locator("#semantic-coverage")).to_be_hidden()
            healthy = search(page, "amber")
            assert healthy["semantic_coverage"]["active"]
            assert_coverage(page)
            page.locator(".results-main").screenshot(path=str(artifacts / "partial-healthy.png"))
            page.locator("#research-tab").click()
            expect(page.locator("#semantic-coverage")).to_be_hidden()
            page.locator("#fragments-tab").click()
            assert_coverage(page)
            page.locator("#book-scope-query").fill("Книга вне индекса")
            expect(page.locator('#book-filter option[value="outside"]')).to_have_count(1)
            with page.expect_response(lambda response: urlsplit(response.url).path == "/api/search"):
                page.locator("#book-filter").select_option("outside")
            outside = search(page, "bronze")
            assert outside["mode"] == "lexical" and outside["semantic_coverage"]["requested_book_covered"] is False
            assert_coverage(page, outside=True)
            expect(page.locator("#search-warnings")).not_to_be_empty()
            hit = page.locator("#fragments-panel .passage-card").first
            quote = hit.locator("blockquote").inner_text()
            hit.get_by_role("button", name="Открыть в контексте").click()
            expect(page.locator("#found-passage")).to_have_text(quote)
            page.locator("#reader-back").click()
            # Slow real transport exposes the pending state without route mocks.
            network = context.new_cdp_session(page)
            network.send("Network.enable")
            network.send(
                "Network.emulateNetworkConditions",
                {"offline": False, "latency": 450, "downloadThroughput": -1, "uploadThroughput": -1},
            )
            with page.expect_response(lambda response: urlsplit(response.url).path == "/api/search"):
                page.locator("#clear-filters").click()
                expect(page.locator("#semantic-coverage")).to_be_hidden()
                expect(page.locator("#semantic-coverage")).to_be_empty()
            network.send(
                "Network.emulateNetworkConditions",
                {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1},
            )
            network.detach()
            assert_coverage(page)

            # A genuine completed global generation exercises the legacy path.
            build_index(target, batch_size=3)
            legacy_contract["omit"] = True
            legacy = search(page, "third")
            assert "semantic_coverage" not in legacy
            expect(page.locator("#semantic-coverage")).to_be_hidden()
            expect(page.locator("#semantic-coverage")).to_be_empty()
            install_partial_index(source.data_dir, target_dir)
            legacy_contract["omit"] = False
            search(page, "amber")
            assert_coverage(page)
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator("#semantic-coverage")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Partial badge overflows mobile"
            page.locator("#semantic-coverage").screenshot(path=str(artifacts / "partial-mobile.png"))
            with target.connect() as conn:
                conn.execute("UPDATE books SET title='Changed covered header' WHERE id='covered'")
            stale = search(page, "amber")
            assert stale["mode"] == "lexical"
            assert not stale.get("semantic_coverage", {}).get("active", False)
            expect(page.locator("#semantic-coverage")).to_be_hidden()
            expect(page.locator("#semantic-coverage")).to_be_empty()
            expect(page.locator("#search-warnings")).not_to_be_empty()
            with target.connect() as conn:
                conn.execute("UPDATE books SET title='Книга с индексом' WHERE id='covered'")
            installed = install_partial_index(source.data_dir, target_dir)
            search(page, "amber")
            assert_coverage(page)
            receipt_path = target_dir / "vectors" / installed["installation_receipt"]["path"]
            receipt_path.write_bytes(receipt_path.read_bytes() + b" ")
            corrupt = search(page, "amber")
            assert corrupt["mode"] == "lexical" and not corrupt.get("semantic_coverage", {}).get("active", False)
            expect(page.locator("#semantic-coverage")).to_be_hidden()
            expect(page.locator("#semantic-coverage")).to_be_empty()
            expect(page.locator("#search-warnings")).not_to_be_empty()
            page.locator(".results-main").screenshot(path=str(artifacts / "partial-fallback.png"))
            assert not errors, errors
            context.close()
            browser.close()
    return {
        "result": "passed",
        "backend": "integrated real API"
        if real_backend
        else "declared in-process contract endpoint with lexical evidence",
        "fixture": "tiny completed fake-encoder generation, real installer/epoch guards, owned temp API/Chromium",
        "workflows": [
            "healthy_edition_coverage",
            "outside_scope_words_warning_exact_reader",
            "pending_clears_old_badge",
            "research_panel_hides_search_coverage",
            "legacy_without_object_clears_badge",
            "stale_epoch_clears_badge",
            "corrupt_receipt_clears_badge",
            "mobile_no_overflow",
        ],
        "browser_errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/partial-coverage-browser"))
    parser.add_argument("--real-backend", action="store_true")
    arguments = parser.parse_args()
    arguments.artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="book-atlas-partial-ui-") as temporary:
        report = run_fixture(Path(temporary).resolve(), arguments.artifacts, real_backend=arguments.real_backend)
    (arguments.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
