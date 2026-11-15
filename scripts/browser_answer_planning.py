"""Exercise opt-in answer planning in a real isolated API and Chromium.

Only generation is substituted, with a stage-aware deterministic generator.
Retrieval, planning validation, evidence selection, citation validation, source
guards, API transport, static UI and reader are real. The original fixture is
CC0; no production library, model, provider or saved note is accessed.

Run: .venv/bin/python scripts/browser_answer_planning.py
Each run creates a new artifact directory and saves its report even on failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, Request, expect, sync_playwright

from book_rag.api import create_app
from book_rag.library import Library
from book_rag.local_generation import GenerationLimits

BOOK_ID = "answer-planning-browser-cc0"
QUESTION = "Куда Мира просит Олега положить янтарную закладку?"
FACT = "Мира просит Олега положить янтарную закладку на седьмую полку."
CLAIM = "Мира просит Олега положить янтарную закладку на седьмую полку."
LATE_CLAIM = "Запоздалый ответ отменённого вопроса."
SOURCE_CANARY = "ORIGINAL_CC0_BODY_MUST_NOT_ENTER_PLANNER"
META = {
    "id": BOOK_ID,
    "work_id": "answer-planning-original-cc0-work",
    "title": "Янтарная закладка · планирование · CC0",
    "author": "Book Atlas original CC0 fixture",
    "language": "ru",
    "edition": "Оригинальный тест CC0 · редакция 1",
    "source_url": "https://example.org/book-atlas/answer-planning-cc0",
    "rights": "CC0 1.0 · original Book Atlas browser fixture",
    "rights_url": "https://creativecommons.org/publicdomain/zero/1.0/",
    "coverage": "full",
}


def original_text(edition: str = "первая") -> str:
    return (
        '<html lang="ru"><head><meta charset="utf-8"><title>Янтарная закладка CC0</title></head><body>'
        "<h1>Янтарная закладка CC0</h1><h2>Глава первая · поручение</h2>"
        f"<p>{FACT} Олег должен сохранить точные слова поручения и проверить номер полки.</p>"
        f"<p>Это {edition} редакция оригинального теста CC0. {SOURCE_CANARY}.</p>"
        "<h2>Глава вторая · наблюдение</h2><p>Олег замечает медную закладку у окна. "
        "Это наблюдение, а не просьба перенести янтарную закладку к окну.</p></body></html>"
    )


class PlanningGenerator:
    """Explicit test double for planner -> writer -> supporting-quote judge."""

    def __init__(self) -> None:
        self.mode = "answered"
        self.calls: list[dict[str, Any]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.change_source: Callable[[], None] | None = None

    def block(self, mode: str) -> None:
        self.mode = mode
        self.started.clear()
        self.release.clear()
        self.finished.clear()

    def generate(
        self,
        messages: list[dict[str, str]],
        limits: GenerationLimits | None = None,
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = json.loads(messages[-1]["content"])
        stage = (
            "plan"
            if "books" in payload and "max_queries" in payload
            else "judge"
            if payload.get("task") == "verify_claims"
            else "writer"
        )
        mode = self.mode
        self.calls.append({"stage": stage, "payload": payload, "mode": mode})
        assert response_schema is not None, "Use the actual bounded structured generation protocol"
        if stage == "plan":
            assert SOURCE_CANARY not in json.dumps(messages, ensure_ascii=False)
            assert "sources" not in payload and payload["books"]
            assert [book["id"] for book in payload["books"]] == [BOOK_ID]
            assert limits is not None and limits.max_tokens == 384 and limits.timeout_seconds <= 20
            if mode == "planner_failed":
                raise RuntimeError("Original CC0 fixture optional planner is unavailable")
            if mode == "slow_plan":
                self.started.set()
                assert self.release.wait(10), "Browser did not release its owned delayed planner"
            output = {
                "response_language": "ru",
                "books": [{"book_id": BOOK_ID, "queries": ["янтарная закладка полку"]}],
            }
        elif stage == "writer":
            assert payload["question"] and payload["sources"]
            if mode == "slow_writer":
                self.started.set()
                assert self.release.wait(10), "Browser did not release its owned delayed writer"
            if mode == "sources_changed":
                assert self.change_source is not None
                self.change_source()
            evidence = next(
                evidence for source in payload["sources"] for evidence in source["evidence"] if FACT in evidence["text"]
            )
            output = {
                "answerable": True,
                "claims": [
                    {
                        "text": LATE_CLAIM if mode in {"slow_plan", "slow_writer"} else CLAIM,
                        "evidence_ids": [evidence["evidence_id"]],
                    }
                ],
            }
        else:
            assert payload["claims_to_verify"]
            assert all(claim["quotes"] for claim in payload["claims_to_verify"])
            output = {"supported": [True] * len(payload["claims_to_verify"])}
            self.finished.set()
        return {
            "text": json.dumps(output, ensure_ascii=False),
            "finish_reason": "stop",
            "model": {"id": "stage-aware-original-cc0-browser-double", "revision": "v1"},
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "elapsed_ms": 1,
        }


@contextmanager
def fixture_server(app: FastAPI) -> Generator[str]:
    """Expose only the temporary app on an OS-assigned loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not worker.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("Temporary planning fixture server failed to start")
                time.sleep(0.02)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            worker.join(timeout=10)
            if worker.is_alive():
                server.force_exit = True
                worker.join(timeout=2)
                raise RuntimeError("Temporary planning fixture server did not stop")


def ask(page: Page, question: str = QUESTION) -> tuple[dict[str, Any], dict[str, Any]]:
    page.locator("#answer-query").fill(question)
    with (
        page.expect_response(lambda response: urlsplit(response.url).path == "/api/ask") as pending_response,
        page.expect_request(lambda request: urlsplit(request.url).path == "/api/ask") as pending_request,
    ):
        page.locator("#run-answer").click()
    response = pending_response.value
    assert response.ok, response.text()
    request = pending_request.value.post_data_json
    assert isinstance(request, dict)
    return response.json(), request


def fingerprints() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in [
            "src/book_rag/static/app.js",
            "src/book_rag/static/index.html",
            "src/book_rag/static/style.css",
            "src/book_rag/api.py",
            "src/book_rag/answers.py",
            "src/book_rag/query_planning.py",
            "scripts/browser_answer_planning.py",
        ]
    }


def run_fixture(directory: Path, artifacts: Path, report: dict[str, Any]) -> None:
    library = Library(directory / "library")
    source_path = directory / "original-planning-cc0.html"
    source_path.write_text(original_text(), encoding="utf-8")
    assert library.import_book(source_path, META)["status"] == "indexed"
    generator = PlanningGenerator()

    def change_source() -> None:
        source_path.write_text(original_text("вторая"), encoding="utf-8")
        metadata = META | {"edition": "Оригинальный тест CC0 · редакция 2"}
        assert library.import_book(source_path, metadata)["status"] == "indexed"

    generator.change_source = change_source
    app = create_app(library.data_dir, answer_generator=generator)
    errors: list[str] = []
    api_mutations: list[dict[str, Any]] = []
    report["api_mutations"] = api_mutations
    report["page_errors"] = errors
    report["generator_calls"] = generator.calls

    def capture_request(request: Request) -> None:
        path = urlsplit(request.url).path
        if request.method != "GET" and path.startswith("/api/"):
            api_mutations.append({"method": request.method, "path": path, "json": request.post_data_json})

    def record(name: str, **details: Any) -> None:
        report["cases"].append({"name": name, "passed": True, **details})
        (artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def no_automatic_ask(counts: tuple[int, int]) -> None:
        assert (len(api_mutations), len(generator.calls)) == counts, "UI action initiated an unsolicited Ask/generation"

    with fixture_server(app) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1050})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", capture_request)
        plan = page.locator("#answer-plan-retrieval")
        page.goto(base + "/?panel=answer", wait_until="networkidle")
        expect(plan).to_be_disabled()
        expect(plan).not_to_be_checked()
        expect(page.locator("#answer-scope")).to_contain_text("Вся библиотека")
        no_automatic_ask((0, 0))
        record("global_default_off_and_disabled")

        page.goto(base + "/?" + urlencode({"panel": "answer", "q": QUESTION, "scope": "missing-cc0"}))
        expect(page.locator("#answer-scope")).to_contain_text("не подтверждена")
        expect(plan).to_be_disabled()
        expect(plan).not_to_be_checked()
        expect(page.locator("#run-answer")).to_be_disabled()
        no_automatic_ask((0, 0))
        record("unconfirmed_scope_blocks_planner_and_submit")

        scoped_url = base + "/?" + urlencode({"panel": "answer", "q": QUESTION, "scope": BOOK_ID})
        page.goto(scoped_url, wait_until="networkidle")
        expect(page.locator("#book-filter")).to_have_value(BOOK_ID)
        expect(plan).to_be_enabled()
        expect(plan).not_to_be_checked()
        plan.check()
        plan.uncheck()
        page.locator("#language-filter").select_option("ru")
        expect(page.locator("#book-filter")).to_have_value(BOOK_ID)
        expect(plan).to_be_enabled()
        page.reload(wait_until="networkidle")
        expect(page.locator("#book-filter")).to_have_value(BOOK_ID)
        expect(page.locator("#language-filter")).to_have_value("ru")
        expect(plan).to_be_enabled()
        no_automatic_ask((0, 0))
        record("known_scope_toggle_filter_reload_never_generate")

        plan.check()
        response, payload = ask(page)
        assert response["status"] == "answered", response
        assert payload == {
            "query": QUESTION,
            "book_ids": [BOOK_ID],
            "language": "ru",
            "mode": "hybrid",
            "limit": 8,
            "plan_retrieval": True,
        }, payload
        assert [call["stage"] for call in generator.calls] == ["plan", "writer", "judge"]
        planning = response["retrieval"]["planning"]
        assert planning["requested"] is True and planning["used"] is True, planning
        assert planning["response_language_hint"] == "ru"
        expect(page.locator(".answer-claim")).to_contain_text(CLAIM)
        supports = [q for claim in response["claims"] for q in claim["supporting_quotes"]]
        assert page.locator(".answer-support-quote").all_text_contents() == [q["quote"] for q in supports]
        for q in supports:
            s = next(s for s in response["sources"] if s["source_id"] == q["source_id"])
            assert q["quote"] == s["text"][q["start"] - s["start"] : q["end"] - s["start"]]
            assert s["book_id"] == BOOK_ID and s["verified"]
        page.locator(".results-main").screenshot(path=str(artifacts / "planned-answer.png"))
        record("explicit_true_real_plan_writer_judge_canonical_quotes", request=payload, planning=planning)

        counts = len(api_mutations), len(generator.calls)
        alias = response["claims"][0]["source_ids"][0]
        page.locator(".answer-source-chip").first.click()
        card = page.locator("#answer-source-" + alias)
        expect(card).to_be_focused()
        expected_text = next(s["text"] for s in response["sources"] if s["source_id"] == alias)
        card.get_by_role("button", name="Открыть в контексте").click()
        expect(page.locator("#found-passage")).to_have_text(expected_text)
        assert supports[0]["quote"] in page.locator("#found-passage").inner_text()
        page.locator("#reader-back").click()
        expect(page.locator(".answer-claim")).to_contain_text(CLAIM)
        page.go_back()
        expect(page.locator("#reader-view")).to_be_visible()
        page.go_back()
        expect(page.locator("#answer-panel")).to_be_visible()
        page.reload(wait_until="networkidle")
        expect(page.locator("#book-filter")).to_have_value(BOOK_ID)
        expect(plan).to_be_enabled()
        expect(page.locator(".answer-claim")).to_have_count(0)
        no_automatic_ask(counts)
        record("canonical_reader_history_reload_never_generate")

        plan.uncheck()
        stages_before = len(generator.calls)
        response, payload = ask(page)
        assert response["status"] == "answered" and "plan_retrieval" not in payload
        assert [c["stage"] for c in generator.calls[stages_before:]] == ["writer", "judge"]
        assert response["retrieval"]["planning"]["requested"] is False
        record("explicit_false_omits_flag_and_never_calls_planner", request=payload)

        counts = len(api_mutations), len(generator.calls)
        plan.check()
        expect(page.locator(".answer-claim")).to_have_count(0)
        expect(page.locator("#answer-status")).to_contain_text("Настройка поиска изменилась")
        no_automatic_ask(counts)
        record("changing_option_invalidates_old_answer_without_generation")

        generator.mode = "planner_failed"
        stages_before = len(generator.calls)
        response, _ = ask(page)
        assert response["status"] == "answered", response
        assert [c["stage"] for c in generator.calls[stages_before:]] == ["plan", "writer", "judge"]
        planning = response["retrieval"]["planning"]
        assert planning["requested"] is True and planning["used"] is False
        expect(page.locator(".answer-claim")).to_contain_text(CLAIM)
        expect(page.locator("#answer-status")).to_contain_text("Ответ готов")
        visible_notice = page.locator("#answer-warnings").inner_text()
        understandable_fallback = "исходн" in visible_notice.lower() or "уточни" in visible_notice.lower()
        if not understandable_fallback:
            report["findings"].append(
                {
                    "case": "optional_planner_failure",
                    "message": (
                        "Answer succeeds using original retrieval, but the visible UI does not explain "
                        "that optional refinement failed."
                    ),
                    "planning_reason": planning["reason"],
                    "visible_status": page.locator("#answer-status").inner_text(),
                    "visible_warnings": visible_notice,
                }
            )
        page.locator(".results-main").screenshot(path=str(artifacts / "planner-fallback.png"))
        record("optional_planner_failure_retains_real_original_retrieval", planning=planning)
        generator.mode = "answered"

        page.set_viewport_size({"width": 390, "height": 844})
        response, _ = ask(page)
        assert response["status"] == "answered"
        expect(plan).to_be_enabled()
        expect(plan).to_be_checked()
        dimensions = page.evaluate("({scroll: document.documentElement.scrollWidth, inner: innerWidth})")
        assert dimensions["scroll"] <= dimensions["inner"], dimensions
        page.locator("#answer-form").scroll_into_view_if_needed()
        page.screenshot(path=str(artifacts / "mobile-390.png"), full_page=True)
        record("mobile_390_checked_option_and_answer_no_page_overflow", **dimensions)

        generator.block("slow_plan")
        page.locator("#run-answer").click()
        assert generator.started.wait(3)
        expect(plan).to_be_disabled()
        expect(page.locator("#cancel-answer")).to_be_visible()
        expect(page.locator("#answer-status")).to_contain_text("Уточняем поиск")
        page.locator("#cancel-answer").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#answer-query")).to_be_focused()
        expect(page.locator("#run-answer")).to_be_enabled()
        expect(plan).to_be_enabled()
        generator.release.set()
        assert generator.finished.wait(5)
        page.wait_for_timeout(200)
        expect(page.locator(".answer-claim")).to_have_count(0)
        expect(page.locator("#answer-status")).to_contain_text("Ожидание остановлено")
        record("keyboard_cancel_during_planner_discards_late_response")

        generator.block("slow_writer")
        page.locator("#run-answer").click()
        assert generator.started.wait(3)
        page.locator("#answer-query").fill(QUESTION + " Уточните поручение.")
        expect(page.locator("#run-answer")).to_be_enabled()
        expect(plan).to_be_enabled()
        generator.release.set()
        assert generator.finished.wait(5)
        page.wait_for_timeout(200)
        expect(page.locator(".answer-claim")).to_have_count(0)
        expect(page.locator("#answer-status")).to_contain_text("Вопрос изменён")
        record("question_edit_during_writer_discards_late_response")

        generator.mode = "sources_changed"
        response, _ = ask(page)
        assert response["status"] == "sources_changed", response
        expect(page.locator("#answer-output .empty-state h3")).to_have_text(
            "Источники изменились во время подготовки ответа"
        )
        expect(page.locator(".answer-claim")).to_have_count(0)
        expect(page.locator("#answer-output .passage-card")).to_have_count(0)
        expect(plan).to_be_enabled()
        page.locator(".results-main").screenshot(path=str(artifacts / "source-changed.png"))
        record("owned_source_change_suppresses_stale_answer_and_fragments")

        generator.mode = "answered"
        response, _ = ask(page)
        assert response["status"] == "answered", response
        assert all(s["edition"].endswith("2") for s in response["sources"])
        expect(page.locator(".answer-claim")).not_to_contain_text(LATE_CLAIM)
        record("fresh_explicit_submit_recovers_after_cancellation_and_source_change")

        counts = len(api_mutations), len(generator.calls)
        page.locator("#clear-filters").click()
        expect(plan).to_be_disabled()
        expect(plan).not_to_be_checked()
        expect(page.locator("#book-filter")).to_have_value("")
        no_automatic_ask(counts)
        record("clearing_scope_unchecks_and_disables_without_generation")
        assert not errors, errors
        assert all(m["method"] == "POST" and m["path"] == "/api/ask" for m in api_mutations)
        report["generator_stage_counts"] = dict(Counter(c["stage"] for c in generator.calls))
        report["cancel_semantics"] = (
            "The UI stops waiting and suppresses stale delivery; the owned fake backend may finish after release. "
            "This does not assert termination of real inference after browser cancellation."
        )
        report["model_modules_loaded"] = [name for name in ["torch", "llama_cpp"] if name in sys.modules]
        context.close()
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/browser-answer-planning"))
    arguments = parser.parse_args()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    artifacts = arguments.artifacts / stamp
    artifacts.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "kind": "isolated_actual_api_chromium_acceptance_with_stage_aware_fake_generator",
        "fixture_license": "CC0-1.0 original fixture authored for this test",
        "production_library_accessed": False,
        "real_model_calls": 0,
        "api_response_mocks": False,
        "cases": [],
        "findings": [],
        "code_before": fingerprints(),
        "artifacts": str(artifacts),
        "passed": False,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="book-atlas-answer-planning-") as temporary:
            run_fixture(Path(temporary), artifacts, report)
        report["code_after"] = fingerprints()
        assert report["code_before"] == report["code_after"], "Tested code changed during the browser run; rerun"
        assert not report["findings"], report["findings"]
        report["passed"] = True
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        (artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"passed": report["passed"], "artifacts": str(artifacts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
