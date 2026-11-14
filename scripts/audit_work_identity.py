"""Audit public indexed Gutenberg editions; preserve existing deferred evidence offline.

No Library initialization, DB writes, manifest edits, full-text downloads or
automatic work-count promotion. A prior blocked run is never retried by rerun.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_rag import work_identity as legacy
from book_rag.authority_audit import (
    _write,
    cached_frontmatter,
    frontmatter_observations,
    propose_authority_audit,
    select_public_entries,
)


def offline_controls(directory: Path) -> dict[str, Any]:
    """Validate existing CC0 authority snapshots without calling their HTTP helper."""
    controls = json.loads((directory / "proposals.json").read_text())
    verified = []
    for path in sorted((directory / "queries").glob("*.json")):
        if path.name == "deferred.json":
            continue
        item = json.loads(path.read_text())
        payload = json.dumps(item["response"], sort_keys=True, ensure_ascii=False).encode()
        assert hashlib.sha256(payload).hexdigest() == item["response_sha256"]
        assert item["endpoint"] == legacy.ENDPOINT
        assert item["provenance"]["license"] == "CC0-1.0"
        legacy._bindings(item["response"])
        verified.append(
            {
                "query_sha256": path.stem,
                "retrieved_at": item["provenance"]["retrieved_at"],
                "response_sha256": item["response_sha256"],
            }
        )
    return {
        "previously_reviewed_editions": controls["input_entries"],
        "counts": controls["counts"],
        "verified_query_caches": verified,
        "network_requests": 0,
        "not_part_of_new_200": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/full/library.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("data/identity/wikidata-200-v1"))
    parser.add_argument("--controls", type=Path, default=Path("data/identity/wikidata-50"))
    parser.add_argument("--summary", type=Path, default=Path("experiments/authority-200-v1.json"))
    parser.add_argument("--verbose", action="store_true", help="Print the 20 frontmatter metadata observations")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifests = [Path("data/catalog/expand-text-new.json"), Path("corpus/manifest.json")]
    reviewed = {entry["id"] for entry in json.loads((args.controls / "input.json").read_text())}
    entries, selection = select_public_entries(manifests, args.database, reviewed)
    assert len(entries) == 200, "This controlled run requires exactly 200 eligible public entries"
    report_path = args.output / "proposals.json"
    if report_path.exists():
        result = json.loads(report_path.read_text())
        old = json.loads((args.output / "input.json").read_text())
        assert [e["id"] for e in old] == [e["id"] for e in entries], (
            "Public selection changed; preserve this run and review separately"
        )
        # Never retry a deferred endpoint implicitly. Reuse the original attempt
        # and verify successful raw query-cache receipts offline.
        for receipt in result["query_evidence"]:
            raw = args.output / "queries" / (receipt["query_sha256"] + ".response.json")
            assert hashlib.sha256(raw.read_bytes()).hexdigest() == receipt["response_sha256"]
        network_this_run = 0
    else:
        _write(args.output / "input.json", entries)
        _write(args.output / "selection.json", selection)
        result = propose_authority_audit(entries, args.output)
        network_this_run = result["network_requests"]
    by_id = {p["id"]: p for p in result["proposals"]}
    priorities = {"authority_linked": 0, "ambiguous": 1, "unresolved": 2}
    buckets = {
        language: sorted(
            [e for e in entries if e["language"] == language],
            key=lambda e: (
                priorities[by_id[e["id"]]["confidence"]],
                not by_id[e["id"]]["mapping_query_complete"],
                entries.index(e),
            ),
        )
        for language in ("en", "de", "fr")
    }
    chosen = []
    while len(chosen) < 20:
        for language in buckets:
            if len(chosen) < 20 and buckets[language]:
                chosen.append(buckets[language].pop(0))
    observations = []
    frontmatter = []
    for entry in chosen:
        try:
            evidence = cached_frontmatter(entry)
        except (OSError, ValueError, KeyError) as error:
            evidence = {"id": entry["id"], "status": "frontmatter_unavailable", "error": str(error)}
        frontmatter.append(evidence)
        observations.append(frontmatter_observations(entry, by_id[entry["id"]], evidence))
    _write(args.output / "frontmatter-evidence.json", frontmatter)
    _write(args.output / "frontmatter-observations.json", observations)
    manual_path = args.output / "frontmatter-manual-review.json"
    manual_reviews = []
    if manual_path.exists():
        manual = json.loads(manual_path.read_text())
        actual = {e["id"]: e.get("artifact_sha256") for e in frontmatter}
        manual_reviews = [r for r in manual["reviews"] if actual.get(r["id"]) == r["artifact_sha256"]]
    controls = offline_controls(args.controls)
    _write(args.output / "offline-controls.json", controls)
    answered = [p for p in result["proposals"] if p["mapping_query_complete"]]
    summary: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "Two public Gutenberg manifests; books/book_metadata queried with mode=ro; no notes/private imports",
        "selection": selection,
        "requested_editions": 200,
        "answered_editions": len(answered),
        "answered_languages": dict(Counter(p["language"] for p in answered)),
        "answered_counts": dict(Counter(p["confidence"] for p in answered)),
        "unavailable_editions": 200 - len(answered),
        "distinct_candidate_roots": result["distinct_candidate_work_roots"],
        "network_requests_initial_attempt": result["network_requests"],
        "network_requests_this_script_run": network_this_run,
        "successful_response_bytes": result["response_bytes"],
        "deferred": result["deferred"],
        "failures": result["failures"],
        "query_evidence": [
            {k: r[k] for k in ("query_sha256", "retrieved_at", "response_sha256", "response_bytes")}
            for r in result["query_evidence"]
        ],
        "linked_proposals": [p for p in result["proposals"] if p["confidence"] == "authority_linked"],
        "frontmatter_observations": observations,
        "frontmatter_manual_reviews": manual_reviews,
        "offline_control_counts": controls["counts"],
        "offline_controls_verified_query_caches": len(controls["verified_query_caches"]),
        "database_modified": False,
        "source_manifests_modified": False,
        "identity_count_promotion_allowed": False,
        "confirmed_work_count_increment": 0,
        "scope": "Authority assertions and lexical/manual-review evidence only; no unique-work verification or merge",
        "bounds": result["bounds"],
    }
    _write(args.summary, summary)
    _write(args.output / "summary.json", summary)
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "requested_editions",
                    "answered_editions",
                    "answered_counts",
                    "unavailable_editions",
                    "distinct_candidate_roots",
                    "network_requests_this_script_run",
                    "deferred",
                )
            },
            ensure_ascii=False,
        )
    )
    for item in observations if args.verbose else []:
        print(
            json.dumps(
                {
                    k: item[k]
                    for k in (
                        "id",
                        "language",
                        "catalog_title",
                        "catalog_author",
                        "opf_titles",
                        "opf_authors",
                        "candidate_qid",
                        "catalog_title_matches_opf",
                        "catalog_author_surname_present",
                    )
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
