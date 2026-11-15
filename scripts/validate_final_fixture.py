"""Verify held-out fixture grounding and SHA commitments without running retrieval.

This script contains no real query, anchor, book ID or title from the sealed set.
It opens only immutable source SQLite and reports aggregates, never private cases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any


class SealValidationError(ValueError):
    """A private fixture or its source does not match its declared evidence."""


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise SealValidationError(code)


def _normal(value: str) -> str:
    value = unicodedata.normalize("NFC", value).casefold()
    return " ".join(re.findall(r"\w+", value))


def _footprint(source: Path) -> dict[str, dict[str, Any] | None]:
    result: dict[str, dict[str, Any] | None] = {}
    for suffix in ("", "-wal", "-shm"):
        path = source.with_name(source.name + suffix)
        result[suffix or "main"] = {"bytes": path.stat().st_size, "sha256": sha256(path)} if path.exists() else None
    return result


def verify_seal(directory: Path) -> dict[str, Any]:
    """Verify immutable private payloads without printing their contents."""
    seal = json.loads((directory / "seal.json").read_text(encoding="utf-8"))
    _require(seal.get("version") == 1 and seal.get("name") == "final-large-v1", "invalid_seal")
    files = seal.get("files")
    _require(isinstance(files, dict) and bool(files), "empty_seal")
    for name, expected in files.items():
        _require(isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z0-9_.-]+", name)), "invalid_seal_path")
        _require(isinstance(expected, str) and bool(re.fullmatch(r"[0-9a-f]{64}", expected)), "invalid_seal_digest")
        path = directory / name
        _require(path.is_file() and not path.is_symlink(), "seal_payload_missing_or_symlink")
        _require(sha256(path) == expected, "seal_payload_hash_mismatch")
    return {"seal_valid": True, "sealed_files": len(files), "seal_sha256": sha256(directory / "seal.json")}


def verify_public_commitment(directory: Path, public_seal: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Bind private bytes and aggregate validation to a separately published SHA seal."""
    commitment = json.loads(public_seal.read_text(encoding="utf-8"))
    _require(
        commitment.get("version") == 1
        and commitment.get("name") == "final-large-v1"
        and commitment.get("status") == "sealed",
        "invalid_public_commitment",
    )
    expected = commitment.get("commitments", {})
    for name, field in (
        ("labels.json", "labels_sha256"),
        ("source_receipts.json", "source_receipts_sha256"),
        ("seal.json", "private_seal_sha256"),
    ):
        value = expected.get(field)
        _require(isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value)), "invalid_public_digest")
        path = directory / name
        _require(path.is_file() and not path.is_symlink(), "public_payload_missing_or_symlink")
        _require(sha256(path) == value, "public_commitment_hash_mismatch")
    private_seal = json.loads((directory / "seal.json").read_text(encoding="utf-8"))
    summary = commitment.get("summary")
    _require(isinstance(summary, dict) and summary == private_seal.get("summary"), "public_private_summary_mismatch")
    _require(all(report.get(key) == value for key, value in summary.items()), "public_validation_summary_mismatch")
    source = report["source_footprint_before"]["main"]
    _require(
        source["sha256"] == expected.get("source_database_sha256")
        and source["bytes"] == expected.get("source_database_bytes"),
        "public_source_identity_mismatch",
    )
    _require(
        report.get("known_fixture_hashes") == commitment.get("known_fixture_sha256"), "public_prior_fixture_mismatch"
    )
    private_result = verify_seal(directory)
    return private_result | {"public_commitment_valid": True, "public_seal_sha256": sha256(public_seal)}


def _anchor(connection: sqlite3.Connection, anchor: dict[str, Any]) -> None:
    """Check exact characters, coordinates, identity and attribution independently."""
    row = connection.execute(
        "SELECT p.*,s.ordinal,s.title section_title,s.text section_text,s.book_id section_book_id,"
        "b.work_id,b.language,b.status,r.section_author,r.section_role,r.attribution_basis "
        "FROM passages p JOIN sections s ON s.id=p.section_id JOIN books b ON b.id=p.book_id "
        "LEFT JOIN section_roles r ON r.section_id=s.id WHERE p.id=?",
        (anchor["passage_id"],),
    ).fetchone()
    _require(row is not None, "anchor_missing_passage")
    assert row is not None
    _require(row["status"] == "indexed", "anchor_not_indexed")
    _require(row["book_id"] == row["section_book_id"], "anchor_section_book_mismatch")
    for field in ("book_id", "work_id", "language", "section_id", "section_title"):
        _require(row[field] == anchor.get(field), "anchor_identity_mismatch")
    _require(row["ordinal"] == anchor.get("section_ordinal"), "anchor_ordinal_mismatch")
    for field in ("section_author", "section_role", "attribution_basis"):
        _require(row[field] == anchor.get(field), "anchor_attribution_mismatch")
    start, end = anchor.get("start"), anchor.get("end")
    _require(type(start) is int and type(end) is int, "anchor_coordinates_not_integers")
    assert isinstance(start, int) and isinstance(end, int)
    _require(0 <= start < end <= len(row["section_text"]), "anchor_coordinates_out_of_range")
    _require(anchor.get("text") == row["section_text"][start:end], "anchor_text_mismatch")
    _require(row["start"] <= start < end <= row["end"], "anchor_outside_passage")
    _require(row["text"] == row["section_text"][row["start"] : row["end"]], "source_passage_not_canonical")
    _require(row["start"] == anchor.get("passage_start"), "passage_start_mismatch")
    _require(row["end"] == anchor.get("passage_end"), "passage_end_mismatch")
    actual_hash = hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
    _require(actual_hash == row["sha256"] == anchor.get("passage_sha256"), "passage_hash_mismatch")
    _require(
        hashlib.sha256(row["section_text"].encode("utf-8")).hexdigest() == anchor.get("section_sha256"),
        "section_hash_mismatch",
    )
    needle = anchor.get("match_text")
    _require(isinstance(needle, str) and len(needle) >= 20, "anchor_match_too_short")
    _require(needle in anchor["text"], "anchor_match_not_present")


def validate(directory: Path, source: Path, known_paths: list[Path]) -> dict[str, Any]:
    """Validate private labels and return only aggregate counts and source hashes."""
    labels = json.loads((directory / "labels.json").read_text(encoding="utf-8"))
    receipts = json.loads((directory / "source_receipts.json").read_text(encoding="utf-8"))
    _require(labels.get("version") == 1 and labels.get("split") == "heldout", "not_heldout_v1")
    _require(labels.get("sealed") is True, "not_sealed")
    _require(labels.get("name") == "final-large-v1", "unexpected_set_name")
    _require("not human-rated" in labels.get("ground_truth", {}).get("origin", ""), "missing_rating_limit")
    before = _footprint(source)
    _require(before["-wal"] is None or before["-wal"]["bytes"] == 0, "source_has_pending_wal")
    _require(before["main"] is not None, "source_missing")
    assert before["main"] is not None
    expected_sha = labels.get("source", {}).get("database_sha256")
    _require(before["main"]["sha256"] == expected_sha, "source_database_hash_mismatch")
    _require(
        receipts.get("database_sha256_before") == receipts.get("database_sha256_after") == expected_sha,
        "source_receipts_hash_mismatch",
    )
    _require(before["main"]["bytes"] == receipts.get("database_bytes"), "source_size_mismatch")
    known_queries: set[str] = set()
    known_targets: set[str] = set()
    known_anchors: list[str] = []
    fixture_hashes: dict[str, str] = {}
    for path in known_paths:
        old = json.loads(path.read_text(encoding="utf-8"))
        fixture_hashes[path.name] = sha256(path)
        known_targets.update(old.get("required_book_ids", []))
        for case in old.get("queries", []):
            known_queries.add(_normal(case["query"]))
            known_targets.update(case.get("relevant_book_ids", []))
            known_anchors.extend(
                _normal(text) for text in case.get("evidence_contains", []) if len(_normal(text)) >= 25
            )
    exclusion = receipts.get("separation", {})
    known_targets.update(exclusion.get("excluded_book_ids", []))
    known_queries.update(_normal(text) for text in exclusion.get("known_probe_queries", []))
    cases = labels.get("queries")
    _require(isinstance(cases, list) and cases, "missing_cases")
    seen_ids: set[str] = set()
    seen_queries: set[str] = set()
    target_ids: set[str] = set()
    anchors_checked = 0
    source_languages: Counter[str] = Counter()
    query_languages: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    positive_count = 0
    negative_count = 0
    cross_directions: Counter[str] = Counter()
    connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        for record in receipts.get("books", []):
            fields = list(record)
            _require(all(re.fullmatch(r"[a-z_][a-z_0-9]*", key) for key in fields), "invalid_metadata_key")
            row = connection.execute(f"SELECT {','.join(fields)} FROM books WHERE id=?", (record["id"],)).fetchone()
            _require(row is not None and dict(row) == record, "book_receipt_metadata_mismatch")
        for case in cases:
            _require(isinstance(case, dict), "case_not_object")
            case_id = case.get("id")
            _require(isinstance(case_id, str) and case_id not in seen_ids, "duplicate_or_missing_case_id")
            seen_ids.add(case_id)
            query = case.get("query")
            _require(isinstance(query, str) and bool(query.strip()), "missing_query")
            normal = _normal(query)
            _require(normal not in seen_queries and normal not in known_queries, "duplicate_or_known_query")
            seen_queries.add(normal)
            language = case.get("language")
            _require(language in {"en", "ru", "fr", "de"}, "unsupported_query_language")
            query_languages[language] += 1
            kind = case.get("type")
            _require(
                kind
                in {"exact_quote", "scene", "idea", "explanation", "description", "cross_language", "unanswerable"},
                "unsupported_case_type",
            )
            kinds[kind] += 1
            _require(type(case.get("answerable")) is bool, "missing_answerability")
            if case["answerable"]:
                positive_count += 1
                _require(kind != "unanswerable", "positive_has_negative_type")
                ids = case.get("relevant_book_ids")
                _require(
                    isinstance(ids, list) and bool(ids) and all(isinstance(x, str) for x in ids), "missing_targets"
                )
                _require(not (set(ids) & known_targets), "target_overlaps_prior_or_known_probe")
                target_ids.update(ids)
                anchors = case.get("expected_evidence")
                _require(isinstance(anchors, list) and bool(anchors), "missing_positive_anchors")
                _require({a["book_id"] for a in anchors} == set(ids), "anchors_target_mismatch")
                for anchor in anchors:
                    _anchor(connection, anchor)
                    anchors_checked += 1
                    source_languages[anchor["language"]] += 1
                    matched = _normal(anchor["match_text"])
                    _require(
                        not any(old in matched or matched in old for old in known_anchors), "anchor_overlaps_known"
                    )
                if case.get("relevance_unit") == "book":
                    _require("evidence_contains" not in case, "book_relevance_cannot_require_one_passage")
                else:
                    _require(case.get("evidence_contains") == [anchors[0]["match_text"]], "evidence_gate_mismatch")
                if kind == "cross_language":
                    target_language = anchors[0]["language"]
                    _require(language != target_language == case.get("target_language"), "cross_language_mismatch")
                    cross_directions[f"{language}->{target_language}"] += 1
            else:
                negative_count += 1
                _require(kind == "unanswerable", "negative_type_mismatch")
                _require(case.get("relevant_book_ids") == [], "negative_has_positive_targets")
                scope = case.get("filters", {})
                _require(set(scope) == {"book_id"} and isinstance(scope["book_id"], str), "negative_not_book_scoped")
                anchor = case.get("grounded_elsewhere")
                _require(
                    isinstance(anchor, dict) and anchor.get("book_id") != scope["book_id"],
                    "negative_scope_is_true_source",
                )
                _anchor(connection, anchor)
                anchors_checked += 1
                terms = case.get("distinguishing_terms_absent")
                _require(isinstance(terms, list) and bool(terms), "negative_missing_absence_support")
                _require(all(isinstance(term, str) and len(term) >= 4 for term in terms), "invalid_negative_term")
                _require(bool(case.get("negative_scope_rationale")), "negative_missing_manual_rationale")
                count = 0
                for row in connection.execute(
                    "SELECT text FROM sections WHERE book_id=? ORDER BY ordinal", (scope["book_id"],)
                ):
                    count += 1
                    folded = row[0].casefold()
                    _require(not any(term.casefold() in folded for term in terms), "negative_term_found_in_scoped_book")
                _require(count > 0, "negative_scope_has_no_text")
        indexed_count = connection.execute("SELECT count(*) FROM books WHERE status='indexed'").fetchone()[0]
    finally:
        connection.close()
    after = _footprint(source)
    _require(before == after, "source_changed_during_validation")
    return {
        "valid": True,
        "cases": len(cases),
        "positives": positive_count,
        "scoped_negatives": negative_count,
        "positive_books": len(target_ids),
        "prior_target_overlap": 0,
        "source_indexed_editions": indexed_count,
        "types": dict(sorted(kinds.items())),
        "query_languages": dict(sorted(query_languages.items())),
        "source_languages_by_anchor": dict(sorted(source_languages.items())),
        "cross_language_directions": dict(sorted(cross_directions.items())),
        "exact_anchor_checks": anchors_checked,
        "known_fixture_hashes": fixture_hashes,
        "source_footprint_before": before,
        "source_footprint_after": after,
        "source_unchanged": before == after,
        "retrieval_evaluations_run": 0,
        "ground_truth": "agent-authored, not human-rated",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data/sealed-evals/final-large-v1",
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--known", type=Path, nargs="*", default=[])
    parser.add_argument("--verify-seal", action="store_true")
    parser.add_argument("--public-seal", type=Path)
    arguments = parser.parse_args()
    try:
        directory = arguments.directory.resolve()
        seal = verify_seal(directory) if arguments.verify_seal or arguments.public_seal else {}
        report = validate(directory, arguments.source.resolve(), arguments.known)
        if arguments.public_seal:
            seal |= verify_public_commitment(directory, arguments.public_seal, report)
        print(json.dumps(report | seal, ensure_ascii=False, sort_keys=True))
    except (SealValidationError, OSError, sqlite3.Error, KeyError, TypeError, ValueError) as error:
        # Unexpected malformed input must not reveal a private query through a
        # traceback, exception interpolation, SQLite statement or source excerpt.
        code = str(error) if isinstance(error, SealValidationError) else "fixture_validation_failed"
        print(json.dumps({"valid": False, "error": code}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
