"""Open-development-only RU fallback prototype; read-only selected book snapshots.

Uses the three declared RU originals from the public RAG dev fixture and original
CC0 examples. No LLM, model encoding, heldout data or live corpus mutation.
Global ingestion may continue: only the selected editions are guarded.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sqlite3
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from html import escape
from pathlib import Path
from typing import Any
from unittest.mock import patch

from book_rag.library import Library
from book_rag.russian_retrieval import scoped_russian_fallback

ROOT = Path(__file__).resolve().parents[1]


def raw_search(library: Library, query: str, book_id: str, language: str | None = None) -> dict[str, Any]:
    # Keep a raw control without a test-only public API switch or a search copy.
    with patch("book_rag.russian_retrieval.scoped_russian_fallback", return_value={"used": False}):
        return library.search(query, "lexical", book_id, language, 40)


def checksum(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SelectedReadOnlyLibrary(Library):
    """No Library constructor/migration. Normal read-only WAL-aware connections."""

    def __init__(self, directory: Path) -> None:
        self.data_dir = directory.resolve(strict=True)
        self.db_path = self.data_dir / "library.sqlite3"

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            connection.close()


def selected_guard(library: SelectedReadOnlyLibrary, book_ids: list[str]) -> dict[str, Any]:
    guard = {}
    with library.connect() as connection:
        connection.execute("BEGIN")
        for identity in book_ids:
            book = connection.execute("SELECT * FROM books WHERE id=?", (identity,)).fetchone()
            if not book or book["status"] != "indexed":
                raise ValueError("Declared selected edition is not indexed")
            sections = [
                {
                    "id": row["id"],
                    "ordinal": row["ordinal"],
                    "title": row["title"],
                    "sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
                }
                for row in connection.execute(
                    "SELECT id,ordinal,title,text FROM sections WHERE book_id=? ORDER BY id", (identity,)
                )
            ]
            passages = [
                dict(row)
                for row in connection.execute(
                    "SELECT id,section_id,start,end,sha256 FROM passages WHERE book_id=? ORDER BY id", (identity,)
                )
            ]
            roles = [
                dict(row)
                for row in connection.execute(
                    "SELECT a.* FROM section_roles a JOIN sections s ON s.id=a.section_id "
                    "WHERE s.book_id=? ORDER BY a.section_id",
                    (identity,),
                )
            ]
            guard[identity] = {
                "book_metadata_sha256": checksum(dict(book)),
                "artifact_sha256": book["artifact_sha256"],
                "content_sha256": book["content_sha256"],
                "section_snapshot_sha256": checksum(sections),
                "passage_identity_sha256": checksum(passages),
                "roles_sha256": checksum(roles),
                "passages": len(passages),
            }
    return guard


def eligible_sources(library: Library, results: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    selected = []
    for result in results:
        if (
            result.get("section_role") == "editorial"
            and " ".join(str(result.get("section_author") or "").split()).casefold() == "bookrag"
        ):
            continue
        current = library.passage(result["id"])
        if (
            not current
            or not current["verified"]
            or current["text"] != result["text"]
            or current["citation"] != result["citation"]
        ):
            raise ValueError("Candidate canonical source changed or failed verification")
        selected.append(result)
        if len(selected) == limit:
            break
    return selected


def cc0_cases() -> list[dict[str, Any]]:
    return [
        {
            "id": "inflection-garden",
            "query": "садовниками кораблями",
            "texts": ["Путник беседовал с садовником о кораблях.", "Посторонние облака закрыли горизонт."],
            "gold": [0],
        },
        {
            "id": "inflection-crafts",
            "query": "кузнецами ремесленниками",
            "texts": ["Кузнец говорил с ремесленником о переправе.", "На столе лежали яблоки."],
            "gold": [0],
        },
        {
            "id": "spelling-yo",
            "query": "летчиками елками",
            "texts": ["Лётчики осматривали ёлки возле пристани."],
            "gold": [0],
        },
        {
            "id": "unicode-lantern",
            "query": "фонарями дорожках",
            "texts": ["Читатель любовался фонарём и дорожками сада 🦉."],
            "gold": [0],
        },
        {
            "id": "prefix-collision",
            "query": "миром",
            "texts": ["Путник размышлял о мире.", "Миротворцы обсуждали далёкие поселения."],
            "gold": [0],
        },
        {
            "id": "absent-science",
            "query": "квантовыми марсианскими двигателями",
            "texts": ["Корабли подходили к пристаням в 1820 году."],
            "gold": [],
        },
        {
            "id": "shared-anchor-negative",
            "query": "кораблями термоядерными реакторами",
            "texts": ["Корабли подходили к пристаням в 1820 году."],
            "gold": [],
        },
        {
            "id": "numeric-negative",
            "query": "кораблями пристанями 2024",
            "texts": ["Корабли подходили к пристаням в 1820 году."],
            "gold": [],
        },
        {
            "id": "negation-negative",
            "query": "кораблями пристанями не",
            "texts": ["Корабли подходили к пристаням в 1820 году.", "Путник не описывал облака."],
            "gold": [],
        },
        {"id": "short-stem-modal-collision", "query": "могила", "texts": ["Он мог прочесть карту."], "gold": []},
        {"id": "short-stem-verb-collision", "query": "пила", "texts": ["Затем он пил воду."], "gold": []},
    ]


def run_cc0(directory: Path) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    returned = correct = 0
    for index, case in enumerate(cc0_cases()):
        library = Library(directory / f"case-{index}")
        path = directory / f"source-{index}.html"
        path.write_text(
            "<h1>Оригинальный опыт CC0</h1>"
            + "".join(f"<h2>Сцена {number}</h2><p>{escape(text)}</p>" for number, text in enumerate(case["texts"]))
        )
        library.import_book(
            path,
            {
                "id": "cc0",
                "title": "Оригинальный опыт CC0",
                "author": "Book Atlas original fixture",
                "language": "ru",
                "rights": "CC0 1.0",
            },
        )
        normal = raw_search(library, case["query"], "cc0")
        if normal["results"]:
            raise AssertionError("Independent CC0 fallback case unexpectedly has normal hits")
        expanded = scoped_russian_fallback(library, case["query"], "cc0", prior_results=normal["results"], limit=8)
        integrated = library.search(case["query"], "lexical", "cc0", limit=8)
        if [(row["id"], row["text"], row["citation"]) for row in integrated["results"]] != [
            (row["id"], row["text"], row["citation"]) for row in expanded["results"]
        ]:
            raise AssertionError("Integrated fallback differs from the prototype canonical evidence")
        gold = {case["texts"][number] for number in case["gold"]}
        texts = [row["text"] for row in expanded["results"]]
        returned += len(texts)
        correct += sum(text in gold for text in texts)
        reports.append(
            {
                "id": case["id"],
                "normal_count": 0,
                "fallback_count": len(texts),
                "expected_positive": bool(gold),
                "exact_gold_match": set(texts) == gold,
                "all_sources_verified": all(row["verified"] for row in expanded["results"]),
                "elapsed_ms": expanded.get("elapsed_ms"),
            }
        )
    return {
        "cases": reports,
        "returned_fragments": returned,
        "gold_correct_fragments": correct,
        "fixture_precision": correct / returned if returned else None,
        "positive_recovered": sum(row["expected_positive"] and row["fallback_count"] > 0 for row in reports),
        "negative_empty": sum(not row["expected_positive"] and row["fallback_count"] == 0 for row in reports),
        "all_expected_outcomes": all(row["exact_gold_match"] for row in reports),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/full")
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/rag-dev-v1.json")
    parser.add_argument("--report", type=Path, default=ROOT / "experiments/russian-fallback-v1.json")
    args = parser.parse_args()
    fixture = json.loads(args.dataset.read_text())
    if fixture.get("split") != "development":
        raise ValueError("Only the open development fixture is allowed")
    cases = [case for case in fixture["cases"] if case["kind"] == "ru_original_factual"]
    if len(cases) != 3:
        raise ValueError("Expected exactly three public audited RU originals")
    if args.report.exists() or args.report.resolve().is_relative_to(args.source.resolve()):
        raise ValueError("Use a new report outside the source library")
    library = SelectedReadOnlyLibrary(args.source)
    ids = sorted({identity for case in cases for identity in case["book_ids"]})
    before = selected_guard(library, ids)
    expected = {book["book_id"]: book for book in fixture["books"]}
    for identity in ids:
        for field in ("artifact_sha256", "content_sha256"):
            if before[identity][field] != expected[identity][field]:
                raise ValueError("Audited development source snapshot changed")
    reports = []
    for case in cases:
        book_id = case["book_ids"][0]
        query = case["question"].translate(str.maketrans(dict.fromkeys('"«»“”„', " "))).strip()
        strict = raw_search(library, case["question"], book_id, "ru")
        normal = raw_search(library, query, book_id, "ru")
        expanded = scoped_russian_fallback(library, query, book_id, "ru", prior_results=normal["results"], limit=40)
        combined = expanded["results"] if expanded["used"] else normal["results"]
        integrated = library.search(query, "lexical", book_id, "ru", 40)
        integrated_matches = [(row["id"], row["text"], row["citation"]) for row in integrated["results"]] == [
            (row["id"], row["text"], row["citation"]) for row in combined
        ]
        if not integrated_matches:
            raise AssertionError("Production and prototype canonical evidence order differ")
        selected = eligible_sources(library, combined)
        anchors = [evidence["quote"] for evidence in case["evidence"]]

        def matches(rows: list[dict[str, Any]], expected: list[str] = anchors) -> list[int]:
            return [index + 1 for index, row in enumerate(rows) if any(anchor in row["text"] for anchor in expected)]

        reports.append(
            {
                "id": case["id"],
                "book_id": book_id,
                "question": case["question"],
                "retrieval_query": query,
                "strict_search_count": len(strict["results"]),
                "softquote_normal_count": len(normal["results"]),
                "fallback_used": expanded["used"],
                "fallback_reason": expanded["reason"],
                "fallback_candidates": expanded["candidates"],
                "fallback_count": len(expanded["results"]),
                "fallback_elapsed_ms": expanded.get("elapsed_ms"),
                "normal_reference_anchor_ranks": matches(normal["results"]),
                "combined_reference_anchor_ranks": matches(combined),
                "selected_evidence_count": len(selected),
                "reference_anchor_hit_at8": bool(matches(selected)),
                "selected_ids": [row["id"] for row in selected],
                "all_sources_verified": all(row["verified"] for row in selected),
                "integrated_matches_prototype_canonical_order": integrated_matches,
                "stem_matches": [
                    {"id": row["id"], "coverage": row.get("stem_coverage"), "stems": row.get("matched_stems")}
                    for row in expanded["results"]
                ],
            }
        )
    after = selected_guard(library, ids)
    if before != after:
        raise RuntimeError("Selected book source/IDs/metadata changed; discard this run")
    with tempfile.TemporaryDirectory(prefix="russian-fallback-cc0-") as temporary:
        independent = run_cc0(Path(temporary))
    report = {
        "kind": "scoped_russian_morphology_prototype_open_development_only",
        "production_integrated": True,
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "module_sha256": hashlib.sha256((ROOT / "src/book_rag/russian_retrieval.py").read_bytes()).hexdigest(),
        "dependency": {
            "name": "snowballstemmer",
            "version": importlib.metadata.version("snowballstemmer"),
            "license": "BSD-3-Clause",
            "primary_package": "https://pypi.org/project/snowballstemmer/3.1.1/",
            "algorithm": "https://snowballstem.org/algorithms/russian/stemmer.html",
        },
        "selected_source_guards_passed": True,
        "selected_sources_before": before,
        "selected_sources_after": after,
        "development_cases": reports,
        "independent_CC0": independent,
        "limitations": [
            "Open development probes informed this prototype; no final/heldout or LLM evaluation.",
            "Reference-anchor presence is not semantic precision or entailment. "
            "CC0 precision applies only to the small authored cases.",
            "Ask softens question quote delimiters separately; Search quoted phrases and baseline remain unchanged.",
            "Only empty scoped lexical results expand. Existing nonempty rankings, "
            "including missed lower-ranked evidence, remain unchanged.",
            "Snowball is heuristic stemming, not lemmatization; irregular stems and prefix collisions limit recall.",
            "Selected book headers/canonical sections/passages/roles are guarded. "
            "Other live corpus growth is allowed; no whole-DB hash/freeze is claimed.",
            "No live writes, Library(original) construction, model encoding or generation; "
            "only owned CC0 fixture imports.",
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"development": reports, "CC0": independent}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
