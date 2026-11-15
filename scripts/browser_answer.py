"""Exercise cited answers in a real isolated API/Chromium with a fake generator.

Only owned temporary CC0 source files and notes are changed. The real ask
endpoint, retrieval, citation validator, source-change guard, reader and save API
are used unchanged. No HTTP response mocks, model downloads or paid APIs.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

from browser_archive import fixture_server
from playwright.sync_api import Page, expect, sync_playwright

from book_rag.api import create_app
from book_rag.library import Library

BOOK_ID = "answer-browser-cc0"
QUESTION = "Зачем читатель хранит янтарную закладку?"
CLAIM = "Читатель хранит янтарную закладку, чтобы помнить прочитанное 🦉."
LITERAL = "<img src=x onerror=window.answerInjected=true>"


def original_text(version: str = "первая") -> str:
    return (
        '<html lang="ru"><head><meta charset="utf-8"><title>Опыт ответа CC0</title></head><body>'
        "<h1>Опыт ответа CC0</h1><h2>Первая глава · память</h2>"
        f"<p>Это {version} редакция оригинального теста CC0. Читатель хранит янтарную закладку 🦉, "
        "чтобы помнить прочитанное и возвращаться к точным словам книги. "
        "Янтарная закладка помогает читателю найти мысль в контексте главы.</p>"
        "<h2>Вторая глава · источник</h2><p>Вторая глава оригинального теста CC0 объясняет, "
        "зачем читатель проверяет источник: точные слова помогают сопоставить вывод с текстом.</p></body></html>"
    )


class FakeGenerator:
    """Actual generator protocol; responses still pass the real source validator."""

    def __init__(self) -> None:
        self.mode = "answered"
        self.calls: list[dict[str, Any]] = []
        self.judge_calls: list[dict[str, Any]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.change_source: Any = None

    def generate(
        self, messages: list[dict[str, Any]], limits: Any = None, *, response_schema: Any = None
    ) -> dict[str, Any]:
        payload = json.loads(messages[-1]["content"])
        if payload.get("task") == "verify_claims":
            self.judge_calls.append(payload)
            assert payload["question"] and payload["claims_to_verify"]
            assert all(claim["quotes"] for claim in payload["claims_to_verify"])
            return {
                "text": json.dumps({"supported": [self.mode != "judge_false"] * len(payload["claims_to_verify"])}),
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "model": {"id": "original-cc0-browser-fake", "revision": "fixture-v1"},
                "elapsed_ms": 1,
            }
        self.calls.append(payload)
        assert payload["question"] and payload["sources"], "Empty retrieval must not call the generator"
        evidence_id = payload["sources"][0]["evidence"][0]["evidence_id"]
        mode = self.mode
        if mode == "slow":
            self.started.set()
            assert self.release.wait(8), "Browser did not release the owned slow fixture"
        if mode == "unavailable":
            raise RuntimeError("Original CC0 browser generator deliberately unavailable")
        if mode == "sources_changed":
            assert self.change_source is not None
            self.change_source()
        answerable = mode != "abstained"
        text = "Запоздалый ответ старого вопроса" if mode == "slow" else CLAIM
        if mode == "literal":
            text += " " + LITERAL
        result = {
            "answerable": answerable,
            "claims": []
            if not answerable
            else [{"text": text, "evidence_ids": ["S999:E1" if mode == "invalid_output" else evidence_id]}],
        }
        return {
            "text": json.dumps(result, ensure_ascii=False),
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "model": {"id": "original-cc0-browser-fake", "revision": "fixture-v1"},
            "elapsed_ms": 1,
        }


def ask(page: Page, question: str = QUESTION) -> tuple[dict[str, Any], dict[str, Any]]:
    page.locator("#answer-query").fill(question)
    with (
        page.expect_response(lambda response: urlsplit(response.url).path == "/api/ask") as response,
        page.expect_request(lambda request: urlsplit(request.url).path == "/api/ask") as request,
    ):
        page.locator("#run-answer").click()
    assert response.value.ok, response.value.text()
    payload = request.value.post_data_json
    assert isinstance(payload, dict)
    return response.value.json(), payload


def run_fixture(directory: Path, artifacts: Path) -> dict[str, Any]:
    library = Library(directory / "library")
    source = directory / "original-answer.html"
    source.write_text(original_text(), encoding="utf-8")
    metadata = {
        "id": BOOK_ID,
        "title": "Янтарная закладка · оригинальный тест CC0",
        "author": "Book Atlas browser fixture",
        "language": "ru",
        "edition": "Первая редакция · CC0",
        "source_url": "https://example.org/book-atlas/answer-browser-fixture",
        "rights": "CC0 1.0 · original Book Atlas browser fixture",
        "rights_url": "https://creativecommons.org/publicdomain/zero/1.0/",
    }
    assert library.import_book(source, metadata)["status"] == "indexed"
    english = directory / "english-answer.txt"
    english.write_text("An original CC0 reader keeps a bronze bookmark to remember a passage.")
    assert (
        library.import_book(
            english,
            {**metadata, "id": "answer-english-cc0", "title": "Bronze bookmark CC0", "language": "en"},
        )["status"]
        == "indexed"
    )
    fake = FakeGenerator()

    def replace_source() -> None:
        source.write_text(original_text("вторая"), encoding="utf-8")
        assert library.import_book(source, metadata | {"edition": "Вторая редакция · CC0"})["status"] == "indexed"

    fake.change_source = replace_source
    app = create_app(library.data_dir, answer_generator=fake)
    errors: list[str] = []
    statuses: list[str] = []
    with (
        patch("browser_archive.create_app", return_value=app),
        fixture_server(library.data_dir) as base,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1100})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/?panel=answer&q=закладка&scope=missing-scope-cc0", wait_until="networkidle")
        expect(page.locator("#run-answer")).to_be_disabled()
        expect(page.locator("#answer-scope")).to_contain_text("не подтверждена")
        assert not fake.calls, "A failed scope must not broaden to the whole library"
        page.locator("#book-scope-selection").get_by_role("button", name="Снять ограничение").click()
        expect(page.locator("#run-answer")).to_be_enabled()
        assert not fake.calls
        page.goto(base, wait_until="networkidle")
        page.locator("#open-answer").click()
        expect(page.locator("#answer-panel")).to_be_visible()
        expect(page.locator("#answer-query")).to_be_focused()
        assert not fake.calls
        page.locator("#answer-query").fill(QUESTION)
        page.locator("#save-query").click()
        expect(page.locator("#save-title")).to_have_value(QUESTION)
        page.locator("#cancel-save").click()
        expect(page.locator("#save-dialog")).not_to_be_visible()
        assert not fake.calls
        data, request = ask(page)
        statuses.append(data["status"])
        assert data["status"] == "answered", data
        assert len(fake.judge_calls) == 1
        assert data["generation"]["grounding_check"]["supported"] == [True]
        assert request == {"query": QUESTION, "book_ids": [], "language": None, "mode": "hybrid", "limit": 8}
        expect(page.locator(".answer-claim")).to_contain_text(CLAIM)
        expect(page.locator(".answer-claim .verified-label")).to_have_count(0)
        supporting_quotes = [support for claim in data["claims"] for support in claim["supporting_quotes"]]
        assert page.locator(".answer-support-quote").all_text_contents() == [
            support["quote"] for support in supporting_quotes
        ]
        expect(page.locator(".answer-support .verified-label")).to_have_count(len(supporting_quotes))
        for support in supporting_quotes:
            original = next(source for source in data["sources"] if source["source_id"] == support["source_id"])
            assert support["quote"] in original["text"]
        expect(page.locator(".answer-verification-note")).to_contain_text("точному тексту фрагмента")
        chip = page.locator(".answer-source-chip").first
        alias = chip.inner_text()
        chip.click()
        card = page.locator("#answer-source-" + alias)
        expect(card).to_be_focused()
        expect(card.locator(".verified-label")).to_have_text("✓ ЦИТАТА ПРОВЕРЕНА")
        quote = card.locator("blockquote").inner_text()
        support_quote = supporting_quotes[0]["quote"]
        assert support_quote in quote
        assert any(source["text"] == quote and source["verified"] for source in data["sources"])
        count = len(fake.calls)
        card.get_by_role("button", name="Открыть в контексте").click()
        expect(page.locator("#found-passage")).to_have_text(quote)
        assert support_quote in page.locator("#found-passage").inner_text()
        page.locator("#reader-back").click()
        expect(page.locator("#answer-panel")).to_be_visible()
        expect(page.locator(".answer-claim")).to_contain_text(CLAIM)
        page.go_back()
        expect(page.locator("#reader-view")).to_be_visible()
        page.go_back()
        expect(page.locator("#answer-panel")).to_be_visible()
        assert len(fake.calls) == count, "Reader/back/history started generation"
        page.reload(wait_until="networkidle")
        expect(page.locator("#answer-query")).to_have_value(QUESTION)
        expect(page.locator(".answer-claim")).to_have_count(0)
        assert len(fake.calls) == count, "Refreshing an answer link started generation"

        page.locator("#language-filter").select_option("ru")
        page.locator("#book-scope-query").fill("Янтарная закладка")
        expect(page.locator(f'#book-filter option[value="{BOOK_ID}"]')).to_have_count(1)
        page.locator("#book-filter").select_option(BOOK_ID)
        expect(page.locator("#answer-panel")).to_be_visible()
        expect(page.locator("#answer-scope")).to_contain_text(metadata["title"])
        assert len(fake.calls) == count, "Filters started generation"
        data, request = ask(page)
        assert data["status"] == "answered" and request["book_ids"] == [BOOK_ID] and request["language"] == "ru"
        count = len(fake.calls)
        page.reload(wait_until="networkidle")
        expect(page.locator("#book-filter")).to_have_value(BOOK_ID)
        expect(page.locator("#language-filter")).to_have_value("ru")
        assert len(fake.calls) == count, "Scope deep link started generation"

        fake.mode = "literal"
        data, _ = ask(page)
        assert data["status"] == "answered", data
        expect(page.locator(".answer-claim")).to_contain_text(LITERAL)
        expect(page.locator("#answer-output img")).to_have_count(0)
        assert page.evaluate("window.answerInjected === undefined")
        page.locator(".results-main").screenshot(path=str(artifacts / "answer-desktop.png"))
        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Answer overflows {width}px"
        page.locator(".results-main").screenshot(path=str(artifacts / "answer-mobile.png"))
        page.set_viewport_size({"width": 1440, "height": 1100})

        for mode, title in (
            ("abstained", "Недостаточно оснований для ответа"),
            ("invalid_output", "Ответ не прошёл проверку"),
            ("unavailable", "Ответ пока недоступен"),
        ):
            fake.mode = mode
            data, _ = ask(page)
            statuses.append(data["status"])
            assert data["status"] == mode, data
            expect(page.locator("#answer-output .empty-state h3")).to_have_text(title)
            expect(page.locator(".answer-claim")).to_have_count(0)
            expect(page.locator("#answer-query")).to_have_value(QUESTION)
        fake.mode = "judge_false"
        before_judgement = len(fake.judge_calls)
        data, _ = ask(page)
        statuses.append(data["status"])
        assert data["status"] == "abstained" and data["claims"] == [], data
        assert len(fake.judge_calls) == before_judgement + 1
        assert data["sources"] and all(source["verified"] for source in data["sources"])
        expect(page.locator("#answer-output .empty-state h3")).to_have_text("Недостаточно оснований для ответа")
        expect(page.locator(".answer-claim")).to_have_count(0)
        expect(page.locator(".answer-support-quote")).to_have_count(0)
        expect(page.locator("#answer-output .passage-card").first).to_be_visible()
        page.locator(".results-main").screenshot(path=str(artifacts / "answer-rejected-claim.png"))
        fake.mode = "answered"
        count = len(fake.calls)
        judge_count = len(fake.judge_calls)
        data, _ = ask(page, "невозможныйкварцзапросбездоказательств")
        assert data["status"] == "abstained" and len(fake.calls) == count
        assert len(fake.judge_calls) == judge_count

        fake.mode = "slow"
        fake.started.clear()
        fake.release.clear()
        page.locator("#answer-query").fill(QUESTION)
        page.locator("#run-answer").click()
        expect(page.locator("#answer-panel")).to_have_attribute("aria-busy", "true")
        assert fake.started.wait(3)
        page.locator("#answer-query").fill(QUESTION + " Почему?")
        expect(page.locator("#run-answer")).to_be_enabled()
        fake.release.set()
        page.wait_for_timeout(250)
        expect(page.locator(".answer-claim")).to_have_count(0)
        expect(page.locator("#answer-status")).to_contain_text("Вопрос изменён")
        fake.mode = "answered"
        data, _ = ask(page)
        assert data["status"] == "answered"
        expect(page.locator(".answer-claim")).not_to_contain_text("Запоздалый")

        fake.mode = "slow"
        fake.started.clear()
        fake.release.clear()
        page.locator("#run-answer").click()
        assert fake.started.wait(3)
        page.locator("#cancel-answer").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#answer-query")).to_be_focused()
        expect(page.locator("#run-answer")).to_be_enabled()
        fake.release.set()
        page.wait_for_timeout(250)
        expect(page.locator(".answer-claim")).to_have_count(0)
        fake.mode = "answered"

        context.set_offline(True)
        page.locator("#answer-query").fill(QUESTION + " Проверка связи.")
        page.locator("#run-answer").click()
        expect(page.locator("#answer-status")).to_contain_text("Не удалось получить ответ")
        expect(page.locator("#answer-query")).to_have_value(QUESTION + " Проверка связи.")
        expect(page.locator("#run-answer")).to_be_enabled()
        context.set_offline(False)

        fake.mode = "sources_changed"
        data, _ = ask(page)
        statuses.append(data["status"])
        assert data["status"] == "sources_changed", data
        expect(page.locator("#answer-output .empty-state h3")).to_have_text(
            "Источники изменились во время подготовки ответа"
        )
        expect(page.locator(".answer-claim")).to_have_count(0)
        expect(page.locator("#answer-output .passage-card")).to_have_count(0)
        expect(page.locator("#answer-coverage")).to_be_hidden()
        fake.mode = "answered"
        data, _ = ask(page)
        assert data["status"] == "answered"
        assert all(source["verified"] for source in data["sources"])
        page.locator(".results-main").screenshot(path=str(artifacts / "answer-current-edition.png"))

        # Delay real transport, preserving the real GET book 404 and ask endpoint.
        # The failed older scope must not clear/re-enable a newer pending answer.
        network = context.new_cdp_session(page)
        network.send("Network.enable")
        network.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 700, "downloadThroughput": -1, "uploadThroughput": -1},
        )
        page.goto(base + "/?panel=answer&q=закладка&scope=slow-missing-scope-cc0", wait_until="domcontentloaded")
        expect(page.locator("#answer-scope")).to_have_text("Проверяем выбранную книгу из ссылки…")
        page.locator("#book-scope-selection").get_by_role("button", name="Снять ограничение").click()
        fake.mode = "slow"
        fake.started.clear()
        fake.release.clear()
        page.locator("#answer-query").fill(QUESTION)
        page.locator("#run-answer").click()
        assert fake.started.wait(3)
        expect(page.locator("#run-answer")).to_be_disabled()
        expect(page.locator("#answer-panel")).to_be_visible()
        expect(page.locator("#book-filter")).to_have_value("")
        expect(page.locator("#answer-output")).to_be_empty()
        fake.release.set()
        expect(page.locator(".answer-claim")).to_contain_text("Запоздалый ответ старого вопроса")
        expect(page.locator("#run-answer")).to_be_enabled()
        count = len(fake.calls)
        page.goto(base + f"/?panel=answer&q=закладка&scope={BOOK_ID}", wait_until="domcontentloaded")
        expect(page.locator("#answer-scope")).to_have_text("Проверяем выбранную книгу из ссылки…")
        page.locator('.site-header nav [data-view="library"]').click()
        expect(page.locator("#library-view")).to_be_visible()
        page.go_back()
        expect(page.locator("#answer-panel")).to_be_visible()
        expect(page.locator("#book-filter")).to_have_value(BOOK_ID)
        expect(page.locator("#answer-scope")).to_contain_text(metadata["title"])
        expect(page.locator("#run-answer")).to_be_enabled()
        assert len(fake.calls) == count, "Retrying a canceled scope lookup started generation"
        network.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1},
        )
        network.detach()
        assert not errors, errors
        context.close()
        browser.close()
    return {
        "result": "passed",
        "backend": "real ask API/retrieval/validator; deterministic generator only",
        "fixture": "owned two-edition CC0 temporary library; no live writes or model inference",
        "statuses": statuses,
        "generator_calls": len(fake.calls),
        "judge_calls": len(fake.judge_calls),
        "workflows": [
            "explicit_submit_only",
            "claim_alias_exact_source_reader_unicode",
            "supporting_quote_matches_api_source_and_reader",
            "back_refresh_and_scope_deep_link_without_generation",
            "failed_scope_blocks_until_explicit_removal",
            "late_scope_failure_cannot_change_new_request",
            "back_retries_canceled_valid_scope_lookup",
            "scoped_language_payload",
            "literal_markup_is_text",
            "abstention_invalid_output_unavailable",
            "judge_rejection_abstains_preserving_verified_fragments",
            "empty_retrieval_skips_generator",
            "edited_question_aborts_stale_response",
            "keyboard_cancel_returns_focus",
            "offline_failure_retains_question",
            "changed_sources_suppress_answer",
            "mobile_320_390_no_overflow",
        ],
        "page_errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/browser-answer"))
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="book-atlas-answer-browser-") as temporary:
        report = run_fixture(Path(temporary), args.artifacts)
    (args.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
