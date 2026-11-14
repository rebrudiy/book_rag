"""Source variants require exact same-edition, rights-verified RDF alternatives."""

from __future__ import annotations

import copy
import io
import tarfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import httpx
import pytest

from book_rag.source_variants import propose_variants


def source_entry(number: int = 63371) -> dict:
    return {
        "id": f"pg{number}-full",
        "work_id": "curated-botany",
        "title": "Botany",
        "author": "Authors",
        "edition": "Specific original electronic edition",
        "language": "de",
        "source_url": f"https://www.gutenberg.org/ebooks/{number}",
        "download_url": f"https://www.gutenberg.org/cache/epub/{number}/pg{number}-images-3.epub",
        "format": "epub",
        "rights": "Public domain in the USA. Check local law.",
        "rights_url": "https://www.gutenberg.org/policy/license.html",
        "section_attributions": [{"title_contains": "Introduction", "author": "Editor", "role": "editorial"}],
    }


def rdf(formats: list[tuple[str, str]], number: int = 63371, rights: str = "Public domain in the USA.") -> bytes:
    resources = "".join(
        "<dc:hasFormat><pg:file rdf:about=" + quoteattr(url) + "><dc:format><rdf:Description>"
        "<rdf:value>" + escape(media) + "</rdf:value></rdf:Description></dc:format></pg:file></dc:hasFormat>"
        for url, media in formats
    )
    return (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:pg="http://www.gutenberg.org/2009/pgterms/" xmlns:dc="http://purl.org/dc/terms/">'
        f'<pg:ebook rdf:about="ebooks/{number}"><dc:title>Botany</dc:title><dc:rights>'
        + escape(rights)
        + "</dc:rights><dc:type><rdf:Description><rdf:value>Text</rdf:value></rdf:Description></dc:type>"
        + resources
        + "</pg:ebook></rdf:RDF>"
    ).encode()


def default_formats(number: int = 63371) -> list[tuple[str, str]]:
    return [
        (f"https://www.gutenberg.org/ebooks/{number}.epub.noimages", "application/epub+zip"),
        (f"https://www.gutenberg.org/ebooks/{number}.txt.utf-8", "text/plain; charset=utf-8"),
        (f"https://www.gutenberg.org/files/{number}/{number}-h/{number}-h.htm", "text/html"),
    ]


def test_alternatives_keep_identity_curation_and_follow_preferred_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.rdf"
    formats = default_formats()
    path.write_bytes(rdf(formats))

    def no_http(**kwargs: object) -> None:
        raise AssertionError("Variant planning must not contact Gutenberg")

    monkeypatch.setattr(httpx, "Client", no_http)
    entry = source_entry()
    before = copy.deepcopy(entry)
    variants = propose_variants(entry, path)
    assert [e["format"] for e in variants] == ["epub", "txt", "html"]
    assert [e["download_url"] for e in variants] == [url for url, _ in formats]
    for variant in variants:
        assert all(
            variant[key] == entry[key] for key in ("id", "work_id", "source_url", "edition", "section_attributions")
        )
        assert variant["illustrations_included"] is False
        assert variant["source_variant"]["status"] == "proposed"
        assert variant["source_variant"]["reason"] == "image-heavy artifact too big"
        assert len(variant["source_variant"]["catalog_record_sha256"]) == 64
    assert entry == before


@pytest.mark.parametrize(
    "url",
    [
        "http://www.gutenberg.org/ebooks/63371.epub.noimages",
        "https://evil.invalid/ebooks/63371.epub.noimages",
        "https://www.gutenberg.org.evil.invalid/ebooks/63371.epub.noimages",
        "https://user:password@www.gutenberg.org/ebooks/63371.epub.noimages",
        "https://www.gutenberg.org:444/ebooks/63371.epub.noimages",
        "https://www.gutenberg.org/ebooks/99999.epub.noimages",
        "https://www.gutenberg.org/files/63371/../99999/noimages.epub",
        "https://www.gutenberg.org/files/63371/%252e%252e/99999/noimages.epub",
    ],
)
def test_unapproved_or_different_book_urls_are_not_proposed(tmp_path: Path, url: str) -> None:
    path = tmp_path / "record.rdf"
    path.write_bytes(rdf([(url, "application/epub+zip")]))
    assert propose_variants(source_entry(), path) == []


@pytest.mark.parametrize("rights", ["Copyrighted. Read the copyright notice.", "", "Public domain"])
def test_catalog_rights_are_not_inferred_from_provider_or_author_age(tmp_path: Path, rights: str) -> None:
    path = tmp_path / "record.rdf"
    path.write_bytes(rdf(default_formats(), rights=rights))
    assert propose_variants(source_entry(), path) == []


def test_restrictive_or_missing_existing_rights_are_not_silently_relaxed(tmp_path: Path) -> None:
    path = tmp_path / "record.rdf"
    path.write_bytes(rdf(default_formats()))
    entry = source_entry()
    for rights in ("Copyrighted", ""):
        entry["rights"] = rights
        assert propose_variants(entry, path) == []


def test_existing_noimage_alias_or_cache_file_is_not_retried_as_same_artifact(tmp_path: Path) -> None:
    path = tmp_path / "record.rdf"
    path.write_bytes(rdf(default_formats()))
    entry = source_entry()
    entry["download_url"] = "https://www.gutenberg.org/cache/epub/63371/pg63371.epub"
    variants = propose_variants(entry, path)
    assert [v["format"] for v in variants] == ["txt", "html"]


def test_variants_clear_stale_artifact_paths_and_hashes_but_keep_original_provenance(tmp_path: Path) -> None:
    path = tmp_path / "record.rdf"
    path.write_bytes(rdf(default_formats()))
    entry = {**source_entry(), "local_path": "/old/image-heavy.epub", "artifact_sha256": "old-hash", "status": "failed"}
    variant = propose_variants(entry, path)[0]
    assert "local_path" not in variant and "artifact_sha256" not in variant and "status" not in variant
    assert variant["source_variant"]["original_artifact"]["artifact_sha256"] == "old-hash"


def test_only_explicit_utf8_and_text_formats_are_proposed(tmp_path: Path) -> None:
    path = tmp_path / "record.rdf"
    formats = [
        ("https://www.gutenberg.org/files/63371/old.txt", "text/plain; charset=iso-8859-1"),
        ("https://www.gutenberg.org/ebooks/63371.epub3.images", "application/epub+zip"),
        ("https://www.gutenberg.org/files/63371/scan.pdf", "application/pdf"),
        ("https://www.gutenberg.org/files/63371/pictures.zip", "application/zip"),
    ]
    path.write_bytes(rdf(formats))
    assert propose_variants(source_entry(), path) == []


def test_streamed_archive_parses_target_record_only_and_does_not_extract_paths(tmp_path: Path) -> None:
    path = tmp_path / "catalog.tar.bz2"
    with tarfile.open(path, "w:bz2") as archive:
        for name, data in [
            ("unrelated/pg11.rdf", b"not xml and never parsed"),
            ("../../cache/epub/63371/pg63371.rdf", rdf(default_formats())),
        ]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    variants = propose_variants(source_entry(), path)
    assert len(variants) == 3
    assert variants[0]["source_variant"]["catalog_member"].endswith("pg63371.rdf")
    assert not (tmp_path.parent / "cache").exists()


def test_wrong_record_or_mismatched_number_never_changes_edition(tmp_path: Path) -> None:
    path = tmp_path / "record.rdf"
    path.write_bytes(rdf(default_formats(11), number=11))
    assert propose_variants(source_entry(), path) == []
    entry = source_entry()
    entry["id"] = "pg11-full"
    with pytest.raises(ValueError, match="disagree"):
        propose_variants(entry, path)


def test_duplicate_format_aliases_choose_one_rdf_listed_candidate(tmp_path: Path) -> None:
    path = tmp_path / "record.rdf"
    formats = default_formats() + [
        ("https://www.gutenberg.org/ebooks/63371.html.images", "text/html"),
        ("https://www.gutenberg.org/cache/epub/63371/pg63371.epub", "application/epub+zip"),
    ]
    path.write_bytes(rdf(formats))
    variants = propose_variants(source_entry(), path)
    assert len(variants) == 3
    assert variants[0]["download_url"] == "https://www.gutenberg.org/cache/epub/63371/pg63371.epub"
    assert all(v["download_url"] in {url for url, _ in formats} for v in variants)
