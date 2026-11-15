"""At most three explicit open-dev planning calls, selected books read-only.

Run as a module from the repository root. No answer generation, embedding calls,
production integration or database construction/migration is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_rag.local_generation import LocalGenerator
from book_rag.query_planning import PlanningGenerator, PlanningLimits, plan_queries, retrieve_plan_candidates
from scripts.evaluate_russian_fallback import SelectedReadOnlyLibrary, selected_guard

ROOT = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def searchable(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in results
        if row.get("verified") is True
        and not (
            row.get("section_role") == "editorial"
            and " ".join(str(row.get("section_author") or "").split()).casefold() == "bookrag"
        )
    ]


def compact(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: row.get(key)
            for key in ("id", "book_id", "section_id", "section_title", "start", "end", "citation", "verified")
        }
        | {"text_sha256": hashlib.sha256(row["text"].encode()).hexdigest()}
        | ({"planned_lexical_rerank": row["planned_lexical_rerank"]} if "planned_lexical_rerank" in row else {})
        for row in results
    ]


def coverage(results: list[dict[str, Any]], anchors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "reference_id": anchor["id"],
            "book_id": anchor["book_id"],
            "first_rank": next(
                (
                    rank
                    for rank, result in enumerate(results, 1)
                    if result["book_id"] == anchor["book_id"] and anchor["quote"] in result["text"]
                ),
                None,
            ),
        }
        for anchor in anchors
    ]


def merge(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    results = []
    for rank in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if rank < len(group) and group[rank]["id"] not in seen:
                results.append(group[rank])
                seen.add(group[rank]["id"])
    return results


def russian_planned_query(library: SelectedReadOnlyLibrary, query: str, book_id: str) -> dict[str, Any]:
    """Explicit experimental plan channel; never changes original-search fallback."""
    from book_rag.russian_retrieval import prepare_russian_fallback, rerank_russian_fallback

    started = time.monotonic()
    plan = prepare_russian_fallback(query)
    if plan is None:
        return {"used": False, "results": [], "reason": "no_bounded_stems"}
    with library.connect() as connection:
        connection.execute("BEGIN")
        row = connection.execute("SELECT language,status FROM books WHERE id=?", (book_id,)).fetchone()
        if not row or row["language"] != "ru" or row["status"] != "indexed":
            return {"used": False, "results": [], "reason": "not_indexed_russian"}
        rows = library._lexical_rows(connection, plan.expression, book_id, "ru", 200)
        candidates = [library._result(row) for row in rows]
    results = rerank_russian_fallback(plan, candidates, limit=40)
    return {
        "used": True,
        "results": searchable(results),
        "stems": list(plan.keys),
        "elapsed_ms": (time.monotonic() - started) * 1000,
        "corrupt_candidates_suppressed": sum(row.get("verified") is not True for row in candidates),
    }


class ReceiptReplay:
    """Revalidate an already generated plan without any native model/runtime."""

    def __init__(self, path: Path) -> None:
        self.receipt = json.loads(path.read_text())

    def generate(
        self, messages: list[dict[str, str]], limits: Any = None, *, response_schema: Any = None
    ) -> dict[str, Any]:
        request = json.loads(messages[-1]["content"])
        matching = [
            case for case in self.receipt["cases"] if case["question"] == request["question"] and case["plan"]["used"]
        ]
        if len(matching) != 1:
            raise ValueError("Receipt has no unique validated plan for this question")
        plan = matching[0]["plan"]
        return {
            "text": json.dumps(
                {
                    "response_language": plan["response_language_hint"],
                    "books": [
                        {"book_id": book["book_id"], "queries": book["queries"]} for book in plan["book_queries"]
                    ],
                },
                ensure_ascii=False,
            ),
            "finish_reason": "stop",
            "model": {"profile": "receipt-replay-no-model-call"},
            "elapsed_ms": 0,
        }


def run_probe(
    source: Path,
    fixture_path: Path,
    case_ids: list[str],
    *,
    generator: PlanningGenerator | None,
    limits: PlanningLimits,
    rerank_candidates: bool = False,
) -> dict[str, Any]:
    fixture_sha = sha(fixture_path)
    fixture = json.loads(fixture_path.read_text())
    if fixture.get("split") != "development" or not 1 <= len(set(case_ids)) == len(case_ids) <= 3:
        raise ValueError("Select one to three distinct public development cases")
    indexed = {case["id"]: case for case in fixture["cases"]}
    if any(key not in indexed for key in case_ids):
        raise ValueError("Unknown public development case")
    cases = [indexed[key] for key in case_ids]
    if any(not case.get("book_ids") for case in cases):
        raise ValueError("The planner requires explicit selected-book scope")
    ids = sorted({identity for case in cases for identity in case["book_ids"]})
    library = SelectedReadOnlyLibrary(source)
    before = selected_guard(library, ids)
    reference_books = {book["book_id"]: book for book in fixture["books"]}
    for identity in ids:
        for key in ("artifact_sha256", "content_sha256"):
            if before[identity][key] != reference_books[identity][key]:
                raise ValueError("Selected edition differs from the public reference source")
    reports: list[dict[str, Any]] = []
    started = time.monotonic()
    for case in cases:
        with library.connect() as connection:
            books = [
                dict(
                    connection.execute(
                        "SELECT id,title,author,language FROM books WHERE id=? AND status='indexed'", (identity,)
                    ).fetchone()
                )
                for identity in case["book_ids"]
            ]
        # No expected facts/quotes/references are supplied to the planner.
        plan = plan_queries(case["question"], books, generator=generator, limits=limits)
        original = case["question"].translate(str.maketrans(dict.fromkeys('"«»“”„', " "))).strip()
        planned_byid = {item["book_id"]: item["queries"] for item in plan["book_queries"]}
        per_book: list[dict[str, Any]] = []
        original_searches: dict[str, dict[str, Any]] = {}
        for book in books:
            anchors = [item for item in case["evidence"] if item["book_id"] == book["id"]]
            control = library.search(original, "lexical", book["id"], limit=40)
            original_searches[book["id"]] = control
            control_hits = searchable(control["results"])
            query_reports: list[dict[str, Any]] = []
            query_groups = []
            for query in planned_byid.get(book["id"], []):
                candidate = library.search(query, "lexical", book["id"], limit=40)
                hits = searchable(candidate["results"])
                assert all(row["book_id"] == book["id"] for row in hits)
                query_groups.append(hits)
                stem_report: dict[str, Any] | None = None
                if book["language"] == "ru":
                    stemmed = russian_planned_query(library, query, book["id"])
                    stem_report = {key: value for key, value in stemmed.items() if key != "results"}
                    stem_report.update(
                        anchors=coverage(stemmed["results"], anchors), results=compact(stemmed["results"])
                    )
                query_reports.append(
                    {
                        "query": query,
                        "mode": candidate["mode"],
                        "elapsed_ms": candidate["elapsed_ms"],
                        "warnings": candidate["warnings"],
                        "anchors": coverage(hits, anchors),
                        "results": compact(hits),
                        "experimental_russian_stems": stem_report,
                    }
                )
            planned = merge(query_groups)
            combined = merge(query_groups + [control_hits])
            per_book.append(
                {
                    "book_id": book["id"],
                    "language": book["language"],
                    "original": {
                        "query": original,
                        "mode": control["mode"],
                        "elapsed_ms": control["elapsed_ms"],
                        "anchors": coverage(control_hits, anchors),
                        "results": compact(control_hits),
                    },
                    "planned_queries": query_reports,
                    "planned_round_robin_anchors": coverage(planned, anchors),
                    "planned_then_original_round_robin_anchors": coverage(combined, anchors),
                    "merge_note": "Diagnostic only; candidate ranking/merging is not integrated into Ask.",
                }
            )
        original_snapshot = json.dumps(original_searches, sort_keys=True, ensure_ascii=False)
        helper = retrieve_plan_candidates(
            library, case["question"], books, plan, original_searches, rerank_candidates=rerank_candidates
        )
        assert json.dumps(original_searches, sort_keys=True, ensure_ascii=False) == original_snapshot
        helper_report = {key: helper[key] for key in ("used", "reason", "elapsed_ms", "warnings")}
        helper_report["original_results_unchanged"] = True
        helper_report["ranking_strategy"] = helper.get("ranking_strategy", "original_retrieval")
        helper_report["rerank_by_book"] = helper.get("rerank_by_book", {})
        helper_report["books"] = [
            {
                "book_id": book["id"],
                "anchors": coverage(
                    helper["candidates_by_book"][book["id"]],
                    [anchor for anchor in case["evidence"] if anchor["book_id"] == book["id"]],
                ),
                "results": compact(helper["candidates_by_book"][book["id"]]),
            }
            for book in books
        ]
        reports.append(
            {
                "id": case["id"],
                "question": case["question"],
                "plan": plan,
                "books": per_book,
                "candidate_helper": helper_report,
            }
        )
        print(
            json.dumps(
                {"case": case["id"], "used": plan["used"], "reason": plan["reason"], "planning_ms": plan["elapsed_ms"]},
                ensure_ascii=False,
            ),
            flush=True,
        )
    after = selected_guard(library, ids)
    if before != after or sha(fixture_path) != fixture_sha:
        raise RuntimeError("Selected-source or public-fixture guard changed; do not credit evidence gains")
    anchors = [(b, anchor) for case in reports for b in case["books"] for anchor in b["original"]["anchors"]]
    planned_anchors = [
        (b, anchor) for case in reports for b in case["books"] for anchor in b["planned_round_robin_anchors"]
    ]
    helper_anchors = [
        anchor for case in reports for book in case["candidate_helper"]["books"] for anchor in book["anchors"]
    ]
    return {
        "kind": "open-development-retrieval-planning-probe",
        "created_at": datetime.now(UTC).isoformat(),
        "source_directory": str(source.resolve()),
        "fixture": str(fixture_path),
        "fixture_sha256": fixture_sha,
        "planner_invoked": generator is not None,
        "limits": {
            "timeout_seconds": limits.timeout_seconds,
            "max_tokens": limits.max_tokens,
            "max_queries": limits.max_queries,
            "max_queries_per_book": limits.max_queries_per_book,
        },
        "comparison_mode": "lexical",
        "planned_candidate_rerank_enabled": rerank_candidates,
        "source_guard_before": before,
        "source_guard_after": after,
        "selected_source_guard_passed": True,
        "elapsed_seconds": time.monotonic() - started,
        "summary": {
            "cases": len(reports),
            "validated_plans": sum(case["plan"]["used"] for case in reports),
            "reference_anchors": len(anchors),
            "helper_anchor_hits_at2_per_book": sum(
                anchor["first_rank"] is not None and anchor["first_rank"] <= 2 for anchor in helper_anchors
            ),
            "helper_anchor_hits_at40_per_book": sum(anchor["first_rank"] is not None for anchor in helper_anchors),
            "original_anchor_hits_at2_per_book": sum(
                a["first_rank"] is not None and a["first_rank"] <= 2 for _, a in anchors
            ),
            "planned_anchor_hits_at2_per_book": sum(
                a["first_rank"] is not None and a["first_rank"] <= 2 for _, a in planned_anchors
            ),
            "original_anchor_hits_at40_per_book": sum(a["first_rank"] is not None for _, a in anchors),
            "planned_anchor_hits_in_query_union": sum(a["first_rank"] is not None for _, a in planned_anchors),
        },
        "cases": reports,
        "limitations": [
            "Three or fewer selected, previously inspected open development failures; not heldout or general quality.",
            "Lexical original versus lexical planned queries; this does not re-run "
            "or score the full hybrid RAG pipeline.",
            "Only query hypotheses are generated; no answer, entailment verdict or user-facing fact is generated.",
            "Keyword-language correctness and topical fidelity require review; "
            "structural validation cannot prove them.",
            "Selected-source guards cover metadata, source hashes, sections, passage IDs "
            "and contributor roles; global ingestion may continue.",
            "No Library(original) constructor, migrations, source writes, embedding jobs or paid inference.",
            "Timing includes a potentially cold local planner; sequential searches have cache/order confounds.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/full")
    parser.add_argument("--fixture", type=Path, default=ROOT / "evals/rag-dev-v1.json")
    parser.add_argument("--case", action="append", required=True)
    parser.add_argument("--report", type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--real-planner", action="store_true")
    group.add_argument("--replay-report", type=Path)
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--queries-per-book", type=int, choices=(1, 2), default=2)
    parser.add_argument("--rerank-planned", action="store_true")
    args = parser.parse_args()
    if args.report.exists() or args.report.resolve().is_relative_to(args.source.resolve()):
        raise ValueError("Use a new report outside the source library")
    generator: PlanningGenerator | None = None
    runtime: LocalGenerator | None = None
    if args.replay_report:
        generator = ReceiptReplay(args.replay_report)
        if generator.receipt["fixture_sha256"] != sha(args.fixture):
            raise ValueError("Replay receipt uses a different fixture")
    if args.real_planner:
        if os.getenv("BOOKRAG_GENERATION_PROFILE") != "qwen3-4b-instruct-2507":
            raise ValueError("Explicitly select the reviewed 4B candidate profile before importing this module")
        runtime = LocalGenerator()
        generator = runtime
    try:
        result = run_probe(
            args.source,
            args.fixture,
            args.case,
            generator=generator,
            limits=PlanningLimits(timeout_seconds=args.timeout, max_queries_per_book=args.queries_per_book),
            rerank_candidates=args.rerank_planned,
        )
    finally:
        if runtime is not None:
            runtime.close()
    result["actual_model_generation_invoked"] = args.real_planner
    if args.replay_report:
        result["replay_receipt"] = {"path": str(args.replay_report), "sha256": sha(args.replay_report)}
        assert isinstance(generator, ReceiptReplay)
        old_guard = generator.receipt["source_guard_after"]
        if any(old_guard.get(identity) != value for identity, value in result["source_guard_before"].items()):
            raise RuntimeError("Replayed selected edition differs from the original planning run")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
