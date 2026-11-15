"""Real GET-only Chromium proof for scoped search URLs and delayed restoration.

Uses only new owned CC0 books, a tiny real float index and a fake encoder. Book
responses are real FastAPI responses, optionally delayed before their actual
handler; no HTTP response mocks, live-library writes or model/GPU calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import numpy as np
from browser_archive import fixture_server
from playwright.sync_api import Page, expect, sync_playwright

from book_rag import semantic
from book_rag.api import create_app
from book_rag.library import Library

FR = "scoped-link-fr-cc0"
RU = "scoped-link-ru-cc0"
UNAVAILABLE = "scoped-link-unavailable-cc0"
MISSING = "scoped-link-missing-cc0"


class Gate:
    def __init__(self) -> None:
        self.started, self.release, self.finished = threading.Event(), threading.Event(), threading.Event()
        self.active = False

    def arm(self) -> None:
        self.started.clear()
        self.release.clear()
        self.finished.clear()
        self.active = True


class FakeEncoder:
    calls = 0

    def encode(self, texts: list[str], **kwargs: Any) -> np.ndarray:
        self.calls += 1
        return np.tile(np.array([[1.0, 0.0]], dtype=np.float32), (len(texts), 1))


class NoGenerator:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("A scope link must never invoke answer generation")


def seed(directory: Path) -> Library:
    library = Library(directory / "library")
    for identity, language, title, text in (
        (
            FR,
            "fr",
            "Atelier des clefs · CC0",
            "La clef bleue 🦉 repose dans la tasse verte. "
            "Le gardien conserve ce fragment exact pour relire son carnet.",
        ),
        (
            RU,
            "ru",
            "Русская закладка · CC0",
            "Янтарная закладка лежит на седьмой полке. Читатель проверяет точные слова собственного дневника.",
        ),
        (
            "scoped-link-en-cc0",
            "en",
            "Copper bookmark · CC0",
            "A copper bookmark rests beside the window. This independent original archive has a different source.",
        ),
    ):
        source = directory / (identity + ".html")
        source.write_text(
            f"<html><meta charset='utf-8'><h1>{title}</h1><h2>Chapter one</h2><p>{text}</p>"
            f"<h2>Chapter two</h2><p>{text} A second exact chapter.</p></html>"
        )
        library.import_book(
            source,
            {
                "id": identity,
                "title": title,
                "author": "Book Atlas original CC0 fixture",
                "language": language,
                "edition": "Owned CC0 edition1",
                "rights": "CC0-1.0",
                "source_url": "https://example.invalid/book-atlas/scoped-links/" + identity,
            },
        )
    library.register(
        {
            "id": UNAVAILABLE,
            "title": "Unavailable original CC0 edition",
            "author": "CC0 fixture",
            "language": "fr",
            "rights": "CC0-1.0",
        }
    )
    generation = library.data_dir / "vectors" / "owned-scoped-link-fixture"
    generation.mkdir(parents=True)
    with library.connect() as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM passages ORDER BY id")]
    np.save(generation / "ids.npy", np.array(ids, dtype=np.int64))
    np.save(generation / "embeddings.npy", np.tile(np.array([[1, 0]], dtype=np.float32), (len(ids), 1)))
    (generation.parent / "active.json").write_text(
        json.dumps(
            {
                "generation": generation.name,
                "model": "owned-CC0-fake-encoder",
                "vectors": len(ids),
                "corpus_fingerprint": semantic.corpus_fingerprint(library),
            }
        )
    )
    return library


def route(
    base: str,
    query: str,
    *,
    scope: str | None = FR,
    language: str = "fr",
    mode: str = "semantic",
    panel: str | None = None,
) -> str:
    fields = {"q": query, "mode": mode, "language": language}
    if scope:
        fields["scope"] = scope
    if panel:
        fields["panel"] = panel
    return base + "/?" + urlencode(fields)


def params(page: Page) -> dict[str, list[str]]:
    return parse_qs(urlsplit(page.url).query)


def wait_event(page: Page, event: threading.Event, label: str) -> None:
    deadline = time.monotonic() + 6
    while not event.is_set():
        if time.monotonic() > deadline:
            raise TimeoutError("Owned API fixture event not observed: " + label)
        # Pump the browser transport, including pass-through request handlers,
        # while the real API coroutine is deliberately held by this fixture.
        page.wait_for_timeout(20)


def restored(page: Page, *, query: str, book: str, language: str, mode: str) -> None:
    expect(page.locator("#query")).to_have_value(query)
    expect(page.locator("#book-filter")).to_have_value(book)
    expect(page.locator("#language-filter")).to_have_value(language)
    expect(page.locator(f'[data-mode="{mode}"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#fragments-panel .passage-card").first).to_be_visible()
    current = params(page)
    assert current.get("q") == [query] and current.get("scope") == [book]
    assert current.get("language") == [language] and current.get("mode") == [mode]


def scoped_requests(
    requests: list[dict[str, Any]], start: int, *, book: str, language: str, mode: str | None = None
) -> None:
    relevant = requests[start:]
    assert relevant, "Expected an actual scoped search request"
    for request in relevant:
        assert request["book_id"] == [book], request
        assert request["language"] == [language], request
        if mode:
            assert request["mode"] == [mode], request


def exact_reader(page: Page, library: Library, book_id: str) -> None:
    quote = page.locator("#fragments-panel .passage-card").first.locator("blockquote").text_content()
    assert quote
    page.locator("#fragments-panel .passage-card").first.get_by_role("button", name="Открыть в контексте").click()
    expect(page.locator("#found-passage")).to_have_text(quote)
    location = params(page)
    assert location["book"] == [book_id]
    passage = library.passage(int(location["passage"][0]))
    assert passage and passage["verified"] and passage["text"] == quote
    chapter = page.locator("#reader-text").text_content()
    assert chapter and chapter[passage["start"] : passage["end"]] == quote


def run_fixture(directory: Path, artifacts: Path) -> dict[str, Any]:
    library = seed(directory)
    encoder, generator = FakeEncoder(), NoGenerator()
    gates = {FR: Gate(), MISSING: Gate()}
    app = create_app(library.data_dir, answer_generator=generator)

    @app.middleware("http")
    async def delayed_real_book(request: Any, call_next: Any) -> Any:
        identity = request.url.path.removeprefix("/api/books/")
        gate = gates.get(identity) if request.url.path.startswith("/api/books/") else None
        if gate is not None and gate.active:
            gate.started.set()
            try:
                if not await asyncio.to_thread(gate.release.wait, 10):
                    raise RuntimeError("Owned GET book gate was not released")
                return await call_next(request)
            finally:
                gate.active = False
                gate.finished.set()
        return await call_next(request)

    searches: list[dict[str, Any]] = []
    writes: list[dict[str, str]] = []
    errors: list[str] = []
    scenarios: list[str] = []
    with (
        patch("browser_archive.create_app", return_value=app),
        patch.object(semantic, "_model", return_value=encoder),
        patch.dict(os.environ, {"BOOKRAG_SEMANTIC_OVERLAY": "", "BOOKRAG_SEMANTIC_BATCH_REGISTRY": ""}),
        fixture_server(library.data_dir) as base,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.set_default_timeout(12_000)

        def protect(item: Any) -> None:
            request = item.request
            if request.method not in {"GET", "HEAD"}:
                writes.append({"method": request.method, "path": urlsplit(request.url).path})
                item.abort()
            else:
                item.continue_()

        context.route("**/*", protect)  # Writes blocked; every GET reaches the actual API.
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def record(request: Any) -> None:
            if urlsplit(request.url).path == "/api/search":
                searches.append(parse_qs(urlsplit(request.url).query))

        page.on("request", record)
        try:
            start = len(searches)
            page.goto(route(base, "clef"), wait_until="networkidle")
            restored(page, query="clef", book=FR, language="fr", mode="semantic")
            scoped_requests(searches, start, book=FR, language="fr", mode="semantic")
            scenarios.append("initial_scoped_semantic_fragment_link")
            start = len(searches)
            page.reload(wait_until="networkidle")
            restored(page, query="clef", book=FR, language="fr", mode="semantic")
            scoped_requests(searches, start, book=FR, language="fr", mode="semantic")
            scenarios.append("reload_preserves_query_mode_book_language")
            exact_reader(page, library, FR)
            page.locator("#reader-back").click()
            restored(page, query="clef", book=FR, language="fr", mode="semantic")
            scenarios.append("reader_custom_back_preserves_scoped_search")
            exact_reader(page, library, FR)
            start = len(searches)
            page.go_back()
            restored(page, query="clef", book=FR, language="fr", mode="semantic")
            assert all(request["book_id"] == [FR] for request in searches[start:])
            scenarios.append("browser_history_back_preserves_scoped_search")

            page.locator('[data-mode="lexical"]').click()
            restored(page, query="clef", book=FR, language="fr", mode="lexical")
            page.reload(wait_until="networkidle")
            restored(page, query="clef", book=FR, language="fr", mode="lexical")
            scenarios.append("changed_search_mode_survives_reload")
            for identity in (MISSING, UNAVAILABLE):
                start = len(searches)
                page.goto(route(base, "clef", scope=identity, mode="lexical"), wait_until="networkidle")
                expect(page.locator("#book-filter")).to_have_value(identity)
                expect(page.locator("#search-form [type=submit]")).to_be_disabled()
                expect(page.locator("#book-scope-selection")).to_be_visible()
                expect(page.locator("#fragments-panel")).to_contain_text("не подтверждена")
                page.locator("#query").press("Enter")
                assert searches[start:] == [], "Unavailable scope silently broadened search"
                assert params(page).get("scope") == [identity]
            scenarios.append("unknown_and_unindexed_scope_block_without_global_query")

            gate = gates[FR]
            gate.arm()
            start = len(searches)
            page.goto(route(base, "clef", mode="lexical"), wait_until="domcontentloaded")
            wait_event(page, gate.started, "delayed valid book started")
            expect(page.locator("#search-form [type=submit]")).to_be_disabled()
            page.locator('.site-header nav [data-view="library"]').click()
            gate.release.set()
            wait_event(page, gate.finished, "delayed valid book finished after navigation")
            expect(page.locator("#library-view")).to_be_visible()
            assert searches[start:] == []
            page.go_back()
            restored(page, query="clef", book=FR, language="fr", mode="lexical")
            scenarios.append("late_success_after_navigation_ignored_back_retries_scope")

            gate.arm()
            start = len(searches)
            page.goto(route(base, "clef", mode="lexical"), wait_until="domcontentloaded")
            wait_event(page, gate.started, "delayed valid book started for language change")
            page.locator("#language-filter").select_option("ru")
            gate.release.set()
            wait_event(page, gate.finished, "delayed valid book finished after language change")
            expect(page.locator("#language-filter")).to_have_value("ru")
            expect(page.locator("#search-form [type=submit]")).to_be_disabled()
            expect(page.locator("#fragments-panel")).to_contain_text("не подтверждена")
            page.locator("#query").press("Enter")
            assert searches[start:] == [], "Changing language silently removed the pending book scope"
            page.locator("#book-scope-selection").get_by_role("button", name="Снять ограничение").click()
            expect(page.locator("#search-form [type=submit]")).to_be_enabled()
            assert searches[start:], "Explicitly removing the scope should submit the retained query"
            assert all(request.get("book_id") is None and request["language"] == ["ru"] for request in searches[start:])
            scenarios.append("late_success_after_filter_change_cannot_restore_or_broaden_scope")

            missing_gate = gates[MISSING]
            missing_gate.arm()
            page.goto(route(base, "закладка", scope=MISSING, mode="lexical"), wait_until="domcontentloaded")
            wait_event(page, missing_gate.started, "delayed missing book started")
            page.locator("#language-filter").select_option("ru")
            page.locator("#book-scope-query").fill("Русская закладка")
            expect(page.locator(f'#book-filter option[value="{RU}"]')).to_have_count(1)
            page.locator("#book-filter").select_option(RU)
            restored(page, query="закладка", book=RU, language="ru", mode="lexical")
            missing_gate.release.set()
            wait_event(page, missing_gate.finished, "delayed missing book finished after explicit selection")
            restored(page, query="закладка", book=RU, language="ru", mode="lexical")
            scenarios.append("late_404_cannot_clear_new_explicit_scope")
            page.screenshot(path=str(artifacts / "scoped-search-desktop.png"))

            page.goto(route(base, "Pourquoi la clef est-elle dans la tasse?", panel="answer"), wait_until="networkidle")
            expect(page.locator("#answer-panel")).to_be_visible()
            expect(page.locator("#book-filter")).to_have_value(FR)
            expect(page.locator("#answer-plan-retrieval")).to_be_enabled()
            expect(page.locator("#answer-plan-retrieval")).not_to_be_checked()
            page.locator("#answer-plan-retrieval").check()
            assert not writes and generator.calls == 0
            page.reload(wait_until="networkidle")
            expect(page.locator("#answer-plan-retrieval")).not_to_be_checked()
            expect(page.locator("#answer-plan-retrieval")).to_be_enabled()
            assert not writes and generator.calls == 0
            scenarios.append("answer_scope_and_checkbox_restore_without_auto_ask")

            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(route(base, "закладка", scope=RU, language="ru", mode="hybrid"), wait_until="networkidle")
            restored(page, query="закладка", book=RU, language="ru", mode="hybrid")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            results_box = page.locator("#search-results .results-main").bounding_box()
            assert results_box is not None and -2 <= results_box["y"] < 100, results_box
            page.screenshot(path=str(artifacts / "scoped-search-mobile-results.png"))
            exact_reader(page, library, RU)
            page.screenshot(path=str(artifacts / "scoped-search-mobile-reader.png"))
            page.locator("#reader-back").click()
            restored(page, query="закладка", book=RU, language="ru", mode="hybrid")
            results_box = page.locator("#search-results .results-main").bounding_box()
            assert results_box is not None and -2 <= results_box["y"] < 100, results_box
            scenarios.append("mobile_scoped_reader_return_has_no_horizontal_overflow")
            assert not errors and not writes and generator.calls == 0, (errors, writes)
            assert library.saved() == []
        finally:
            for gate in gates.values():
                gate.release.set()
            context.close()
            browser.close()
    return {
        "kind": "owned-CC0-scoped-search-links-browser-proof",
        "real_api": True,
        "http_response_mocks": False,
        "get_only": True,
        "javascript_errors": errors,
        "non_get_requests": writes,
        "answer_generation_calls": generator.calls,
        "real_model_calls": 0,
        "fake_encoder_calls": encoder.calls,
        "saved_items_written": 0,
        "scenarios": scenarios,
        "search_requests": searches,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/scoped-search-links"))
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bookrag-scoped-links-") as directory:
        report = run_fixture(Path(directory), args.artifacts)
    (args.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "scenarios": len(report["scenarios"]),
                "get_only": True,
                "real_model_calls": 0,
                "javascript_errors": len(report["javascript_errors"]),
            }
        )
    )


if __name__ == "__main__":
    main()
