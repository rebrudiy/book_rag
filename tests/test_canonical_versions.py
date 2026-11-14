"""Exact historical context remains readable across parser/index/source changes."""

from __future__ import annotations

import errno
import gzip
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from book_rag import canonical_versions as versions
from book_rag.canonical_versions import (
    CanonicalIntegrityError,
    preserve_canonical,
    read_canonical_manifest,
    read_canonical_section,
)
from book_rag.parsers import ParsedBook, Section

ARTIFACT = "a" * 64
PARSER = "b" * 64
INDEXER = "c" * 64


def book(text: str = "Exact original words.\n\nA second paragraph.") -> ParsedBook:
    return ParsedBook("Historical book", "CC0 fixture author", "en", [Section("Chapter one", text)])


def keep(data: Path, parsed: ParsedBook | None = None, **kwargs: Any) -> versions.PreservedCanonical:
    return preserve_canonical(parsed or book(), data, artifact_sha256=ARTIFACT, **kwargs)


def files(data: Path) -> dict[str, bytes]:
    return {str(path.relative_to(data)): path.read_bytes() for path in data.rglob("*") if path.is_file()}


def assert_no_temporaries(data: Path) -> None:
    assert not list(data.rglob(".canonical-*"))


def section_path(data: Path, digest: str) -> Path:
    return data / "canonical" / "sections" / digest[:2] / f"{digest}.json.gz"


def forge_manifest(data: Path, source: Path, mutate: Any) -> str:
    payload = json.loads(source.read_bytes())
    mutate(payload)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    destination = data / "canonical" / "versions" / digest[:2] / f"{digest}.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_bytes(raw)
    return "cv1-" + digest


def test_exact_non_nfc_unicode_utf8_newlines_and_provenance(tmp_path: Path) -> None:
    exact = "Cafe\u0301\r\n\tЯ 🌍\n\n  indented code\t= 1\r\n"
    parsed = ParsedBook("Cafe\u0301", "Автор", "ru", [Section("Не NFC: e\u0301", exact)])
    result = keep(
        tmp_path,
        parsed,
        parser_fingerprint=PARSER.upper(),
        indexer_fingerprint=INDEXER,
        title="Exact override title",
        edition="Historical edition 1900",
        source_url="https://example.org/edition-one",
        rights="CC0 original test",
        rights_url="https://creativecommons.org/publicdomain/zero/1.0/",
        section_metadata=[
            {
                "section_author": "Редактор e\u0301",
                "section_role": "editorial",
                "section_translator": "Переводчик",
                "attribution_basis": "explicit owned fixture",
            }
        ],
    )
    manifest = read_canonical_manifest(tmp_path, result["version_id"])
    chapter = read_canonical_section(tmp_path, result["version_id"], 0)
    assert chapter["section"]["text"] == exact and "e\u0301" in chapter["section"]["title"]
    assert chapter["section"]["section_author"] == "Редактор e\u0301"
    assert chapter["section"]["section_translator"] == "Переводчик"
    assert chapter["section"]["section_role"] == "editorial"
    assert chapter["book"]["title"] == "Exact override title"
    assert chapter["book"]["edition"] == "Historical edition 1900"
    assert chapter["book"]["source_url"] == "https://example.org/edition-one"
    assert chapter["artifact_sha256"] == ARTIFACT
    assert chapter["parser_fingerprint"] == PARSER and chapter["indexer_fingerprint"] == INDEXER
    source = section_path(tmp_path, manifest["sections"][0]["sha256"])
    compressed = source.read_bytes()
    raw = gzip.decompress(compressed)
    assert hashlib.sha256(raw).hexdigest() == manifest["sections"][0]["sha256"]
    assert json.loads(raw)["text"] == exact
    assert compressed[4:8] == b"\0\0\0\0" and compressed[9] == 255
    assert result["canonical_bytes"] == len(raw) + result["path"].stat().st_size
    assert_no_temporaries(tmp_path)


def test_deterministic_identity_and_repeated_sections_share_one_compressed_object(tmp_path: Path) -> None:
    parsed = book()
    parsed.sections.append(Section(parsed.sections[0].title, parsed.sections[0].text))
    first = keep(tmp_path, parsed)
    before = files(tmp_path)
    second = keep(tmp_path, parsed)
    assert first == second and files(tmp_path) == before
    manifest = read_canonical_manifest(tmp_path, first["version_id"])
    assert manifest["sections"][0]["sha256"] == manifest["sections"][1]["sha256"]
    assert len(list(tmp_path.rglob("*.json.gz"))) == 1
    assert first["stored_bytes"] == sum(len(payload) for payload in before.values())
    assert read_canonical_section(tmp_path, first["version_id"], 1)["section"]["ordinal"] == 1


def test_source_parser_indexer_metadata_or_text_changes_preserve_earlier_version(tmp_path: Path) -> None:
    first = keep(tmp_path)
    candidates = [
        keep(tmp_path, parser_fingerprint=PARSER),
        keep(tmp_path, indexer_fingerprint=INDEXER),
        preserve_canonical(book(), tmp_path, artifact_sha256="d" * 64),
        keep(tmp_path, edition="A different edition"),
        keep(tmp_path, section_metadata={0: {"section_role": "editorial", "section_author": "Verified editor"}}),
        keep(tmp_path, book("A differently normalized replacement.\n")),
    ]
    assert len({first["version_id"], *(item["version_id"] for item in candidates)}) == 7
    assert read_canonical_section(tmp_path, first["version_id"], 0)["section"]["text"] == book().sections[0].text
    unknown = read_canonical_manifest(tmp_path, first["version_id"])
    assert unknown["parser_fingerprint"] is None and unknown["indexer_fingerprint"] is None
    assert "section_author" not in read_canonical_section(tmp_path, first["version_id"], 0)["section"]
    assert len(list(tmp_path.rglob("*.json.gz"))) == 2


@pytest.mark.parametrize("digest", ["", "a" * 63, "g" * 64, "../" + "a" * 64, None, True])
def test_invalid_source_or_parser_sha_rejected_before_storage(tmp_path: Path, digest: Any) -> None:
    with pytest.raises(ValueError):
        preserve_canonical(book(), tmp_path, artifact_sha256=digest)
    if digest is not None:
        with pytest.raises(ValueError):
            keep(tmp_path, parser_fingerprint=digest)
    assert not (tmp_path / "canonical").exists()


@pytest.mark.parametrize("version_id", ["", "../anything", "cv1-" + "A" * 64, "cv2-" + "a" * 64, None])
def test_version_ids_cannot_select_arbitrary_paths(tmp_path: Path, version_id: Any) -> None:
    with pytest.raises(ValueError):
        read_canonical_manifest(tmp_path, version_id)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("ordinal", [True, "0", -1, 1])
def test_section_ordinal_is_strict_and_bounded(tmp_path: Path, ordinal: Any) -> None:
    result = keep(tmp_path)
    with pytest.raises((ValueError, IndexError)):
        read_canonical_section(tmp_path, result["version_id"], ordinal)


@pytest.mark.parametrize(
    "metadata",
    [
        [{"note": "Never store private notes here"}],
        [{"section_author": None}],
        [{"section_role": "guessed-author"}],
        [{"section_role": True}],
        [],
        {True: {"section_role": "editorial"}},
        {1: {"section_role": "editorial"}},
    ],
)
def test_contributor_metadata_whitelist_and_alignment(tmp_path: Path, metadata: Any) -> None:
    with pytest.raises(ValueError):
        keep(tmp_path, section_metadata=metadata)
    assert not (tmp_path / "canonical").exists()


def test_surrogates_nonstrings_empty_books_and_byte_limits_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid_text: Any = 12
    for parsed in (book("bad\ud800"), ParsedBook("T", "A", "en", []), book(invalid_text)):
        with pytest.raises(ValueError):
            keep(tmp_path, parsed)
    monkeypatch.setattr(versions, "MAX_SECTION_BYTES", 128)
    with pytest.raises(ValueError, match="limit"):
        keep(tmp_path, book("x" * 129))
    monkeypatch.setattr(versions, "MAX_CANONICAL_BYTES", 200)
    with pytest.raises(ValueError, match="limit"):
        keep(tmp_path, book("small valid section"))
    if (tmp_path / "canonical").exists():
        assert not list((tmp_path / "canonical" / "versions").glob("*/*.json"))
    assert_no_temporaries(tmp_path)


@pytest.mark.parametrize("corruption", ["bytes", "truncated", "bomb", "wrong_digest"])
def test_corrupt_existing_sections_rejected_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    result = keep(tmp_path, book("Small exact text."))
    manifest = read_canonical_manifest(tmp_path, result["version_id"])
    path = section_path(tmp_path, manifest["sections"][0]["sha256"])
    corrupted = b"not gzip" if corruption == "bytes" else path.read_bytes()[:-5]
    if corruption == "bomb":
        monkeypatch.setattr(versions, "MAX_SECTION_BYTES", 128)
        corrupted = gzip.compress(b"x" * 129, mtime=0)
    elif corruption == "wrong_digest":
        corrupted = gzip.compress(gzip.decompress(path.read_bytes()).replace(b"Small exact", b"Wrong exact"), mtime=0)
    path.write_bytes(corrupted)
    with pytest.raises(CanonicalIntegrityError):
        read_canonical_section(tmp_path, result["version_id"], 0)
    with pytest.raises(CanonicalIntegrityError):
        keep(tmp_path, book("Small exact text."))
    assert path.read_bytes() == corrupted
    assert_no_temporaries(tmp_path)


def test_corrupt_manifest_and_missing_section_fail_explicitly(tmp_path: Path) -> None:
    result = keep(tmp_path)
    manifest = read_canonical_manifest(tmp_path, result["version_id"])
    source = section_path(tmp_path, manifest["sections"][0]["sha256"])
    source.unlink()
    with pytest.raises(FileNotFoundError):
        read_canonical_section(tmp_path, result["version_id"], 0)
    result["path"].write_bytes(b'{"corrupt":true}')
    before = result["path"].read_bytes()
    with pytest.raises(CanonicalIntegrityError):
        read_canonical_manifest(tmp_path, result["version_id"])
    with pytest.raises(CanonicalIntegrityError):
        keep(tmp_path)
    assert result["path"].read_bytes() == before


@pytest.mark.parametrize("mutation", ["title", "characters", "schema", "private_note"])
def test_valid_hash_does_not_bypass_manifest_shape_and_section_checks(tmp_path: Path, mutation: str) -> None:
    result = keep(tmp_path)

    def mutate(payload: dict[str, Any]) -> None:
        if mutation == "title":
            payload["sections"][0]["title"] = "Wrong matching context"
        elif mutation == "characters":
            payload["sections"][0]["characters"] += 1
        elif mutation == "schema":
            payload["schema_version"] = True
        else:
            payload["note"] = "No annotations belong inside canonical context"

    forged = forge_manifest(tmp_path, result["path"], mutate)
    with pytest.raises(CanonicalIntegrityError):
        read_canonical_section(tmp_path, forged, 0)


def test_reader_decompresses_only_requested_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = book()
    parsed.sections.extend(Section(f"Chapter {index}", f"Unique historical context {index}") for index in range(3))
    result = keep(tmp_path, parsed)
    original = versions._section
    read = []

    def observe(path: Path, digest: str) -> tuple[dict[str, Any], int]:
        read.append(digest)
        return original(path, digest)

    monkeypatch.setattr(versions, "_section", observe)
    manifest = read_canonical_manifest(tmp_path, result["version_id"])
    assert read == []
    result_section = read_canonical_section(tmp_path, result["version_id"], 2)
    assert result_section["section"]["text"] == parsed.sections[2].text
    assert read == [manifest["sections"][2]["sha256"]]


def test_concurrent_writers_publish_one_complete_version(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: keep(tmp_path, indexer_fingerprint=INDEXER), range(24)))
    assert len({item["version_id"] for item in results}) == 1
    assert len(list(tmp_path.rglob("*.json.gz"))) == len(list(tmp_path.rglob("*.json"))) == 1
    assert read_canonical_section(tmp_path, results[0]["version_id"], 0)["section"]["text"] == book().sections[0].text
    assert_no_temporaries(tmp_path)


def test_failed_manifest_publication_cleans_owned_temps_and_retains_reusable_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = os.link

    def fail_manifest(source: Path, destination: Path, **kwargs: Any) -> None:
        if destination.suffix == ".json":
            raise OSError(errno.EIO, "Owned publication failure")
        link(source, destination, **kwargs)

    monkeypatch.setattr(versions.os, "link", fail_manifest)
    with pytest.raises(OSError, match="Owned publication failure"):
        keep(tmp_path)
    assert not list(tmp_path.rglob("*.json")) and len(list(tmp_path.rglob("*.json.gz"))) == 1
    assert_no_temporaries(tmp_path)
    monkeypatch.setattr(versions.os, "link", link)
    recovered = keep(tmp_path)
    assert read_canonical_manifest(tmp_path, recovered["version_id"])["section_count"] == 1


def test_storage_aliases_are_rejected_without_writing_external_directory(tmp_path: Path) -> None:
    data = tmp_path / "data"
    external = tmp_path / "owned-external"
    data.mkdir()
    external.mkdir()
    (data / "canonical").symlink_to(external, target_is_directory=True)
    with pytest.raises(CanonicalIntegrityError, match="symlinks"):
        keep(data)
    assert not list(external.iterdir())


def test_new_corrupt_publication_is_removed_and_does_not_expose_a_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = os.link

    def corrupt_published(source: Path, destination: Path, **kwargs: Any) -> None:
        link(source, destination, **kwargs)
        destination.write_bytes(b"Corrupted newly published owned inode")

    monkeypatch.setattr(versions.os, "link", corrupt_published)
    with pytest.raises(CanonicalIntegrityError):
        keep(tmp_path)
    assert not files(tmp_path)
    assert_no_temporaries(tmp_path)


def test_interrupted_file_flush_cleans_temporary_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(descriptor: int) -> None:
        raise KeyboardInterrupt("Owned flush interrupted")

    monkeypatch.setattr(versions.os, "fsync", interrupt)
    with pytest.raises(KeyboardInterrupt, match="Owned flush interrupted"):
        keep(tmp_path)
    assert not files(tmp_path)
    assert_no_temporaries(tmp_path)
