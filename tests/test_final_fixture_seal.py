"""Independent toy-data tests; no sealed queries, models, Library or live writes."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/validate_final_fixture.py"
SPEC = importlib.util.spec_from_file_location("validate_final_fixture", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _refresh_source_receipt(folder: Path, source: Path) -> None:
    labels = json.loads((folder / "labels.json").read_text())
    receipts = json.loads((folder / "source_receipts.json").read_text())
    value = validator.sha256(source)
    labels["source"]["database_sha256"] = value
    receipts["database_sha256_before"] = receipts["database_sha256_after"] = value
    receipts["database_bytes"] = source.stat().st_size
    _write(folder / "labels.json", labels)
    _write(folder / "source_receipts.json", receipts)


@pytest.fixture
def fixture(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    folder = tmp_path / "private"
    folder.mkdir()
    source = tmp_path / "source.sqlite3"
    text = "Cafe\u0301 remains decomposed.\r\n\r\nA tiny robot counts seven blue shells.\n"
    wrong = "Ein leises Gedicht ueber einen ruhigen Winterabend.\n"
    with sqlite3.connect(source) as connection:
        connection.executescript(
            "CREATE TABLE books(id TEXT PRIMARY KEY,work_id TEXT,language TEXT,status TEXT);"
            "CREATE TABLE sections(id INTEGER PRIMARY KEY,book_id TEXT,ordinal INTEGER,title TEXT,text TEXT);"
            "CREATE TABLE passages(id INTEGER PRIMARY KEY,book_id TEXT,section_id INTEGER,start INTEGER,"
            "end INTEGER,text TEXT,sha256 TEXT);"
            "CREATE TABLE section_roles(section_id INTEGER PRIMARY KEY,section_author TEXT,"
            "section_role TEXT,attribution_basis TEXT);"
        )
        connection.executemany(
            "INSERT INTO books VALUES(?,?,?,?)",
            [("toy-alpha", "work-alpha", "en", "indexed"), ("toy-beta", "work-beta", "de", "indexed")],
        )
        connection.executemany(
            "INSERT INTO sections VALUES(?,?,?,?,?)",
            [(1, "toy-alpha", 3, "A small chapter", text), (2, "toy-beta", 7, "Ein Gedicht", wrong)],
        )
        connection.execute(
            "INSERT INTO passages VALUES(?,?,?,?,?,?,?)",
            (10, "toy-alpha", 1, 0, len(text), text, hashlib.sha256(text.encode()).hexdigest()),
        )
    anchor = {
        "book_id": "toy-alpha",
        "work_id": "work-alpha",
        "language": "en",
        "section_id": 1,
        "section_ordinal": 3,
        "section_title": "A small chapter",
        "section_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "start": 0,
        "end": len(text),
        "text": text,
        "match_text": "A tiny robot counts seven blue shells.",
        "passage_id": 10,
        "passage_start": 0,
        "passage_end": len(text),
        "passage_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "section_author": None,
        "section_role": None,
        "attribution_basis": None,
    }
    positive = {
        "id": "toy-1",
        "query": "A robot counting seven blue shells",
        "type": "scene",
        "language": "en",
        "answerable": True,
        "relevant_book_ids": ["toy-alpha"],
        "expected_evidence": [anchor],
        "evidence_contains": [anchor["match_text"]],
        "relevance_unit": "passage",
    }
    negative = {
        "id": "toy-2",
        "query": "Найти робота, считающего семь синих ракушек в выбранной книге",
        "type": "unanswerable",
        "language": "ru",
        "answerable": False,
        "relevant_book_ids": [],
        "filters": {"book_id": "toy-beta"},
        "grounded_elsewhere": copy.deepcopy(anchor),
        "negative_scope_rationale": "The selected poem has no robot episode.",
        "distinguishing_terms_absent": ["robot", "shells"],
    }
    labels = {
        "version": 1,
        "split": "heldout",
        "name": "final-large-v1",
        "sealed": True,
        "source": {"database_sha256": validator.sha256(source)},
        "ground_truth": {"origin": "agent-authored, not human-rated"},
        "queries": [positive, negative],
    }
    receipts = {
        "database_sha256_before": validator.sha256(source),
        "database_sha256_after": validator.sha256(source),
        "database_bytes": source.stat().st_size,
        "books": [{"id": "toy-alpha", "work_id": "work-alpha", "language": "en", "status": "indexed"}],
        "separation": {},
    }
    _write(folder / "labels.json", labels)
    _write(folder / "source_receipts.json", receipts)
    yield folder, source


def test_validation_preserves_source_and_existing_shm(fixture: tuple[Path, Path]) -> None:
    folder, source = fixture
    shm = source.with_name(source.name + "-shm")
    shm.write_bytes(b"owned-test-sentinel-only")
    before = {p.name: validator.sha256(p) for p in (source, shm)}
    result = validator.validate(folder, source, [])
    assert result["valid"] and result["cases"] == 2 and result["exact_anchor_checks"] == 2
    assert result["retrieval_evaluations_run"] == 0
    assert before == {p.name: validator.sha256(p) for p in (source, shm)}
    assert result["source_unchanged"]


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("start", 1, "anchor_text_mismatch"),
        ("end", 10_000, "anchor_coordinates_out_of_range"),
        ("section_id", 2, "anchor_identity_mismatch"),
        ("book_id", "toy-beta", "anchors_target_mismatch"),
        ("passage_sha256", "0" * 64, "passage_hash_mismatch"),
        ("section_author", "Unverified Person", "anchor_attribution_mismatch"),
    ],
)
def test_rejects_anchor_tampering(fixture: tuple[Path, Path], field: str, value: Any, code: str) -> None:
    folder, source = fixture
    labels = json.loads((folder / "labels.json").read_text())
    labels["queries"][0]["expected_evidence"][0][field] = value
    _write(folder / "labels.json", labels)
    with pytest.raises(validator.SealValidationError, match=code):
        validator.validate(folder, source, [])


def test_does_not_normalize_non_nfc_source_text(fixture: tuple[Path, Path]) -> None:
    folder, source = fixture
    labels = json.loads((folder / "labels.json").read_text())
    anchor = labels["queries"][0]["expected_evidence"][0]
    anchor["text"] = unicodedata.normalize("NFC", anchor["text"])
    _write(folder / "labels.json", labels)
    with pytest.raises(validator.SealValidationError, match="anchor_text_mismatch"):
        validator.validate(folder, source, [])


def test_rejects_actual_passage_drift_even_with_matching_claimed_hash(fixture: tuple[Path, Path]) -> None:
    folder, source = fixture
    with sqlite3.connect(source) as connection:
        wrong = "A forged passage text that does not match its chapter."
        connection.execute(
            "UPDATE passages SET text=?,sha256=? WHERE id=10", (wrong, hashlib.sha256(wrong.encode()).hexdigest())
        )
    _refresh_source_receipt(folder, source)
    with pytest.raises(validator.SealValidationError, match="source_passage_not_canonical"):
        validator.validate(folder, source, [])


def test_negative_requires_a_different_explicit_book_scope(fixture: tuple[Path, Path]) -> None:
    folder, source = fixture
    labels = json.loads((folder / "labels.json").read_text())
    labels["queries"][1]["filters"]["book_id"] = "toy-alpha"
    _write(folder / "labels.json", labels)
    with pytest.raises(validator.SealValidationError, match="negative_scope_is_true_source"):
        validator.validate(folder, source, [])


def test_negative_absence_is_checked_against_all_scoped_chapters(fixture: tuple[Path, Path]) -> None:
    folder, source = fixture
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO sections VALUES(3,'toy-beta',8,'Later chapter','A robot appears here.')")
    _refresh_source_receipt(folder, source)
    with pytest.raises(validator.SealValidationError, match="negative_term_found"):
        validator.validate(folder, source, [])


def test_rejects_source_hash_mismatch(fixture: tuple[Path, Path]) -> None:
    folder, source = fixture
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE sections SET title='A different chapter' WHERE id=1")
    with pytest.raises(validator.SealValidationError, match="source_database_hash_mismatch"):
        validator.validate(folder, source, [])


def test_rejects_nonempty_wal_without_opening_sqlite(
    fixture: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, source = fixture
    source.with_name(source.name + "-wal").write_bytes(b"not-a-production-wal")
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("Must reject before SQLite open"))
    with pytest.raises(validator.SealValidationError, match="source_has_pending_wal"):
        validator.validate(folder, source, [])


@pytest.mark.parametrize(
    "known_case,code",
    [
        ({"query": "A robot counting seven blue shells"}, "duplicate_or_known_query"),
        ({"query": "other", "relevant_book_ids": ["toy-alpha"]}, "target_overlaps_prior"),
        ({"query": "other", "evidence_contains": ["A tiny robot counts seven blue shells."]}, "anchor_overlaps_known"),
    ],
)
def test_rejects_prior_fixture_overlap(fixture: tuple[Path, Path], known_case: dict[str, Any], code: str) -> None:
    folder, source = fixture
    path = folder / "known.json"
    _write(path, {"queries": [known_case]})
    with pytest.raises(validator.SealValidationError, match=code):
        validator.validate(folder, source, [path])


def test_source_change_during_validation_discards_report(
    fixture: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, source = fixture
    original = validator._footprint
    calls = 0

    def changing_footprint(path: Path) -> dict[str, dict[str, Any] | None]:
        nonlocal calls
        calls += 1
        if calls == 2:
            with sqlite3.connect(source) as connection:
                connection.execute("UPDATE sections SET title='Changed while reading' WHERE id=2")
        return original(path)

    monkeypatch.setattr(validator, "_footprint", changing_footprint)
    with pytest.raises(validator.SealValidationError, match="source_changed_during_validation"):
        validator.validate(folder, source, [])


def test_cli_outputs_only_aggregate_values(fixture: tuple[Path, Path]) -> None:
    folder, source = fixture
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--directory", str(folder), "--source", str(source)],
        capture_output=True,
        check=True,
        text=True,
    )
    assert json.loads(result.stdout)["cases"] == 2
    assert "blue shells" not in result.stdout and "toy-alpha" not in result.stdout and "robot" not in result.stdout


@pytest.mark.parametrize("mutation", ["bytes", "symlink", "path"])
def test_manifest_seal_rejects_modified_or_redirected_payloads(tmp_path: Path, mutation: str) -> None:
    payload = tmp_path / "payload.json"
    payload.write_bytes(b'{"sealed":true}\n')
    seal = {"version": 1, "name": "final-large-v1", "files": {"payload.json": validator.sha256(payload)}}
    _write(tmp_path / "seal.json", seal)
    assert validator.verify_seal(tmp_path)["seal_valid"]
    if mutation == "bytes":
        payload.write_bytes(b'{"sealed":false}\n')
    elif mutation == "symlink":
        target = tmp_path / "elsewhere.json"
        target.write_bytes(payload.read_bytes())
        payload.unlink()
        payload.symlink_to(target)
    else:
        seal["files"] = {"../payload.json": validator.sha256(payload)}
        _write(tmp_path / "seal.json", seal)
    with pytest.raises(validator.SealValidationError):
        validator.verify_seal(tmp_path)


def _public_fixture(folder: Path, source: Path) -> tuple[Path, dict[str, Any]]:
    """Publish only toy aggregate commitments, never use a real sealed fixture."""
    report = validator.validate(folder, source, [])
    summary = {key: report[key] for key in ("cases", "positives", "scoped_negatives", "query_languages", "types")}
    private = {
        "version": 1,
        "name": "final-large-v1",
        "files": {name: validator.sha256(folder / name) for name in ("labels.json", "source_receipts.json")},
        "summary": summary,
    }
    _write(folder / "seal.json", private)
    public = {
        "version": 1,
        "name": "final-large-v1",
        "status": "sealed",
        "summary": summary,
        "known_fixture_sha256": {},
        "commitments": {
            "labels_sha256": validator.sha256(folder / "labels.json"),
            "source_receipts_sha256": validator.sha256(folder / "source_receipts.json"),
            "private_seal_sha256": validator.sha256(folder / "seal.json"),
            "source_database_sha256": validator.sha256(source),
            "source_database_bytes": source.stat().st_size,
        },
    }
    path = folder / "aggregate.seal.json"
    _write(path, public)
    return path, report


def test_public_commitment_binds_private_payload_and_source(fixture: tuple[Path, Path]) -> None:
    folder, source = fixture
    path, report = _public_fixture(folder, source)
    result = validator.verify_public_commitment(folder, path, report)
    assert result["public_commitment_valid"] and result["seal_valid"]
    assert result["public_seal_sha256"] == validator.sha256(path)


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("labels_digest", "public_commitment_hash_mismatch"),
        ("source_digest", "public_source_identity_mismatch"),
        ("source_size", "public_source_identity_mismatch"),
        ("summary", "public_private_summary_mismatch"),
        ("prior_fixtures", "public_prior_fixture_mismatch"),
    ],
)
def test_public_commitment_rejects_mismatches(fixture: tuple[Path, Path], mutation: str, code: str) -> None:
    folder, source = fixture
    path, report = _public_fixture(folder, source)
    public = json.loads(path.read_text())
    if mutation == "labels_digest":
        public["commitments"]["labels_sha256"] = "0" * 64
    elif mutation == "source_digest":
        public["commitments"]["source_database_sha256"] = "0" * 64
    elif mutation == "source_size":
        public["commitments"]["source_database_bytes"] += 1
    elif mutation == "summary":
        public["summary"]["cases"] += 1
    else:
        public["known_fixture_sha256"] = {"synthetic-dev.json": "0" * 64}
    _write(path, public)
    with pytest.raises(validator.SealValidationError, match=code):
        validator.verify_public_commitment(folder, path, report)


def test_changed_private_payload_and_new_private_seal_cannot_replace_public_commitment(
    fixture: tuple[Path, Path],
) -> None:
    folder, source = fixture
    path, _ = _public_fixture(folder, source)
    labels = json.loads((folder / "labels.json").read_text())
    labels["queries"][0]["query"] = "Another invented query about a counting machine and blue shells"
    _write(folder / "labels.json", labels)
    private = json.loads((folder / "seal.json").read_text())
    private["files"]["labels.json"] = validator.sha256(folder / "labels.json")
    _write(folder / "seal.json", private)
    assert validator.verify_seal(folder)["seal_valid"]
    changed_report = validator.validate(folder, source, [])
    with pytest.raises(validator.SealValidationError, match="public_commitment_hash_mismatch"):
        validator.verify_public_commitment(folder, path, changed_report)


def test_public_commitment_cannot_validate_a_different_aggregate_report(fixture: tuple[Path, Path]) -> None:
    folder, source = fixture
    path, report = _public_fixture(folder, source)
    report["cases"] += 1
    with pytest.raises(validator.SealValidationError, match="public_validation_summary_mismatch"):
        validator.verify_public_commitment(folder, path, report)


def test_public_cli_verifies_commitment_without_leaking_toy_cases(fixture: tuple[Path, Path]) -> None:
    folder, source = fixture
    path, _ = _public_fixture(folder, source)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--directory", str(folder), "--source", str(source), "--public-seal", str(path)],
        capture_output=True,
        check=True,
        text=True,
    )
    assert json.loads(result.stdout)["public_commitment_valid"]
    assert "blue shells" not in result.stdout and "toy-alpha" not in result.stdout


def test_cli_malformed_private_payload_fails_without_content_or_traceback(fixture: tuple[Path, Path]) -> None:
    folder, source = fixture
    (folder / "labels.json").write_text('{"private":"DO_NOT_PRINT_THIS_QUERY_CONTENT", broken', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--directory", str(folder), "--source", str(source)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 2 and not result.stdout
    assert json.loads(result.stderr) == {"valid": False, "error": "fixture_validation_failed"}
    assert "DO_NOT_PRINT" not in result.stderr and "Traceback" not in result.stderr
