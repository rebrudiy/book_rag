"""Measure25 open curated-metadata descriptions; never body-semantic accuracy."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path
from typing import Any

from book_rag.book_discovery import ReadOnlyBookLibrary, _save, discover_books


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/full"))
    parser.add_argument("--fixture", type=Path, default=Path("evals/book-discovery-dev-v1.json"))
    parser.add_argument("--output", type=Path, default=Path("data/evaluations/book-discovery-v1/results.json"))
    args = parser.parse_args()
    if any("sealed" in part.casefold() for part in args.fixture.parts):
        parser.error("Sealed inputs are not permitted")
    fixture_bytes = args.fixture.read_bytes()
    fixture = json.loads(fixture_bytes)
    if fixture.get("full_text_relevance_gold") is not False:
        parser.error("Expected explicitly open metadata-only development fixture")
    library = ReadOnlyBookLibrary(args.data_dir)
    results = []
    for case in fixture["cases"]:
        began = time.monotonic()
        response = discover_books(library, case["query"], language=case["language"], limit=12)
        elapsed = time.monotonic() - began
        ids = [book["id"] for book in response["books"]]
        ranks = [ids.index(bid) + 1 for bid in case["relevant_edition_ids"] if bid in ids]
        results.append(
            case
            | {
                "elapsed_seconds": elapsed,
                "status": response["status"],
                "coverage": response["coverage"],
                "warnings": response["warnings"],
                "error": response.get("error"),
                "first_relevant_rank": min(ranks) if ranks else None,
                "books": [
                    {
                        key: book.get(key)
                        for key in [
                            "id",
                            "title",
                            "author",
                            "language",
                            "score",
                            "matched_fields",
                            "metadata_evidence",
                            "reader_url",
                            "coverage",
                            "coverage_scope",
                            "illustrations_included",
                        ]
                    }
                    for book in response["books"]
                ],
            }
        )
    positive = [r for r in results if r["relevant_edition_ids"]]
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "metadata_semantic_development_only",
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "cases": len(results),
        "positive_cases": len(positive),
        "negative_curated_scope_controls": len(results) - len(positive),
        "known_curated_target_recall_at": {
            str(k): sum(r["first_relevant_rank"] is not None and r["first_relevant_rank"] <= k for r in positive)
            / len(positive)
            for k in [1, 5, 12]
        },
        "known_curated_target_mrr_at12": sum(
            1 / r["first_relevant_rank"] if r["first_relevant_rank"] else 0 for r in positive
        )
        / len(positive),
        "cold_query_seconds": results[0]["elapsed_seconds"],
        "warm_median_seconds": statistics.median(r["elapsed_seconds"] for r in results[1:]),
        "results": results,
        "database_modified": False,
        "passage_semantic_accuracy_claimed": False,
        "human_gold_labels": False,
        "interpretation": (
            "Recall of fixed knowncurated target editions/groups on authored open metadata descriptions; "
            "alternate uncatalogued translations may be relevant but receive no posthoc credit. "
            "Negative controls assert no curated20 target, not whole-library absence."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    _save(args.output, report)
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False, indent=2))
    return 0 if all(r["status"] == "ready" for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
