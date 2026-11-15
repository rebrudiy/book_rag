"""Opt-in Ask planning through real temporary CC0 source/reader pipelines.

Only generation is substituted. No local model, production database or sealed
fixture is used; model quality is a separate open-development measurement.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from book_rag import answers, query_planning
from book_rag.api import create_app
from book_rag.library import Library
from book_rag.local_generation import GenerationLimits

TEXT = (
    "Mira tells Oleg to place the amber bookmark on shelf seven. "
    "Oleg observes that the copper bookmark already rests near the window. "
    "PRIVATE_CC0_SOURCE_SENTENCE must never enter the planning prompt."
)
META = {
    "id": "cc0-planning",
    "title": "Original CC0 workshop observations",
    "author": "Original CC0 author",
    "language": "en",
    "edition": "CC0 integration edition1",
    "work_id": "cc0-work",
    "source_url": "https://example.org/cc0/planning",
    "rights": "CC0-1.0",
    "rights_url": "https://creativecommons.org/publicdomain/zero/1.0/",
}


class PlannedFake:
    def __init__(
        self,
        *,
        hint: str = "en",
        plan_error: Exception | None = None,
        invalid: bool = False,
        empty_plan_results: bool = False,
        on_plan: Any = None,
        on_writer: Any = None,
        clock: dict[str, float] | None = None,
    ) -> None:
        self.hint = hint
        self.plan_error = plan_error
        self.invalid = invalid
        self.empty_plan_results = empty_plan_results
        self.on_plan = on_plan
        self.on_writer = on_writer
        self.clock = clock
        self.calls: list[dict[str, Any]] = []

    def generate(
        self, messages: list[dict[str, str]], limits: GenerationLimits | None = None, *, response_schema: Any = None
    ) -> dict[str, Any]:
        payload = json.loads(messages[-1]["content"])
        kind = (
            "plan"
            if "books" in payload and "max_queries" in payload
            else "judge"
            if payload.get("task") == "verify_claims"
            else "writer"
        )
        self.calls.append(
            {"kind": kind, "payload": payload, "messages": messages, "limits": limits, "schema": response_schema}
        )
        if kind == "plan":
            if self.plan_error:
                raise self.plan_error
            if self.on_plan:
                self.on_plan()
            if self.clock is not None:
                self.clock["now"] += 12
            output = {
                "response_language": self.hint,
                "books": [
                    {
                        "book_id": book["id"],
                        "queries": ["nonexistent query words" if self.empty_plan_results else "amber bookmark"],
                    }
                    for book in payload["books"]
                ],
            }
            if self.invalid:
                output["books"][0]["queries"] = ["PRIVATE_REJECTED_MODEL_QUERY”, "]
        elif kind == "judge":
            if self.clock is not None:
                self.clock["now"] += 3
            output = {"supported": [True] * len(payload["claims_to_verify"])}
        else:
            if self.on_writer:
                self.on_writer()
            if self.clock is not None:
                self.clock["now"] += 4
            evidence = payload["sources"][0]["evidence"][0]["evidence_id"]
            output = {
                "answerable": True,
                "claims": [
                    {
                        "text": "Мира обращается к Олегу." if self.hint == "ru" else "Mira tells Oleg what to do.",
                        "evidence_ids": [evidence],
                    }
                ],
            }
        return {
            "text": json.dumps(output, ensure_ascii=False),
            "finish_reason": "stop",
            "model": {"id": "explicit-test-double"},
            "elapsed_ms": 1,
        }


@pytest.fixture
def library(tmp_path: Path) -> Library:
    lib = Library(tmp_path / "library")
    source = tmp_path / "original.txt"
    source.write_text(TEXT)
    assert lib.import_book(source, META)["status"] == "indexed"
    return lib


def test_default_empty_evidence_never_calls_planner_or_writer_and_prompt_is_unchanged(library: Library) -> None:
    fake = PlannedFake()
    missing = answers.ask(library, "янтарная закладка", book_ids=[META["id"]], mode="lexical", generator=fake)
    assert missing["status"] == "abstained" and fake.calls == []
    assert missing["retrieval"]["planning"]["reason"] == "disabled"
    assert missing["retrieval"]["planning"]["requested"] is False
    response = answers.ask(library, "amber bookmark", book_ids=[META["id"]], mode="lexical", generator=fake)
    assert response["status"] == "answered" and [c["kind"] for c in fake.calls] == ["writer", "judge"]
    assert fake.calls[0]["messages"][0]["content"] == answers.SYSTEM_PROMPT
    assert set(fake.calls[0]["payload"]) == {"question", "sources"}
    assert answers.OPT_IN_FIDELITY_INSTRUCTIONS not in fake.calls[1]["messages"][0]["content"]


def test_real_cc0_opt_in_recovers_cross_language_source_and_only_hint_enters_writer(library: Library) -> None:
    question = "Кому Мира просит положить янтарную закладку на седьмую полку?"
    fake = PlannedFake(hint="ru")
    result = answers.ask(
        library, question, book_ids=[META["id"]], language="en", mode="lexical", generator=fake, plan_retrieval=True
    )
    assert result["status"] == "answered" and [c["kind"] for c in fake.calls] == ["plan", "writer", "judge"]
    planner, writer, judge = fake.calls
    assert planner["payload"]["question"] == question
    assert planner["payload"]["books"] == [{key: META[key] for key in ["id", "title", "author", "language"]}]
    assert "PRIVATE_CC0_SOURCE_SENTENCE" not in json.dumps(planner["messages"])
    assert planner["payload"]["max_queries_per_book"] == 1 and planner["limits"].max_tokens == 384
    assert planner["limits"].timeout_seconds == 20
    assert writer["payload"]["question"] == question and writer["payload"]["response_language_hint"] == "ru"
    assert set(writer["payload"]) == {"question", "sources", "response_language_hint"}
    assert "books" not in writer["payload"] and "queries" not in writer["payload"]
    assert writer["payload"]["sources"][0]["book_author"] == META["author"]
    assert writer["payload"]["sources"][0]["evidence"][0]["text"] == TEXT
    assert answers.OPT_IN_FIDELITY_INSTRUCTIONS in writer["messages"][0]["content"]
    assert answers.OPT_IN_FIDELITY_INSTRUCTIONS in judge["messages"][0]["content"]
    planning = result["retrieval"]["planning"]
    assert planning["requested"] and planning["used"] and planning["reason"] == "validated_plan_candidates"
    assert (
        planning["ranking_strategy"] == "body-coverage-proximity-v2-local32"
        and planning["response_language_hint"] == "ru"
    )
    assert planning["book_queries"] == [{"book_id": META["id"], "language": "en", "queries": ["amber bookmark"]}]
    assert all(
        source["book_id"] == META["id"] and source["language"] == "en" and source["verified"]
        for source in result["sources"]
    )
    quote = result["claims"][0]["supporting_quotes"][0]
    source = result["sources"][0]
    assert quote["quote"] == source["text"][quote["start"] - source["start"] : quote["end"] - source["start"]]


@pytest.mark.parametrize("flag", ["true", "false", 1, 0, None, [], {}])
def test_api_plan_flag_is_strict_boolean_before_any_generation(library: Library, flag: Any) -> None:
    fake = PlannedFake()
    with TestClient(create_app(library.data_dir, answer_generator=fake)) as client:
        response = client.post(
            "/api/ask", json={"query": "amber bookmark", "book_ids": [META["id"]], "plan_retrieval": flag}
        )
    assert response.status_code == 422 and fake.calls == []


def test_api_actual_opt_in_contract_and_reader(library: Library) -> None:
    fake = PlannedFake(hint="ru")
    with TestClient(create_app(library.data_dir, answer_generator=fake)) as client:
        response = client.post(
            "/api/ask",
            json={
                "query": "янтарная закладка",
                "book_ids": [META["id"]],
                "language": "en",
                "mode": "lexical",
                "plan_retrieval": True,
            },
        )
        result = response.json()
        assert response.status_code == 200 and result["status"] == "answered"
        assert (
            result["retrieval"]["planning"]["used"]
            and client.get(result["sources"][0]["reader_url"]).status_code == 200
        )
    assert [c["kind"] for c in fake.calls] == ["plan", "writer", "judge"]


@pytest.mark.parametrize("scopes", [None, [], ["unknown"], [META["id"], "unknown"]])
def test_global_or_unknown_selected_scope_never_plans(
    library: Library, scopes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = []

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        attempts.append(True)
        raise AssertionError("scope must not reach planner")

    monkeypatch.setattr(query_planning, "plan_queries", forbidden)
    fake = PlannedFake()
    result = answers.ask(
        library, "amber bookmark", book_ids=scopes, mode="lexical", generator=fake, plan_retrieval=True
    )
    assert attempts == [] and all(c["kind"] != "plan" for c in fake.calls)
    plan = result["retrieval"]["planning"]
    assert plan["requested"] and not plan["used"] and plan["book_queries"] == []
    assert plan["reason"] == ("no_explicit_selected_scope" if not scopes else "selected_scope_unavailable")
    if scopes:
        assert all(s["book_id"] in scopes for s in result["sources"])


@pytest.mark.parametrize("failure", ["runtime", "invalid", "helper"])
def test_optional_failure_retains_original_sources_without_private_output(
    library: Library, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    original = library.search("amber bookmark", "lexical", META["id"], limit=40)
    fake = PlannedFake(
        plan_error=RuntimeError("PRIVATE_NATIVE_ERROR") if failure == "runtime" else None, invalid=failure == "invalid"
    )
    if failure == "helper":

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("PRIVATE_HELPER_ERROR")

        monkeypatch.setattr(query_planning, "retrieve_plan_candidates", fail)
    result = answers.ask(
        library, "amber bookmark", book_ids=[META["id"]], mode="lexical", generator=fake, plan_retrieval=True
    )
    assert result["status"] == "answered" and not result["retrieval"]["planning"]["used"]
    assert result["retrieval"]["planning"]["book_queries"] == []
    assert result["sources"][0]["id"] == original["results"][0]["id"]
    assert "PRIVATE_NATIVE_ERROR" not in json.dumps(result) and "PRIVATE_REJECTED_MODEL_QUERY" not in json.dumps(result)
    assert "PRIVATE_HELPER_ERROR" not in json.dumps(result)


def test_opted_planner_can_run_on_empty_retrieval_but_writer_requires_sources(library: Library) -> None:
    fake = PlannedFake(hint="ru", empty_plan_results=True)
    result = answers.ask(
        library, "неизвестные слова", book_ids=[META["id"]], mode="lexical", generator=fake, plan_retrieval=True
    )
    assert result["status"] == "abstained" and not result["sources"]
    assert [c["kind"] for c in fake.calls] == ["plan"]


def test_changed_selected_edition_during_plan_discards_stale_original_evidence(library: Library) -> None:
    def change() -> None:
        with library.connect() as db:
            db.execute("UPDATE books SET title=? WHERE id=?", ("Changed source title", META["id"]))

    fake = PlannedFake(on_plan=change)
    result = answers.ask(
        library, "amber bookmark", book_ids=[META["id"]], mode="lexical", generator=fake, plan_retrieval=True
    )
    assert result["status"] == "abstained" and result["sources"] == []
    assert result["retrieval"]["planning"]["reason"] == "selected_scope_changed"
    assert [c["kind"] for c in fake.calls] == ["plan"]


def test_source_hydration_and_post_writer_guards_still_apply(library: Library) -> None:
    def corrupt() -> None:
        with library.connect() as db:
            db.execute("UPDATE passages SET text='Changed without matching SHA'")

    fake = PlannedFake(on_writer=corrupt)
    result = answers.ask(
        library, "amber bookmark", book_ids=[META["id"]], mode="lexical", generator=fake, plan_retrieval=True
    )
    assert result["status"] == "sources_changed" and not result["claims"] and not result["sources"]
    assert [c["kind"] for c in fake.calls] == ["plan", "writer"]


def test_planner_writer_and_judge_share_remaining45_second_budget(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(answers.time, "monotonic", lambda: clock["now"])
    fake = PlannedFake(clock=clock)
    result = answers.ask(
        library, "amber bookmark", book_ids=[META["id"]], mode="lexical", generator=fake, plan_retrieval=True
    )
    assert result["status"] == "answered"
    planner, writer, judge = fake.calls
    assert planner["limits"].timeout_seconds == 20
    assert writer["limits"].timeout_seconds == 33 and judge["limits"].timeout_seconds == 29
    assert clock["now"] == 19 and result["elapsed_ms"] == 19000


def test_initial_search_responses_not_mutated_and_two_per_book_cap_uses_helper_order(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = library.data_dir / "four-rooms.html"
    source.write_text(
        "<h1>Original CC0 room markers</h1>"
        + "".join(
            f"<h2>{color} room</h2><p>Mira tells Oleg to put the {color} bookmark beside the marker.</p>"
            for color in ("red", "copper", "ivory", "amber")
        )
    )
    assert library.import_book(source, META)["status"] == "indexed"
    initial = library.search("marker", "lexical", META["id"], limit=40)
    assert len(initial["results"]) == 4
    # Substitute only the already-canonical original ordering, not the planned
    # search or helper: demonstrate that the original two-source cap would miss it.
    initial["results"].sort(key=lambda row: "amber" in row["text"])
    wanted = next(row for row in initial["results"] if "amber" in row["text"])
    assert initial["results"].index(wanted) >= 2
    original = copy.deepcopy(initial)
    actual_search = library.search

    def search(query: str, mode: str, scope: str | None, language: str | None, limit: int) -> dict[str, Any]:
        return initial if query == "marker" else actual_search(query, mode, scope, language, limit)

    monkeypatch.setattr(library, "search", search)
    result = answers.ask(
        library, "marker", book_ids=[META["id"]], mode="lexical", generator=PlannedFake(), plan_retrieval=True
    )
    assert result["status"] == "answered" and initial == original
    assert len(result["sources"]) == 2 and all(s["verified"] for s in result["sources"])
    assert result["sources"][0]["id"] == wanted["id"]


def test_late_optional_planner_does_not_reset_expired_shared_generation_budget(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(answers.time, "monotonic", lambda: clock["now"])
    fake = PlannedFake(on_plan=lambda: clock.update(now=46.0))
    result = answers.ask(
        library, "amber bookmark", book_ids=[META["id"]], mode="lexical", generator=fake, plan_retrieval=True
    )
    assert result["status"] == "unavailable" and result["sources"]
    assert result["retrieval"]["planning"]["reason"] == "planning_deadline_exceeded"
    assert [call["kind"] for call in fake.calls] == ["plan"]


@pytest.mark.parametrize("flag", ["true", 1, None])
def test_direct_ask_rejects_nonboolean_planning_flag(library: Library, flag: Any) -> None:
    fake = PlannedFake()
    with pytest.raises(ValueError, match="strict boolean"):
        answers.ask(library, "amber bookmark", generator=fake, plan_retrieval=flag)
    assert fake.calls == []


@pytest.mark.parametrize("stage", ["plan", "writer"])
def test_opt_in_cannot_release_source_that_changes_outside_language_filter(library: Library, stage: str) -> None:
    def change() -> None:
        with library.connect() as db:
            db.execute('UPDATE books SET language="ru" WHERE id=?', (META["id"],))

    fake = PlannedFake(on_plan=change if stage == "plan" else None, on_writer=change if stage == "writer" else None)
    result = answers.ask(
        library,
        "amber bookmark",
        book_ids=[META["id"]],
        language="en",
        mode="lexical",
        generator=fake,
        plan_retrieval=True,
    )
    assert result["status"] == ("abstained" if stage == "plan" else "sources_changed")
    assert result["sources"] == [] and result["claims"] == []
