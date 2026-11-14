"""Collect raw model scores from a read-only development snapshot and explore floors.

Usage: uv run python scripts/calibrate_retrieval.py --kind cosine --output ...
Never reads historical held-out fixtures or applies a threshold to the application.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_rag.calibration import calibrate, evaluate_threshold
from book_rag.evaluation import load_dataset, verify_result
from book_rag.library import Library


class ReadOnlyLibrary(Library):
    """Reuse retrieval methods without schema creation or writable connections."""

    def __init__(self, directory: Path) -> None:
        self.data_dir = directory
        self.db_path = directory / "library.sqlite3"
        if not self.db_path.is_file():
            raise FileNotFoundError(self.db_path)

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.create_function("casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
        try:
            yield connection
        finally:
            connection.close()


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _relevant(case: dict[str, Any], result: dict[str, Any], book: dict[str, Any], checks: dict[str, bool]) -> bool:
    identity = result["book_id"] in case.get("relevant_book_ids", []) or book["work_id"] in case.get(
        "relevant_work_ids", []
    )
    phrases = case.get("evidence_contains", [])
    return bool(
        case["answerable"]
        and identity
        and all(checks.values())
        and (not phrases or any(phrase.casefold() in result["text"].casefold() for phrase in phrases))
    )


def collect(args: argparse.Namespace) -> dict[str, Any]:
    fixture = load_dataset(args.dataset)
    if fixture["split"] != "dev":
        raise ValueError("This exploratory runner accepts development fixtures only")
    library = ReadOnlyLibrary(args.data_dir)
    active_path = args.data_dir / "vectors" / "active.json"
    metadata_bytes = active_path.read_bytes()
    index_metadata = json.loads(metadata_bytes)
    before = _digest(library.db_path)
    source = library.books()
    source_fields = (
        "id",
        "work_id",
        "title",
        "author",
        "language",
        "edition",
        "status",
        "artifact_sha256",
        "content_sha256",
        "source_url",
        "rights",
        "rights_url",
    )
    source_snapshot = [{key: book.get(key) for key in source_fields} for book in sorted(source, key=lambda b: b["id"])]
    if fixture.get("required_book_ids") and {book["id"] for book in source} != set(fixture["required_book_ids"]):
        raise ValueError("Calibration corpus must exactly match the fixture's required edition subset")
    old_floor = os.environ.get("BOOKRAG_SEMANTIC_THRESHOLD")
    # Uncensor raw cosine scores only in this experiment's process. Reranker
    # candidate generation retains the declared existing floor for comparability.
    floor = -1.0 if args.kind == "cosine" else args.upstream_floor
    os.environ["BOOKRAG_SEMANTIC_THRESHOLD"] = str(floor)
    examples: list[dict[str, Any]] = []
    books: dict[str, dict[str, Any]] = {}
    started = time.perf_counter()
    try:
        for number, case in enumerate(fixture["queries"], 1):
            query_started = time.perf_counter()
            mode = "semantic" if args.kind == "cosine" else "hybrid"
            response = library.search(
                case["query"],
                mode=mode,
                limit=1 if args.kind == "cosine" else args.candidate_depth,
                **case.get("filters", {}),
            )
            if response["mode"] != mode:
                raise RuntimeError("Cannot calibrate a fallback mode: " + str(response.get("warnings")))
            results = response["results"]
            warnings = list(response.get("warnings", []))
            reranker_metadata = None
            if args.kind == "logit" and results:
                from book_rag.reranking import rerank

                ranked = rerank(case["query"], results, args.cache_dir, limit=1)
                if ranked["available"] is not True:
                    raise RuntimeError("Reranker unavailable: " + str(ranked["warnings"]))
                results = ranked["results"]
                warnings.extend(ranked["warnings"])
                reranker_metadata = {key: value for key, value in ranked.items() if key != "results"}
            result = results[0] if results else None
            checks = {}
            relevant = False
            if result:
                book_id = result["book_id"]
                if book_id not in books:
                    book = library.get_book(book_id)
                    if not book:
                        raise ValueError("Source book disappeared during calibration")
                    books[book_id] = book
                checks = verify_result(result, books[book_id])
                relevant = _relevant(case, result, books[book_id], checks)
            score_key = "semantic_score" if args.kind == "cosine" else "reranker_score"
            if result and score_key not in result:
                raise ValueError(f"Missing raw {score_key}; refusing to substitute an RRF retrieval score")
            examples.append(
                {
                    "id": case["id"],
                    "query": case["query"],
                    "type": case["type"],
                    "language": case["language"],
                    "filters": case.get("filters", {}),
                    "score": float(result[score_key]) if result else None,
                    "answerable": case["answerable"],
                    "top_result_relevant": relevant,
                    "top_result": {
                        key: result.get(key)
                        for key in (
                            "id",
                            "book_id",
                            "work_id",
                            "section_id",
                            "section_title",
                            "text",
                            "start",
                            "end",
                            "citation",
                            "section_author",
                            "section_role",
                        )
                    }
                    if result
                    else None,
                    "independent_source_checks": checks,
                    "candidate_count": len(response["results"]),
                    "warnings": warnings,
                    "reranker_metadata": reranker_metadata,
                    "elapsed_ms": round((time.perf_counter() - query_started) * 1000, 3),
                }
            )
            print(
                f"{number}/{len(fixture['queries'])} {case['id']} score={examples[-1]['score']} relevant={relevant}",
                flush=True,
            )
    finally:
        if old_floor is None:
            os.environ.pop("BOOKRAG_SEMANTIC_THRESHOLD", None)
        else:
            os.environ["BOOKRAG_SEMANTIC_THRESHOLD"] = old_floor
    if active_path.read_bytes() != metadata_bytes or _digest(library.db_path) != before:
        raise RuntimeError("Source database or vector generation changed; discard this calibration run")
    calibration = calibrate(
        examples,
        target_precision=args.target_precision,
        min_accepted=args.min_accepted,
        objective=args.objective,
        false_accept_cost=args.false_accept_cost,
    )
    validation_ids = set(calibration["partition"]["validation_ids"])
    validation = [row for row in examples if row["id"] in validation_ids]
    threshold = calibration["training_selection"]["threshold"]
    by_language = {
        language: evaluate_threshold([row for row in validation if row["language"] == language], threshold)
        for language in sorted({row["language"] for row in validation})
    }
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "kind": args.kind,
        "score_semantics": "raw cosine of normalized embedding windows"
        if args.kind == "cosine"
        else "raw cross-encoder ranking logit",
        "not_answer_probabilities": True,
        "dataset": str(args.dataset),
        "dataset_sha256": _digest(args.dataset),
        "split": "dev",
        "source_database": str(library.db_path),
        "source_database_sha256": before,
        "corpus": library.stats(),
        "source_snapshot": source_snapshot,
        "corpus_sha256": hashlib.sha256(
            json.dumps(source_snapshot, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "semantic_index_metadata": index_metadata,
        "index_metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
        "candidate_generation": {
            "mode": "semantic" if args.kind == "cosine" else "hybrid",
            "requested_candidates": 1 if args.kind == "cosine" else args.candidate_depth,
            "upstream_cosine_floor": floor,
        },
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "examples": examples,
        "calibration": calibration,
        "validation_by_language": by_language,
        "implementation_sha256": {
            str(path): _digest(path)
            for path in [
                Path(__file__),
                Path("src/book_rag/calibration.py"),
                Path("src/book_rag/semantic.py"),
                Path("src/book_rag/reranking.py"),
            ]
        },
        "limitations": [
            "No threshold is applied to the application; this is development-only exploratory research.",
            "Top relevance uses manual anchors/book identity and independent source checks, not entailment.",
            "Anchors are nonexhaustive; comparisons may need additional sources beyond relevant top1.",
            "Contributor-role probes are not automatically scored by these relevance labels.",
            "Ten curated scoped negatives are too few for a universal answerability threshold.",
            "No old heldout fixture was read and no new final-test dataset was used.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["cosine", "logit"], default="cosine")
    parser.add_argument("--dataset", type=Path, default=Path("evals/dev-expanded.json"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/snapshots/curated20-v3"))
    parser.add_argument("--cache-dir", type=Path, default=Path(os.getenv("BOOKRAG_MODEL_CACHE", "data/models")))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-precision", type=float, default=0.9)
    parser.add_argument("--min-accepted", type=int, default=3)
    parser.add_argument("--objective", choices=["precision", "utility"], default="precision")
    parser.add_argument("--false-accept-cost", type=float, default=5.0)
    parser.add_argument("--candidate-depth", type=int, choices=range(1, 51), default=50)
    parser.add_argument("--upstream-floor", type=float, default=0.3)
    args = parser.parse_args()
    if not -1 <= args.upstream_floor <= 1:
        parser.error("--upstream-floor must be in [-1, 1]")
    report = collect(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "selection": report["calibration"]["training_selection"],
                "validation": report["calibration"]["validation_metrics"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
