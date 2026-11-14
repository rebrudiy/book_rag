"""Rights-verified, deterministic, balanced offline acquisition planning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from book_rag import corpus_planning
from book_rag.corpus_planning import classify_subjects, plan_balanced_corpus
from book_rag.sources import load_manifest


def row(number: int, language: str = "en", subjects: list[str] | None = None, **overrides: object) -> dict:
    value = {
        "number": str(number),
        "title": f"Book {number}",
        "author": "Writer",
        "languages": [language],
        "type": "Text",
        "rights": "Public domain in the USA.",
        "issued": "2000-01-01",
        "subjects": subjects or [],
        "formats": [
            (f"https://www.gutenberg.org/cache/epub/{number}/pg{number}-images-3.epub", "application/epub+zip")
        ],
    }
    value.update(overrides)
    return value


def plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[dict], **options: Any) -> tuple[dict, list[dict]]:
    source, output = tmp_path / "catalog.rdf", tmp_path / "balanced.json"
    source.write_text("Fixed offline catalog fixture")
    monkeypatch.setattr(corpus_planning, "_rows", lambda path: iter(rows))

    def disallow_http(**kwargs: object) -> None:
        raise AssertionError("Planning must never access HTTP")

    monkeypatch.setattr(httpx, "Client", disallow_http)
    report = plan_balanced_corpus(source, output, **options)
    return report, load_manifest(output)


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("Historical fiction", "fiction"),
        ("Ethics", "philosophy"),
        ("France -- History", "history"),
        ("Physics", "science"),
        ("Political economy", "economics"),
        ("Autobiography", "biography"),
        ("Mechanical engineering", "technical"),
        ("Unfamiliar subject", "other"),
    ],
)
def test_subject_classification_is_grounded_in_explicit_terms(subject: str, expected: str) -> None:
    result = classify_subjects([subject])
    assert result["primary"] == expected
    if expected != "other":
        assert subject in result["basis"][expected]
    else:
        assert result["basis"] == {}


def test_nonfiction_and_criticism_are_not_asserted_as_fiction() -> None:
    for subject in ("Non-fiction", "Fiction -- History and criticism"):
        assert "fiction" not in classify_subjects([subject])["areas"]


def test_rights_formats_and_periodicals_are_filtered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        row(11, subjects=["Fantasy fiction"]),
        row(12, rights="Copyrighted."),
        row(13, rights=""),
        row(14, type="Sound"),
        row(15, subjects=["English periodicals"]),
        row(16, title="Punchinello, Volume 1, No. 09"),
        row(17, formats=[("https://www.gutenberg.org/book.html", "text/html")]),
    ]
    report, entries = plan(tmp_path, monkeypatch, rows, languages=["en"], per_language=20)
    assert [e["id"] for e in entries] == ["pg11-full"]
    assert report["skipped_rights"] == 2
    assert report["skipped_type"] == 1
    assert report["skipped_periodical_issue"] == 2
    assert report["skipped_format"] == 1
    assert entries[0]["work_identity_status"] == "curated"


def test_subject_round_robin_balances_every_available_area(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subjects = ["Fiction", "Philosophy", "History", "Physics", "Economics", "Biography", "Engineering", "Unknown"]
    rows = [
        row(1000 + language_index * 100 + area * 10 + index, language, [subject])
        for language_index, language in enumerate(["en", "fr"])
        for area, subject in enumerate(subjects)
        for index in range(4)
    ]
    report, entries = plan(tmp_path, monkeypatch, rows, languages=["en", "fr"], per_language=8)
    assert len(entries) == 16
    assert report["selected_by_language"] == {"en": 8, "fr": 8}
    assert all(n == 1 for counts in report["selected_by_language_and_subject"].values() for n in counts.values())
    assert all(e["work_identity_status"] == "provisional" for e in entries)


def test_unknown_subjects_do_not_gain_labels_from_titles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, entries = plan(
        tmp_path,
        monkeypatch,
        [row(999999, title="Physics and philosophy", subjects=[])],
        languages=["en"],
        per_language=1,
    )
    assert entries[0]["primary_subject_area"] == "other"
    assert entries[0]["subject_classification"]["basis"] == {}


def test_shortages_are_reported_without_replacing_rare_language_with_english(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [row(n, "en", ["Fiction"]) for n in range(100, 110)] + [row(5316, "ru", ["Poetry"])]
    report, entries = plan(tmp_path, monkeypatch, rows, languages=["en", "ru"], per_language=4)
    assert report["selected_by_language"] == {"en": 4, "ru": 1}
    assert report["language_shortages"] == {"en": 0, "ru": 3}
    assert len(entries) == 5


def test_multilingual_source_ids_are_assigned_once_with_rare_language_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [row(5316, languages=["en", "ru"], subjects=["Poetry"])] + [row(n, "en") for n in range(100, 110)]
    report, entries = plan(tmp_path, monkeypatch, rows, languages=["en", "ru"], per_language=2)
    assert report["selection_language_order"] == ["ru", "en"]
    russian = next(e for e in entries if e["id"] == "pg5316-full")
    assert russian["planning_language"] == "ru"
    assert russian["languages"] == ["en", "ru"]
    assert len(entries) == len({e["id"] for e in entries}) == 3


def test_output_is_deterministic_independent_of_record_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [row(n, "en", ["Physics" if n % 2 else "Fiction"]) for n in range(100, 200)]
    first, entries = plan(tmp_path, monkeypatch, rows, languages=["en"], per_language=10)
    manifest_bytes = (tmp_path / "balanced.json").read_bytes()
    second, repeated = plan(tmp_path, monkeypatch, list(reversed(rows)), languages=["en"], per_language=10)
    assert entries == repeated
    assert (tmp_path / "balanced.json").read_bytes() == manifest_bytes
    assert first["catalog_editions_selected"] == second["catalog_editions_selected"] == 10


def test_reservoir_is_bounded_and_subject_shortages_spill_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report, entries = plan(
        tmp_path, monkeypatch, [row(n, "en", ["Fiction"]) for n in range(1000, 3000)], languages=["en"], per_language=3
    )
    assert report["eligible_records"] == 2000
    assert report["candidate_slots_retained"] == 3
    assert len(entries) == 3
    assert report["language_shortages"]["en"] == 0


def test_duplicate_rows_never_create_duplicate_output_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report, entries = plan(tmp_path, monkeypatch, [row(11), row(11), row(84)], languages=["en"], per_language=10)
    assert report["catalog_editions_selected"] == len({e["id"] for e in entries}) == 2
    assert "not a verified" in report["work_identity_note"]
    assert json.loads((tmp_path / "balanced.report.json").read_text())["language_shortages"]["en"] == 8


def test_utf8_text_is_eligible_without_epub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    formats = [("https://www.gutenberg.org/files/11/11-0.txt", "text/plain; charset=utf-8")]
    _, entries = plan(tmp_path, monkeypatch, [row(11, formats=formats)], languages=["en"], per_language=1)
    assert entries[0]["format"] == "txt"


@pytest.mark.parametrize("options", [{"per_language": 0}, {"languages": []}, {"languages": ["../../bad"]}])
def test_invalid_options_fail_before_output(tmp_path: Path, options: dict) -> None:
    with pytest.raises(ValueError):
        plan_balanced_corpus(tmp_path / "missing.rdf", tmp_path / "out.json", **options)
