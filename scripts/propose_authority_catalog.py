#!/usr/bin/env python3
"""Review-only exact PG authority proposals; cached metadata is the default."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

from book_rag.authority_audit import cached_frontmatter, frontmatter_observations
from book_rag.authority_catalog import (
    _save,
    acquire_graph_pilot,
    acquire_identifier_catalog,
    build_proposals,
    choose_graph_pilot,
    select_public_pg_editions,
    verify_public_selection,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/full/library.sqlite3"))
    parser.add_argument("--rdf-catalog", type=Path, default=Path("data/catalog/rdf-files.tar.bz2"))
    parser.add_argument("--output", type=Path, default=Path("data/identity/wikidata-catalog-v1"))
    parser.add_argument("--input-editions", type=Path, help="Reuse a public RDF selection, checking current DB guards")
    parser.add_argument("--fetch-metadata", action="store_true", help="Allow bounded public metadata requests")
    parser.add_argument("--graph-pilot", action="store_true", help="Try at most40 exact-ID seed work graphs")
    parser.add_argument("--frontmatter", type=int, default=0, help="Read up to20 bounded cached public-source previews")
    args = parser.parse_args()
    if not 0 <= args.frontmatter <= 20:
        parser.error("--frontmatter must be0–20")
    if args.input_editions:
        body = args.input_editions.read_bytes()
        selected = json.loads(body)
        entries, selection = selected["entries"], selected["selection"]
        if len(entries) > 100000:
            parser.error("Public edition selection exceeds bounded100000 inputs")
        with args.rdf_catalog.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != selection["catalog_sha256"]:
                parser.error("Cached public selection belongs to a different official RDF snapshot")
        verify_public_selection(entries, args.database)
        selection = selection | {
            "input_selection_sha256": hashlib.sha256(body).hexdigest(),
            "current_guards_verified": True,
        }
    else:
        entries, selection = select_public_pg_editions(args.database, args.rdf_catalog)
        _save(args.output / "input-editions.json", {"selection": selection, "entries": entries})
    catalog = acquire_identifier_catalog(args.output, allow_network=args.fetch_metadata)
    graph, evidence = {}, {}
    seeds = choose_graph_pilot(entries, catalog)
    if args.graph_pilot and seeds:
        graph, evidence = acquire_graph_pilot(seeds, args.output, allow_network=args.fetch_metadata)
    report = build_proposals(entries, catalog, graph)
    report["public_selection"] = selection
    report["graph_pilot"] = evidence
    _save(args.output / "authority-proposals.json", report)
    if args.frontmatter:
        proposals = {p["id"]: p for p in report["proposals"]}
        eligible = [entry for entry in entries if proposals[entry["id"]].get("authority_item_ids")]
        eligible.sort(
            key=lambda e: (
                len(proposals[e["id"]]["authority_item_ids"]) <= 1,
                proposals[e["id"]].get("authority_item_ids", [None])[0] not in seeds,
                hashlib.sha256(("authority-frontmatter-v1:" + e["id"]).encode()).hexdigest(),
            )
        )
        observations: list[dict[str, Any]] = []
        with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            for entry in eligible[: args.frontmatter]:
                path = db.execute("SELECT local_path FROM books WHERE id=?", (entry["id"],)).fetchone()
                source = cached_frontmatter(entry | {"indexed_local_path": path[0] if path else None})
                proposal = proposals[entry["id"]]
                adapted = proposal | {
                    "candidates": [proposal["candidate"]] if "candidate" in proposal else [],
                    "candidate_qid": proposal.get("candidate_work_qid"),
                }
                observations.append(
                    {
                        "source_evidence": source,
                        "observations": frontmatter_observations(entry, adapted, source),
                        "manual_review": "pending",
                    }
                )
        _save(
            args.output / "frontmatter-review.json",
            {
                "examples": observations,
                "source_status_counts": dict(Counter(o["source_evidence"]["status"] for o in observations)),
                "interpretation": "Lexical observations only, not semantic or complete-work identity verification",
                "database_modified": False,
            },
        )
    summary = {
        k: report[k]
        for k in (
            "input_editions",
            "exact_identifier_matched_editions",
            "confidence_counts",
            "reason_counts",
            "competing_item_editions",
            "database_modified",
            "identity_count_promotion_allowed",
        )
    }
    summary.update(
        new_network_requests=catalog["network_requests"] + evidence.get("network_requests", 0),
        response_bytes=catalog["response_bytes"] + evidence.get("response_bytes", 0),
        output=str(args.output / "authority-proposals.json"),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if catalog["count_consistent"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
