"""Independent replay checks on real temporary canonical books; no model is run."""

from __future__ import annotations

import hashlib
import json
import runpy
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from book_rag.library import Library

RUNNER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/evaluate_rag_answers.py"))
TEXT = "The amber bookmark keeps the reader's place. The copper bookmark is on the desk."
META = {
    "id": "cc0-bookmark-a",
    "title": "CC0 bookmarks",
    "author": "CC0 fixture author",
    "edition": "CC0 edition 1",
    "language": "en",
    "source_url": "https://example.org/cc0-bookmark",
    "rights": "CC0-1.0",
    "rights_url": "https://creativecommons.org/publicdomain/zero/1.0/",
    "work_id": "cc0-bookmark-work",
}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def example(tmp_path: Path) -> tuple[Library, dict[str, Any], dict[str, Any]]:
    library = Library(tmp_path / "library")
    path = tmp_path / "book.txt"
    path.write_text(TEXT)
    assert library.import_book(path, META)["status"] == "indexed"
    with sqlite3.connect(library.db_path) as db:
        db.row_factory = sqlite3.Row
        book = dict(db.execute("SELECT * FROM books WHERE id=?", (META["id"],)).fetchone())
        section = dict(db.execute("SELECT * FROM sections WHERE book_id=?", (META["id"],)).fetchone())
        passage_id = db.execute("SELECT id FROM passages WHERE book_id=?", (META["id"],)).fetchone()[0]
    source = library.passage(passage_id)
    assert source is not None
    source["source_id"] = "S1"
    quote = "The amber bookmark keeps the reader's place."
    evidence = {
        "id": "amber",
        "book_id": book["id"],
        "section_ordinal": section["ordinal"],
        "section_title": section["title"],
        "section_sha256": sha(section["text"]),
        "quote": quote,
        "quote_start": 0,
        "quote_end": len(quote),
        "quote_sha256": sha(quote),
        "expected_attribution": {
            key: source.get(key) or ""
            for key in (
                "section_author",
                "section_role",
                "section_translator",
            )
        },
    }
    case = {
        "id": "amber",
        "kind": "book_factual",
        "question": "What does the amber bookmark do?",
        "book_ids": [book["id"]],
        "expected_answerability": True,
        "expected_facts": ["The bookmark keeps the reader's place."],
        "evidence": [evidence],
        "required_cited_book_ids": [book["id"]],
    }
    fixture = {
        "version": "test-dev",
        "split": "development",
        "books": [
            {
                "book_id": book["id"],
                **{k: book[k] for k in RUNNER["BOOK_FIELDS"]},
            }
        ],
        "cases": [case],
    }
    response = {
        "query": case["question"],
        "status": "answered",
        "sources": [source],
        "claims": [
            {
                "text": "The amber bookmark keeps the reader's place.",
                "source_ids": ["S1"],
                "supporting_quotes": [{"source_id": "S1", "quote": quote}],
            }
        ],
    }
    return library, fixture, response


def test_real_source_replay_checks_exact_text_but_does_not_claim_semantic_accuracy(
    example: tuple[Library, dict[str, Any], dict[str, Any]], tmp_path: Path
) -> None:
    lib, fixture, response = example
    path = tmp_path / "dev.json"
    path.write_text(json.dumps(fixture))
    loaded, digest = RUNNER["load_fixture"](path)
    assert len(digest) == 64 and loaded == fixture
    before = hashlib.sha256(lib.db_path.read_bytes()).hexdigest()
    report = RUNNER["evaluate"](fixture, lib.db_path, {"amber": response})
    assert report["fixture_validation"]["valid"]
    assert report["summary"]["observed_answerability_matches"] == 1
    assert report["summary"]["invalid_sources"] == report["summary"]["invalid_claim_citations"] == 0
    case = report["cases"][0]
    assert case["required_books_cited"]
    assert case["reference_anchor_presence"] == [{"id": "amber", "retrieved": True, "in_cited_passage": True}]
    assert case["semantic_entailment"] == case["factual_correctness"] == "unreviewed"
    assert hashlib.sha256(lib.db_path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", "A fabricated source sentence."),
        ("edition", "Invented edition"),
        ("reader_url", "/?book=other&section=1&passage=1"),
        ("reader_url", "http://[invalid"),
        ("section_author", "Invented contributor"),
        ("start", 1),
        ("verified", False),
    ],
)
def test_response_verification_flag_cannot_replace_independent_canonical_checks(
    example: tuple[Library, dict[str, Any], dict[str, Any]], field: str, value: Any
) -> None:
    lib, fixture, response = example
    response["sources"][0][field] = value
    report = RUNNER["evaluate"](fixture, lib.db_path, {"amber": response})
    assert report["summary"]["invalid_sources"] == 1
    assert report["summary"]["invalid_claim_citations"] == 1


def test_temporary_database_corruption_is_detected_independently(
    example: tuple[Library, dict[str, Any], dict[str, Any]],
) -> None:
    lib, fixture, response = example
    with sqlite3.connect(lib.db_path) as db:
        db.execute("UPDATE passages SET text='bad inline copy'")
    report = RUNNER["evaluate"](fixture, lib.db_path, {"amber": response})
    assert report["fixture_validation"]["valid"]  # Section/source snapshot is still intact.
    assert report["summary"]["invalid_sources"] == 1


def test_fabricated_supporting_quote_fails_but_false_paraphrase_requires_human_review(
    example: tuple[Library, dict[str, Any], dict[str, Any]],
) -> None:
    lib, fixture, response = example
    response["claims"][0]["supporting_quotes"][0]["quote"] = "The bookmark belongs to a dragon."
    report = RUNNER["evaluate"](fixture, lib.db_path, {"amber": response})
    assert report["summary"]["invalid_claim_citations"] == 1
    response["claims"][0]["supporting_quotes"][0]["quote"] = TEXT[:43]
    response["claims"][0]["text"] = "The amber bookmark owns the Moon."
    report = RUNNER["evaluate"](fixture, lib.db_path, {"amber": response})
    assert report["summary"]["invalid_claim_citations"] == 0
    assert report["summary"]["human_factual_reviewed_cases"] == 0
    assert report["cases"][0]["semantic_entailment"] == "unreviewed"


def test_unavailable_is_not_scored_as_correct_abstention(
    example: tuple[Library, dict[str, Any], dict[str, Any]],
) -> None:
    lib, fixture, response = example
    case = fixture["cases"][0]
    case.update(expected_answerability=False, evidence=[], required_cited_book_ids=[], expected_facts=[])
    response.update(status="unavailable", claims=[])
    report = RUNNER["evaluate"](fixture, lib.db_path, {"amber": response})
    assert report["summary"]["scored_answerability_cases"] == 0
    assert report["summary"]["observed_answerability_matches"] == 0
    assert report["summary"]["operational_failure_cases"] == 1
    response["status"] = "abstained"
    report = RUNNER["evaluate"](fixture, lib.db_path, {"amber": response})
    assert report["summary"]["observed_answerability_matches"] == 1
    assert report["summary"]["scored_answerability_cases"] == 1


def test_comparison_requires_citations_from_both_books_and_scope_escape_fails(
    example: tuple[Library, dict[str, Any], dict[str, Any]], tmp_path: Path
) -> None:
    lib, fixture, response = example
    path = tmp_path / "other.txt"
    path.write_text("The silver bookmark belongs to the visitor. It keeps another page marked.")
    meta = META | {"id": "cc0-bookmark-b", "work_id": "second-work", "title": "Other book"}
    lib.import_book(path, meta)
    with sqlite3.connect(lib.db_path) as db:
        pid = db.execute("SELECT id FROM passages WHERE book_id=?", (meta["id"],)).fetchone()[0]
    other = lib.passage(pid)
    assert other is not None
    other["source_id"] = "S2"
    response["sources"].append(other)
    case = fixture["cases"][0]
    report = RUNNER["assess_case"](case, response, lib.db_path)
    assert not report["source_checks"][1]["valid"]
    case["book_ids"].append(meta["id"])
    case["required_cited_book_ids"].append(meta["id"])
    report = RUNNER["assess_case"](case, response, lib.db_path)
    assert all(s["valid"] for s in report["source_checks"])
    assert not report["required_books_cited"]
    response["claims"][0]["source_ids"].append("S2")
    response["claims"][0]["supporting_quotes"].append({"source_id": "S2", "quote": other["text"][:50]})
    report = RUNNER["assess_case"](case, response, lib.db_path)
    assert report["required_books_cited"]
    assert report["semantic_entailment"] == "unreviewed"


def test_source_snapshot_change_stops_scoring_and_collection_before_http(
    example: tuple[Library, dict[str, Any], dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib, fixture, response = example
    fixture["books"][0]["artifact_sha256"] = "0" * 64
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("No HTTP on snapshot mismatch"))
    report = RUNNER["evaluate"](fixture, lib.db_path, {"amber": response})
    assert not report["fixture_validation"]["valid"] and not report["cases"]
    with pytest.raises(ValueError, match="snapshot mismatch"):
        RUNNER["collect"](
            fixture, "sha", lib.db_path, "http://127.0.0.1:8001", tmp_path / "response.json", allow_generation=True
        )


@pytest.mark.parametrize("url,allow", [("http://127.0.0.1:8001", False), ("https://example.org", True)])
def test_collection_needs_explicit_local_generation_opt_in(
    example: tuple[Library, dict[str, Any], dict[str, Any]],
    tmp_path: Path,
    url: str,
    allow: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib, fixture, _ = example
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("No model/network allowed"))
    with pytest.raises(ValueError):
        RUNNER["collect"](fixture, "sha", lib.db_path, url, tmp_path / "responses.json", allow_generation=allow)


def test_mocked_api_collection_is_fixture_bound_resumable_and_does_not_filter_crosslingual_sources(
    example: tuple[Library, dict[str, Any], dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib, fixture, response = example
    fixture["cases"][0]["language"] = "ru"
    fixture["cases"][0]["question"] = response["query"] = "Зачем нужна закладка?"
    calls: list[httpx.Request] = []
    client_type = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["book_ids"] == [META["id"]] and "language" not in body
        calls.append(request)
        return httpx.Response(200, json=response)

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(handler)))
    path = tmp_path / "transcript.json"
    captured = RUNNER["collect"](
        fixture, "fixture-sha", lib.db_path, "http://localhost:8001", path, allow_generation=True
    )
    assert len(calls) == 1 and captured["responses"]["amber"] == response
    again = RUNNER["collect"](
        fixture,
        "fixture-sha",
        lib.db_path,
        "http://localhost:8001",
        path,
        allow_generation=True,
        resume=True,
    )
    assert len(calls) == 1 and again["responses"] == captured["responses"]
    with pytest.raises(ValueError, match="different fixture"):
        RUNNER["collect"](
            fixture,
            "changed-sha",
            lib.db_path,
            "http://localhost:8001",
            path,
            allow_generation=True,
            resume=True,
        )


@pytest.mark.parametrize("body", [b"<html>unexpected error</html>", b"[]", b"x" * (2 * 1024 * 1024 + 1)])
def test_malformed_or_oversized_api_payload_is_an_operational_failure(
    example: tuple[Library, dict[str, Any], dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    lib, fixture, _ = example
    client_type = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(**kwargs, transport=transport))
    captured = RUNNER["collect"](
        fixture,
        "sha",
        lib.db_path,
        "http://localhost:8001",
        tmp_path / "transcript.json",
        allow_generation=True,
    )
    report = RUNNER["evaluate"](fixture, lib.db_path, captured["responses"])
    assert report["summary"]["operational_failure_cases"] == 1
    assert report["summary"]["scored_answerability_cases"] == 0


def test_only_open_development_files_and_existing_databases_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "heldout-do-not-read.json"
    path.write_text("not JSON")
    with pytest.raises(ValueError, match="development"):
        RUNNER["load_fixture"](path)
    db = tmp_path / "absent.sqlite3"
    with pytest.raises(sqlite3.OperationalError), RUNNER["canonical"](db):
        pytest.fail("Read-only missing database must not open")
    assert not db.exists()


def test_invented_literal_claim_quote_is_rejected_even_with_a_valid_support_quote(
    example: tuple[Library, dict[str, Any], dict[str, Any]],
) -> None:
    lib, fixture, response = example
    response["claims"][0]["text"] = "The source says «an invented sentence about a dragon»."
    report = RUNNER["evaluate"](fixture, lib.db_path, {"amber": response})
    assert report["summary"]["invalid_sources"] == 0
    assert report["summary"]["invalid_claim_citations"] == 1


def test_wrong_question_or_nonobject_response_gets_no_answerability_credit(
    example: tuple[Library, dict[str, Any], dict[str, Any]],
) -> None:
    lib, fixture, response = example
    response["query"] = "A different question"
    report = RUNNER["evaluate"](fixture, lib.db_path, {"amber": response})
    assert report["summary"]["scored_answerability_cases"] == 0
    assert report["summary"]["structure_error_cases"] == 1
    report = RUNNER["evaluate"](fixture, lib.db_path, {"amber": ["not an API object"]})
    assert report["summary"]["scored_answerability_cases"] == 0
    assert report["summary"]["operational_failure_cases"] == 1


def test_published_fixture_is_open_has_required_probe_types_and_quote_hashes() -> None:
    fixture, digest = RUNNER["load_fixture"](Path(__file__).resolve().parents[1] / "evals/rag-dev-v1.json")
    assert 10 <= len(fixture["cases"]) <= 16 and len(digest) == 64
    kinds = {c["kind"] for c in fixture["cases"]}
    assert {"two_book_comparison", "crosslingual_ru_to_en", "ru_original_factual", "wrong_topic_scoped"} <= kinds
    editorial = next(c for c in fixture["cases"] if c["kind"] == "editorial_attribution")
    assert editorial["evidence"][0]["expected_attribution"]["section_author"] == "Benjamin Jowett"
    assert editorial["evidence"][0]["expected_attribution"]["section_role"] == "editorial"
    assert all(c["automatic_semantic_entailment"] == "unreviewed" for c in fixture["cases"])
