#!/usr/bin/env python3
"""Open RAG development checks: exact citations are scored; entailment stays unreviewed.

The default only validates the fixture against a read-only canonical database.
Model/API collection requires an explicit local URL and --allow-generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
EXECUTED_STATUSES = {"answered", "abstained"}
BOOK_FIELDS = (
    "work_id",
    "title",
    "author",
    "language",
    "edition",
    "source_url",
    "rights",
    "rights_url",
    "artifact_sha256",
    "content_sha256",
)
SOURCE_FIELDS = (
    "book_id",
    "section_id",
    "start",
    "end",
    "text",
    "title",
    "author",
    "edition",
    "source_url",
    "rights",
    "rights_url",
    "artifact_sha256",
    "content_sha256",
    "section_title",
    "section_author",
    "section_role",
    "section_translator",
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


@contextmanager
def canonical(database: Path) -> Generator[sqlite3.Connection]:
    """Bound each read transaction; never hold a snapshot during model generation."""
    db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        yield db
    finally:
        db.rollback()
        db.close()


def load_fixture(path: Path) -> tuple[dict[str, Any], str]:
    if re.search(r"final|heldout|seal", path.name, re.I):
        raise ValueError("This runner accepts open development fixtures only")
    if path.stat().st_size > 512 * 1024:
        raise ValueError("Development fixture exceeds 512 KiB")
    raw = path.read_bytes()
    fixture = json.loads(raw)
    if not isinstance(fixture, dict) or fixture.get("split") != "development":
        raise ValueError("This runner accepts open development fixtures only")
    cases = fixture.get("cases")
    books = fixture.get("books")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 16 or not isinstance(books, list):
        raise ValueError("Expected 1–16 development cases and source snapshots")
    book_ids = [b["book_id"] for b in books]
    if len(book_ids) != len(set(book_ids)) or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Duplicate source or case IDs")
    for case in cases:
        scope = case.get("book_ids", [])
        if not scope or len(scope) > 10 or any(bid not in book_ids for bid in scope):
            raise ValueError(f"Invalid explicit book scope: {case['id']}")
        if type(case.get("expected_answerability")) is not bool:
            raise ValueError("Expected answerability must be a boolean")
        evidence = case.get("evidence")
        if not isinstance(evidence, list) or (case["expected_answerability"] and not evidence):
            raise ValueError("Answerable cases require manually audited evidence")
        for item in evidence:
            quote = item["quote"]
            if item["book_id"] not in scope or not isinstance(quote, str) or not 8 <= len(quote) <= 600:
                raise ValueError("Invalid bounded reference quotation")
            if item.get("quote_sha256") != _sha(quote):
                raise ValueError("Reference quotation hash does not match")
    return fixture, hashlib.sha256(raw).hexdigest()


def validate_fixture(fixture: dict[str, Any], database: Path) -> dict[str, Any]:
    issues: list[str] = []
    checked = 0
    with canonical(database) as db:
        for expected in fixture["books"]:
            row = db.execute("SELECT * FROM books WHERE id=? AND status='indexed'", (expected["book_id"],)).fetchone()
            if row is None:
                issues.append(f"Missing indexed source: {expected['book_id']}")
                continue
            for key in BOOK_FIELDS:
                if row[key] != expected.get(key):
                    issues.append(f"Source snapshot mismatch: {expected['book_id']}:{key}")
        for case in fixture["cases"]:
            for evidence in case["evidence"]:
                checked += 1
                row = db.execute(
                    "SELECT s.text,s.title,a.section_author,a.section_role,a.section_translator "
                    "FROM sections s LEFT JOIN section_roles a ON a.section_id=s.id "
                    "WHERE s.book_id=? AND s.ordinal=?",
                    (evidence["book_id"], evidence["section_ordinal"]),
                ).fetchone()
                label = f"{case['id']}:{evidence['id']}"
                if row is None:
                    issues.append(f"Missing evidence section: {label}")
                    continue
                start, end = evidence["quote_start"], evidence["quote_end"]
                if not (
                    type(start) is int
                    and type(end) is int
                    and 0 <= start < end <= len(row["text"])
                    and row["text"][start:end] == evidence["quote"]
                    and _sha(row["text"]) == evidence["section_sha256"]
                    and row["title"] == evidence["section_title"]
                ):
                    issues.append(f"Canonical evidence mismatch: {label}")
                for key, value in evidence["expected_attribution"].items():
                    if (row[key] or "") != value:
                        issues.append(f"Contributor mismatch: {label}:{key}")
    return {"valid": not issues, "books_checked": len(fixture["books"]), "anchors_checked": checked, "issues": issues}


def _source_check(db: sqlite3.Connection, source: dict[str, Any], scope: set[str]) -> tuple[list[str], int | None]:
    errors: list[str] = []
    passage_id = source.get("id")
    if type(passage_id) is not int:
        return ["Invalid passage ID"], None
    row = db.execute(
        "SELECT p.book_id,p.section_id,p.start,p.end,p.text,p.sha256,s.text AS section_text,"
        "s.book_id AS section_book_id,s.ordinal,s.title AS section_title,"
        "b.title,b.author,b.edition,b.source_url,b.rights,b.rights_url,b.artifact_sha256,b.content_sha256,"
        "a.section_author,a.section_role,a.section_translator "
        "FROM passages p JOIN sections s ON s.id=p.section_id JOIN books b ON b.id=p.book_id "
        "LEFT JOIN section_roles a ON a.section_id=s.id WHERE p.id=? AND b.status='indexed'",
        (passage_id,),
    ).fetchone()
    if row is None:
        return ["Missing indexed canonical passage"], None
    if row["book_id"] not in scope or row["book_id"] != row["section_book_id"]:
        errors.append("Book scope or section ownership mismatch")
    if not (
        0 <= row["start"] < row["end"] <= len(row["section_text"])
        and row["text"] == row["section_text"][row["start"] : row["end"]]
        and _sha(row["text"]) == row["sha256"]
    ):
        errors.append("Canonical passage integrity mismatch")
    if source.get("verified") is not True:
        errors.append("Source is not marked verified")
    for field in SOURCE_FIELDS:
        if source.get(field) != row[field]:
            errors.append(f"Source metadata/text mismatch: {field}")
    citation = source.get("citation")
    if not isinstance(citation, str) or not all(str(row[k]) in citation for k in ("author", "title", "edition")):
        errors.append("Missing bibliographic citation")
    contributor = row["section_author"]
    if contributor and contributor != row["author"] and (not isinstance(citation, str) or contributor not in citation):
        errors.append("Citation omits section contributor")
    translator = row["section_translator"]
    if translator and (not isinstance(citation, str) or translator not in citation):
        errors.append("Citation omits verified section translator")
    try:
        reader = urlparse(str(source.get("reader_url") or ""))
    except ValueError:
        return errors + ["Malformed reader URL"], row["ordinal"]
    args = parse_qs(reader.query, keep_blank_values=True)
    if (
        reader.scheme
        or reader.netloc
        or reader.path != "/"
        or any(
            args.get(key) != [str(value)]
            for key, value in {
                "book": row["book_id"],
                "section": row["section_id"],
                "passage": passage_id,
            }.items()
        )
    ):
        errors.append("Reader URL identity mismatch")
    return errors, row["ordinal"]


def _literal_claim_quotes(text: str, sources: list[dict[str, Any]]) -> list[str]:
    pattern = r'"([^\"]*)"|«([^»]*)»|“([^”]*)”|„([^“]*)“|‘([^’]*)’'
    errors: list[str] = []
    for match in re.finditer(pattern, text, re.DOTALL):
        quote = next((part for part in match.groups() if part), "")
        if not quote or not any(
            quote in str(source.get(field) or "")
            for source in sources
            for field in ("text", "title", "author", "section_title", "section_author", "edition")
        ):
            errors.append("Literal claim quotation is absent from cited sources/bibliography")
    if re.search('["«»“”„‘]', re.sub(pattern, "", text, flags=re.DOTALL)):
        errors.append("Unbalanced literal claim quotation")
    return errors


def assess_case(case: dict[str, Any], answer: dict[str, Any] | None, database: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": case["id"],
        "kind": case["kind"],
        "question": case["question"],
        "expected_answerability": case["expected_answerability"],
        "expected_facts_for_human_review": case["expected_facts"],
        "review_cautions": case.get("review_cautions", []),
        "factual_correctness": "unreviewed",
        "semantic_entailment": "unreviewed",
        "answer_language_appropriateness": "unreviewed",
        "status": "not_run",
        "answerability_match": None,
        "structure_errors": [],
        "source_checks": [],
        "claim_checks": [],
        "reference_anchor_presence": [],
        "required_books_cited": None,
    }
    if answer is None:
        return result
    if not isinstance(answer, dict):
        result.update(status="invalid_response", structure_errors=["Response is not an object"])
        return result
    status = answer.get("status")
    result["status"] = status
    result["generation"] = answer.get("generation")
    result["retrieval"] = answer.get("retrieval")
    result["elapsed_ms"] = answer.get("elapsed_ms")
    if answer.get("query") != case["question"]:
        result["structure_errors"].append("Question mismatch")
    sources = answer.get("sources")
    claims = answer.get("claims")
    if not isinstance(sources, list) or not all(isinstance(s, dict) for s in sources):
        result["structure_errors"].append("Invalid source array")
        sources = []
    elif len(sources) > 12:
        result["structure_errors"].append("More than twelve sources")
    if not isinstance(claims, list) or not all(isinstance(c, dict) for c in claims):
        result["structure_errors"].append("Invalid claim array")
        claims = []
    elif len(claims) > 4:
        result["structure_errors"].append("More than four claims")
    if status not in EXECUTED_STATUSES:
        result["structure_errors"].append("No usable answered/abstained response")
    elif (status == "answered") != bool(claims):
        result["structure_errors"].append("Status and claims disagree")
    else:
        result["answerability_match"] = (status == "answered") == case["expected_answerability"]
    indexed: dict[str, dict[str, Any]] = {}
    valid_sources: dict[str, dict[str, Any]] = {}
    with canonical(database) as db:
        for source in sources:
            identity = source.get("source_id")
            issues, ordinal = _source_check(db, source, set(case["book_ids"]))
            if not isinstance(identity, str) or not re.fullmatch(r"S[1-9]\d*", identity):
                issues.append("Invalid source ID")
            elif identity in indexed:
                issues.append("Repeated source ID")
            else:
                indexed[identity] = source
                if not issues:
                    valid_sources[identity] = source | {"_section_ordinal": ordinal}
            result["source_checks"].append({"source_id": identity, "valid": not issues, "issues": issues})
    cited: set[str] = set()
    for ordinal, claim in enumerate(claims):
        errors: list[str] = []
        identities = claim.get("source_ids")
        quotes = claim.get("supporting_quotes")
        text = claim.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            errors.append("Invalid claim text")
        if not isinstance(identities, list) or not identities or not all(isinstance(i, str) for i in identities):
            errors.append("Invalid cited source IDs")
            identities = []
        if len(set(identities)) != len(identities) or any(i not in valid_sources for i in identities):
            errors.append("Unknown, repeated or unverified cited source")
        if not isinstance(quotes, list) or not quotes or not all(isinstance(q, dict) for q in quotes):
            errors.append("Invalid supporting quotations")
            quotes = []
        quote_ids: set[str] = set()
        for quote in quotes:
            identity, wording = quote.get("source_id"), quote.get("quote")
            if not isinstance(identity, str) or identity not in valid_sources:
                errors.append("Supporting quote cites unknown/unverified source")
                continue
            quote_ids.add(identity)
            if (
                not isinstance(wording, str)
                or not 8 <= len(wording) <= 600
                or wording not in valid_sources[identity]["text"]
            ):
                errors.append("Supporting quote is not an exact bounded source substring")
        if quote_ids != set(identities):
            errors.append("Every and only cited sources need supporting quotations")
        if isinstance(text, str):
            errors.extend(_literal_claim_quotes(text, [valid_sources[i] for i in identities if i in valid_sources]))
        if not errors:
            cited.update(identities)
        result["claim_checks"].append({"ordinal": ordinal, "text": text, "valid": not errors, "issues": errors})
    result["required_books_cited"] = set(case["required_cited_book_ids"]) <= {
        valid_sources[i]["book_id"] for i in cited
    }
    for reference in case["evidence"]:
        matches = [
            identity
            for identity, source in valid_sources.items()
            if source["book_id"] == reference["book_id"]
            and source["_section_ordinal"] == reference["section_ordinal"]
            and reference["quote"] in source["text"]
        ]
        result["reference_anchor_presence"].append(
            {
                "id": reference["id"],
                "retrieved": bool(matches),
                "in_cited_passage": bool(set(matches) & cited),
            }
        )
    if result["structure_errors"]:
        result["answerability_match"] = None
    return result


def evaluate(fixture: dict[str, Any], database: Path, responses: dict[str, Any]) -> dict[str, Any]:
    validation = validate_fixture(fixture, database)
    if not validation["valid"]:
        return {"fixture_validation": validation, "cases": [], "summary": {"scored_cases": 0}}
    cases = [assess_case(case, responses.get(case["id"]), database) for case in fixture["cases"]]
    statuses = Counter(c["status"] for c in cases)
    scored = [c for c in cases if c["answerability_match"] is not None]
    sources = [s for c in cases for s in c["source_checks"]]
    claims = [q for c in cases for q in c["claim_checks"]]
    anchors = [a for c in cases for a in c["reference_anchor_presence"]]
    return {
        "fixture_validation": validation,
        "cases": cases,
        "summary": {
            "cases": len(cases),
            "statuses": dict(statuses),
            "scored_answerability_cases": len(scored),
            "observed_answerability_matches": sum(c["answerability_match"] for c in scored),
            "not_run_cases": statuses["not_run"],
            "operational_failure_cases": sum(
                n for s, n in statuses.items() if s not in EXECUTED_STATUSES | {"not_run"}
            ),
            "structure_error_cases": sum(bool(c["structure_errors"]) for c in cases),
            "sources_checked": len(sources),
            "invalid_sources": sum(not s["valid"] for s in sources),
            "claims_checked": len(claims),
            "invalid_claim_citations": sum(not c["valid"] for c in claims),
            "reference_anchors_observed": len(anchors),
            "reference_anchors_retrieved": sum(a["retrieved"] for a in anchors),
            "reference_anchors_in_cited_passages": sum(a["in_cited_passage"] for a in anchors),
            "human_factual_reviewed_cases": 0,
            "human_entailment_reviewed_cases": 0,
            "quality_note": (
                "Literal evidence integrity and answerability are separate from factual correctness/entailment; "
                "semantic checks remain unreviewed."
            ),
        },
    }


def collect(
    fixture: dict[str, Any],
    fixture_sha: str,
    database: Path,
    url: str,
    transcript: Path,
    *,
    allow_generation: bool,
    mode: str = "hybrid",
    limit: int = 8,
    resume: bool = False,
) -> dict[str, Any]:
    if not allow_generation:
        raise ValueError("Actual model collection requires --allow-generation")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Only an explicitly selected local answer API is supported")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Use a plain local API origin without credentials/path/query")
    validation = validate_fixture(fixture, database)
    if not validation["valid"]:
        raise ValueError("Source snapshot mismatch; collection was not started")
    config = {"mode": mode, "limit": limit, "origin": url.rstrip("/")}
    payload: dict[str, Any] = {
        "fixture_sha256": fixture_sha,
        "request_config": config,
        "created_at": datetime.now(UTC).isoformat(),
        "responses": {},
        "captures": {},
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "server_code_attestation": (
            "Unavailable; observed per-response generation/model metadata is retained, "
            "not assumed to attest loaded server code."
        ),
    }
    if transcript.exists():
        if not resume:
            raise ValueError(
                "Transcript already exists; choose a new run path or explicitly --resume the same server setup"
            )
        payload = json.loads(transcript.read_text())
        if payload.get("fixture_sha256") != fixture_sha or payload.get("request_config") != config:
            raise ValueError("Cannot resume a different fixture/request configuration")
    _save(transcript, payload)
    with httpx.Client(timeout=httpx.Timeout(60, connect=5), follow_redirects=False) as client:
        for case in fixture["cases"]:
            if case["id"] in payload["responses"]:
                continue
            request = {"query": case["question"], "book_ids": case["book_ids"], "mode": mode, "limit": limit}
            try:
                raw = bytearray()
                with client.stream("POST", config["origin"] + "/api/ask", json=request) as response:
                    response.raise_for_status()
                    for block in response.iter_bytes():
                        raw.extend(block)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise ValueError("API response exceeds bounded 2 MiB")
                answer = json.loads(raw)
                if not isinstance(answer, dict):
                    raise ValueError("API answer must be an object")
                capture = {
                    "captured_at": datetime.now(UTC).isoformat(),
                    "response_sha256": hashlib.sha256(raw).hexdigest(),
                }
            except (httpx.HTTPError, ValueError) as error:
                answer = {
                    "query": case["question"],
                    "status": "evaluation_transport_error",
                    "claims": [],
                    "sources": [],
                }
                capture = {"captured_at": datetime.now(UTC).isoformat(), "error_type": type(error).__name__}
            payload["responses"][case["id"]] = answer
            payload["captures"][case["id"]] = capture
            _save(transcript, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=Path("evals/rag-dev-v1.json"))
    parser.add_argument("--database", type=Path, default=Path("data/full/library.sqlite3"))
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--responses", type=Path, help="Replay a saved fixture-bound API transcript without any model call"
    )
    action.add_argument("--collect-url", help="Local API origin; requires --allow-generation")
    action.add_argument("--validate-only", action="store_true", help="Default: source validation only, no model calls")
    parser.add_argument("--allow-generation", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--mode", choices=["lexical", "hybrid", "semantic"], default="hybrid")
    parser.add_argument("--limit", type=int, choices=range(1, 13), default=8)
    parser.add_argument("--transcript", type=Path, default=Path("data/evaluations/rag-dev-v1/responses.json"))
    parser.add_argument("--output", type=Path, default=Path("data/evaluations/rag-dev-v1/report.json"))
    args = parser.parse_args()
    fixture, fixture_sha = load_fixture(args.fixture)
    captured: dict[str, Any] = {"responses": {}}
    if args.collect_url:
        captured = collect(
            fixture,
            fixture_sha,
            args.database,
            args.collect_url,
            args.transcript,
            allow_generation=args.allow_generation,
            mode=args.mode,
            limit=args.limit,
            resume=args.resume,
        )
    elif args.responses:
        captured = json.loads(args.responses.read_text())
        if captured.get("fixture_sha256") != fixture_sha:
            raise ValueError("Response transcript belongs to a different fixture")
    if not isinstance(captured.get("responses"), dict):
        raise ValueError("Expected a response mapping")
    if set(captured["responses"]) - {c["id"] for c in fixture["cases"]}:
        raise ValueError("Transcript contains unknown case IDs")
    report: dict[str, Any] = evaluate(fixture, args.database, captured["responses"]) | {
        "fixture": str(args.fixture),
        "fixture_sha256": fixture_sha,
        "observed_at": datetime.now(UTC).isoformat(),
        "split": "development",
        "actual_model_collection_requested": bool(args.collect_url),
        "generation_metadata": {
            k: captured[k] for k in ("request_config", "created_at", "evaluator_sha256") if k in captured
        },
    }
    _save(args.output, report)
    print(json.dumps({"fixture_validation": report["fixture_validation"], "summary": report["summary"]}, indent=2))
    if not report["fixture_validation"]["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
