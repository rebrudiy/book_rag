"""Offline catalog safety, rights isolation and resumable discovery."""

from __future__ import annotations

import csv
import io
import json
import tarfile
from pathlib import Path
from xml.etree.ElementTree import ParseError

import pytest

from book_rag.catalog import discover_gutenberg
from book_rag.sources import load_manifest


def rdf_record(
    number: str = "11",
    rights: str = "Public domain in the USA.",
    language: str = "en",
    kind: str = "Text",
    media_type: str = "application/epub+zip",
    url: str | None = None,
) -> bytes:
    url = url or f"https://www.gutenberg.org/ebooks/{number}.epub3.images"
    return f'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns:pg="http://www.gutenberg.org/2009/pgterms/" xmlns:dc="http://purl.org/dc/terms/">
      <pg:ebook rdf:about="ebooks/{number}">
        <dc:title>Book {number}</dc:title><dc:rights>{rights}</dc:rights><dc:issued>2000-01-01</dc:issued>
        <dc:creator><pg:agent><pg:name>Writer</pg:name></pg:agent></dc:creator>
        <dc:language><rdf:Description><rdf:value>{language}</rdf:value></rdf:Description></dc:language>
        <dc:type><rdf:Description><rdf:value>{kind}</rdf:value></rdf:Description></dc:type>
        <dc:hasFormat><pg:file rdf:about="{url}"><dc:format><rdf:Description>
          <rdf:value>{media_type}</rdf:value></rdf:Description></dc:format></pg:file></dc:hasFormat>
      </pg:ebook></rdf:RDF>'''.encode()


def write_archive(path: Path, records: list[bytes]) -> None:
    with tarfile.open(path, "w:bz2") as archive:
        for i, data in enumerate(records):
            # Catalog member names are never extracted to the filesystem.
            member = tarfile.TarInfo(f"../../untrusted/pg{i}.rdf")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


def test_offline_rdf_discovery_builds_valid_manifest(tmp_path: Path) -> None:
    source, output = tmp_path / "pg11.rdf", tmp_path / "manifest.json"
    source.write_bytes(rdf_record())
    report = discover_gutenberg(source, output)
    assert report["added"] == 1
    entry = load_manifest(output)[0]
    assert entry["id"] == "pg11-full"
    assert entry["work_id"] == "pg11-alice"
    assert entry["download_url"] == "https://www.gutenberg.org/cache/epub/11/pg11-images-3.epub"
    assert entry["coverage"] == "full"
    assert len(entry["catalog_sha256"]) == 64


def test_catalog_append_limit_resumes_without_duplicates(tmp_path: Path) -> None:
    source, output = tmp_path / "catalog.tar.bz2", tmp_path / "manifest.json"
    write_archive(source, [rdf_record(str(i)) for i in [11, 12, 13]])
    first = discover_gutenberg(source, output, limit=2)
    assert (first["added"], first["total"]) == (2, 2)
    second = discover_gutenberg(source, output, limit=2)
    assert (second["added"], second["already_present"], second["total"]) == (1, 2, 3)
    third = discover_gutenberg(source, output, limit=2)
    assert (third["added"], third["total"]) == (0, 3)
    assert len({e["id"] for e in json.loads(output.read_text())}) == 3
    assert not (tmp_path.parent / "untrusted").exists()


@pytest.mark.parametrize("rights", ["Copyrighted. Read the copyright notice.", "", "Public domain", "CC BY 4.0"])
def test_rights_are_explicit_per_edition(tmp_path: Path, rights: str) -> None:
    source, output = tmp_path / "pg11.rdf", tmp_path / "manifest.json"
    source.write_bytes(rdf_record(rights=rights))
    report = discover_gutenberg(source, output)
    assert report["added"] == 0
    assert report["skipped_rights"] == 1


def test_audio_catalog_records_are_excluded(tmp_path: Path) -> None:
    source, output = tmp_path / "audio.rdf", tmp_path / "manifest.json"
    source.write_bytes(rdf_record(kind="Sound"))
    assert discover_gutenberg(source, output)["skipped_type"] == 1


def test_language_filter_and_work_edition_alias(tmp_path: Path) -> None:
    source, output = tmp_path / "catalog.tar.bz2", tmp_path / "manifest.json"
    write_archive(source, [rdf_record("1228"), rdf_record("2009"), rdf_record("4650", language="fr")])
    report = discover_gutenberg(source, output, languages=["en"])
    assert report["skipped_language"] == 1
    entries = load_manifest(output)
    assert len(entries) == 2
    assert entries[0]["work_id"] == entries[1]["work_id"] == "pg1228-origin"
    assert entries[0]["id"] != entries[1]["id"]


@pytest.mark.parametrize("url", ["https://evil.invalid/book.epub", "file:///etc/passwd", "javascript:alert(1)"])
def test_download_candidates_are_provider_scoped(tmp_path: Path, url: str) -> None:
    source, output = tmp_path / "book.rdf", tmp_path / "manifest.json"
    source.write_bytes(rdf_record(url=url))
    assert discover_gutenberg(source, output)["skipped_format"] == 1


def test_official_csv_without_rights_is_not_an_allowlist(tmp_path: Path) -> None:
    source, output = tmp_path / "catalog.csv", tmp_path / "manifest.json"
    source.write_text(
        "Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves\n"
        '5200,Text,2005-08-17,Metamorphosis,en,"Kafka, Franz",Psychology,PT,Fiction\n'
    )
    report = discover_gutenberg(source, output)
    assert (report["added"], report["skipped_rights"]) == (0, 1)


def test_enriched_csv_with_verified_rights_and_download(tmp_path: Path) -> None:
    source, output = tmp_path / "catalog.csv", tmp_path / "manifest.json"
    fields = ["Text#", "Type", "Issued", "Title", "Language", "Authors", "Rights", "Download URL"]
    with source.open("w") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerow(
            {
                "Text#": "11",
                "Type": "Text",
                "Title": "Alice",
                "Language": "en",
                "Rights": "Public domain in the USA.",
                "Download URL": "https://www.gutenberg.org/cache/epub/11/pg11-images-3.epub",
            }
        )
    assert discover_gutenberg(source, output)["added"] == 1


def test_invalid_catalog_preserves_previous_output(tmp_path: Path) -> None:
    source, output = tmp_path / "catalog.rdf", tmp_path / "manifest.json"
    source.write_bytes(b"<broken")
    output.write_text('[{"id":"existing"}]')
    original = output.read_bytes()
    with pytest.raises(ParseError):
        discover_gutenberg(source, output)
    assert output.read_bytes() == original


def test_invalid_limit_fails_before_rewriting(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        discover_gutenberg(tmp_path / "missing.rdf", tmp_path / "out.json", limit=0)


def test_curated_and_provisional_work_identities_are_explicit(tmp_path: Path) -> None:
    source, output = tmp_path / "catalog.tar.bz2", tmp_path / "manifest.json"
    write_archive(source, [rdf_record("11"), rdf_record("61"), rdf_record("999999")])
    discover_gutenberg(source, output)
    entries = load_manifest(output)
    assert [e["work_identity_status"] for e in entries] == ["curated", "curated", "provisional"]
    assert all(e["work_type"] == "book_or_text" for e in entries)


@pytest.mark.parametrize(
    "title,is_issue",
    [
        ("Punchinello, Volume 1, No. 09, May 28, 1870", True),
        ("The Mirror of Literature. Volume 10, No. 268, August 11, 1827", True),
        ("Science Magazine, Issue 12", True),
        ("The Works of Aphra Behn, Volume III", False),
        ("A textbook of mathematics", False),
    ],
)
def test_issue_classification_requires_explicit_metadata(tmp_path: Path, title: str, is_issue: bool) -> None:
    source, output = tmp_path / "book.rdf", tmp_path / "manifest.json"
    source.write_bytes(rdf_record().replace(b"Book 11", title.encode()))
    discover_gutenberg(source, output)
    assert (load_manifest(output)[0]["work_type"] == "periodical_issue") is is_issue


def test_periodicals_subject_is_preserved_and_classified(tmp_path: Path) -> None:
    source, output = tmp_path / "book.rdf", tmp_path / "manifest.json"
    subject = b"<dc:subject><rdf:Description><rdf:value>English periodicals</rdf:value></rdf:Description></dc:subject>"
    source.write_bytes(rdf_record().replace(b"</pg:ebook>", subject + b"</pg:ebook>"))
    discover_gutenberg(source, output)
    entry = load_manifest(output)[0]
    assert entry["work_type"] == "periodical_issue"
    assert "English periodicals" in entry["subjects"]


def test_resume_backfills_work_classification_without_duplicate_downloads(tmp_path: Path) -> None:
    source, output = tmp_path / "book.rdf", tmp_path / "manifest.json"
    source.write_bytes(rdf_record())
    discover_gutenberg(source, output)
    entries = json.loads(output.read_text())
    entries[0].pop("work_identity_status")
    entries[0].pop("work_type")
    output.write_text(json.dumps(entries))
    report = discover_gutenberg(source, output)
    assert report["added"] == 0
    assert report["already_present"] == 1
    entry = load_manifest(output)[0]
    assert (entry["work_identity_status"], entry["work_type"]) == ("curated", "book_or_text")


def test_duplicate_catalog_records_keep_single_identity(tmp_path: Path) -> None:
    source, output = tmp_path / "catalog.tar.bz2", tmp_path / "manifest.json"
    write_archive(source, [rdf_record("11"), rdf_record("11"), rdf_record("12")])
    report = discover_gutenberg(source, output, limit=10)
    assert (report["added"], report["already_present"], report["total"]) == (2, 1, 2)
