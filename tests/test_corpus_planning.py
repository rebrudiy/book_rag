"""Rights-verified, deterministic, balanced offline acquisition planning."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

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
    monkeypatch.setattr(corpus_planning, "_text_rows", lambda path: iter(rows))

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


def text_formats(number: int) -> list[tuple[str, str]]:
    return [
        (f"https://www.gutenberg.org/cache/epub/{number}/pg{number}-images-3.epub", "application/epub+zip"),
        (f"https://www.gutenberg.org/ebooks/{number}.epub.noimages", "application/epub+zip"),
        (f"https://www.gutenberg.org/files/{number}/{number}-0.txt", "text/plain; charset=utf-8"),
        (f"https://www.gutenberg.org/files/{number}/{number}-h/{number}-h.htm", "text/html"),
    ]


def test_text_only_prefers_explicit_noimage_epub_and_records_alias_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, entries = plan(
        tmp_path,
        monkeypatch,
        [row(11, formats=text_formats(11), catalog_record_sha256="a" * 64, catalog_member="cache/epub/11/pg11.rdf")],
        text_only=True,
        languages=["en"],
        per_language=1,
    )
    entry = entries[0]
    assert entry["format"] == "epub"
    assert entry["download_url"] == "https://www.gutenberg.org/cache/epub/11/pg11.epub"
    assert entry["illustrations_included"] is False
    assert entry["coverage_scope"] == "text_only_without_illustrations"
    assert entry["work_id"] == "pg11-alice"
    assert entry["work_identity_status"] == "curated"
    variant = entry["source_variant"]
    assert variant["catalog_format_url"] == "https://www.gutenberg.org/ebooks/11.epub.noimages"
    assert variant["format_family"] == "epub_noimages"
    assert variant["url_resolution"] == "existing_gutenberg_generated_epub_noimages_alias_mapping"
    assert variant["original_image_format"]["catalog_url"].endswith("pg11-images-3.epub")
    assert variant["catalog_record_sha256"] == "a" * 64
    assert variant["catalog_record"] == "cache/epub/11/pg11.rdf"
    assert variant["same_gutenberg_id"] == "11"
    assert "visual equations and diagrams" in variant["omitted_content"]
    assert report["selected_by_format"] == {"epub": 1}
    assert report["selected_by_format_family"] == {"epub_noimages": 1}
    assert report["artifact_sizes"]["known"] is False
    assert report["artifact_sizes"]["estimated_bytes"] is None


def test_direct_rdf_noimage_url_is_preserved_without_synthesizing_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://gutenberg.org/cache/epub/11/pg11.epub"
    _, entries = plan(
        tmp_path,
        monkeypatch,
        [row(11, formats=[(url, "application/epub+zip")])],
        text_only=True,
        languages=["en"],
        per_language=1,
    )
    assert entries[0]["download_url"] == url
    assert entries[0]["source_variant"]["catalog_format_url"] == url
    assert entries[0]["source_variant"]["url_resolution"] == "exact_catalog_url"
    assert entries[0]["source_variant"]["original_image_format"] is None


def test_utf8_text_then_html_fallback_and_image_only_records_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, entries = plan(
        tmp_path,
        monkeypatch,
        [
            row(11, formats=[text_formats(11)[0], *text_formats(11)[2:]]),
            row(12, formats=[text_formats(12)[-1]]),
            row(13),
        ],
        text_only=True,
        languages=["en"],
        per_language=10,
    )
    by_id = {entry["id"]: entry for entry in entries}
    assert by_id["pg11-full"]["format"] == "txt"
    assert by_id["pg12-full"]["format"] == "html"
    assert by_id["pg12-full"]["work_identity_status"] == "provisional"
    assert "pg13-full" not in by_id
    assert report["skipped_format"] == 1
    assert report["selected_by_format"] == {"html": 1, "txt": 1}
    assert report["selected_by_format_family"] == {"html_text": 1, "txt_utf8": 1}


@pytest.mark.parametrize(
    "url,media",
    [
        ("http://www.gutenberg.org/ebooks/11.epub.noimages", "application/epub+zip"),
        ("https://evil.test/ebooks/11.epub.noimages", "application/epub+zip"),
        ("https://www.gutenberg.org:443/ebooks/11.epub.noimages", "application/epub+zip"),
        ("https://www.gutenberg.org/ebooks/12.epub.noimages", "application/epub+zip"),
        ("https://www.gutenberg.org/files/12/book.htm", "text/html"),
        ("https://www.gutenberg.org/files/11/../12/book.htm", "text/html"),
        ("https://www.gutenberg.org/files/11/%252e%252e/12/book.htm", "text/html"),
        ("https://www.gutenberg.org/files/11/book.htm?other=12", "text/html"),
        ("https://www.gutenberg.org/files/11/book.txt", "text/plain; charset=iso-8859-1"),
        ("https://www.gutenberg.org/files/11/book.txt", "text/plain"),
        ("https://www.gutenberg.org/cache/epub/11/pg11-images-3.epub", "application/epub+zip"),
    ],
)
def test_text_only_rejects_unsafe_mismatched_or_nontext_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, media: str
) -> None:
    report, entries = plan(
        tmp_path, monkeypatch, [row(11, formats=[(url, media)])], text_only=True, languages=["en"], per_language=1
    )
    assert not entries
    assert report["skipped_format"] == 1
    assert report["language_shortages"] == {"en": 1}


def test_text_only_retains_rights_type_language_and_periodical_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        row(11, formats=text_formats(11)),
        row(12, formats=text_formats(12), rights="Copyrighted."),
        row(13, formats=text_formats(13), rights=""),
        row(14, formats=text_formats(14), type="Sound"),
        row(15, formats=text_formats(15), subjects=["Periodicals"]),
        row(16, "es", formats=text_formats(16)),
    ]
    report, entries = plan(tmp_path, monkeypatch, rows, text_only=True, languages=["en"], per_language=10)
    assert [entry["id"] for entry in entries] == ["pg11-full"]
    assert report["skipped_rights"] == 2
    assert report["skipped_type"] == 1
    assert report["skipped_periodical_issue"] == 1
    assert report["skipped_language"] == 1


def test_default_false_preserves_previous_manifest_and_image_format_preference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [row(number, formats=text_formats(number)) for number in range(100, 130)]
    _, implicit = plan(tmp_path, monkeypatch, rows, languages=["en"], per_language=10)
    before = (tmp_path / "balanced.json").read_bytes()
    _, explicit = plan(tmp_path, monkeypatch, rows, text_only=False, languages=["en"], per_language=10)
    assert implicit == explicit
    assert (tmp_path / "balanced.json").read_bytes() == before
    assert all(entry["download_url"].endswith("-images-3.epub") for entry in explicit)
    assert all("illustrations_included" not in entry and "source_variant" not in entry for entry in explicit)


def test_text_only_selection_and_format_choice_stable_under_catalog_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        row(number, formats=text_formats(number), subjects=["Physics" if number % 2 else "Fiction"])
        for number in range(100, 200)
    ]
    _, first = plan(tmp_path, monkeypatch, rows, text_only=True, languages=["en"], per_language=10)
    reversed_rows = [{**value, "formats": list(reversed(value["formats"]))} for value in reversed(rows)]
    report, second = plan(tmp_path, monkeypatch, reversed_rows, text_only=True, languages=["en"], per_language=10)
    assert first == second
    assert report["candidate_slots_retained"] <= report["candidate_capacity"]


def _rdf_record(number: int, rights: str = "Public domain in the USA.") -> bytes:
    formats = "".join(
        "<dc:hasFormat><pg:file rdf:about="
        + quoteattr(url)
        + "><dc:format><rdf:Description><rdf:value>"
        + escape(media)
        + "</rdf:value></rdf:Description></dc:format></pg:file></dc:hasFormat>"
        for url, media in text_formats(number)
    )
    return (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:pg="http://www.gutenberg.org/2009/pgterms/" xmlns:dc="http://purl.org/dc/terms/">'
        f'<pg:ebook rdf:about="ebooks/{number}"><dc:title>Book {number}</dc:title><dc:rights>'
        + escape(rights)
        + "</dc:rights><dc:type><rdf:Description><rdf:value>Text</rdf:value></rdf:Description>"
        "</dc:type><dc:language><rdf:Description><rdf:value>en</rdf:value></rdf:Description></dc:language>"
        + formats
        + "</pg:ebook></rdf:RDF>"
    ).encode()


def test_real_streaming_rdf_record_hash_and_no_archive_extraction(tmp_path: Path) -> None:
    catalog = tmp_path / "records.tar.bz2"
    data = _rdf_record(11)
    with tarfile.open(catalog, "w:bz2") as archive:
        member = tarfile.TarInfo("../../not-extracted/pg11.rdf")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
        copyrighted = _rdf_record(12, rights="Copyrighted.")
        member = tarfile.TarInfo("cache/epub/12/pg12.rdf")
        member.size = len(copyrighted)
        archive.addfile(member, io.BytesIO(copyrighted))
    report = plan_balanced_corpus(catalog, tmp_path / "result.json", per_language=10, languages=["en"], text_only=True)
    entry = load_manifest(tmp_path / "result.json")[0]
    assert report["records_scanned"] == 2
    assert report["skipped_rights"] == 1
    assert entry["source_variant"]["catalog_record_sha256"] == hashlib.sha256(data).hexdigest()
    assert entry["source_variant"]["catalog_record"] == "../../not-extracted/pg11.rdf"
    assert not (tmp_path / "not-extracted").exists()
    assert len(entry["source_variant"]["catalog_sha256"]) == 64


def test_text_only_rejects_csv_without_raw_rdf_provenance(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.csv"
    catalog.write_text("Text#,Title,Language,Type\n11,Alice,en,Text\n")
    with pytest.raises(ValueError, match="requires official RDF"):
        plan_balanced_corpus(catalog, tmp_path / "out.json", text_only=True)
