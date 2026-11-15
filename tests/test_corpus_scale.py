"""Large-corpus queues preserve rights, same-ID URLs and exact dedup evidence."""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from book_rag.catalog import _parse_rdf
from book_rag.corpus_scale import _txt, plan_scale_corpus, rejection_reason, work_key


def record(
    number: int,
    title: str,
    *,
    rights: str = "Public domain in the USA.",
    size: int = 60000,
    author: str = "Writer",
    host: str = "www.gutenberg.org",
) -> bytes:
    return f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns:pg="http://www.gutenberg.org/2009/pgterms/" xmlns:dc="http://purl.org/dc/terms/">
      <pg:ebook rdf:about="ebooks/{number}"><dc:title>{title}</dc:title><dc:rights>{rights}</dc:rights>
      <dc:creator><pg:agent><pg:name>{author}</pg:name></pg:agent></dc:creator>
      <dc:language><rdf:Description><rdf:value>en</rdf:value></rdf:Description></dc:language>
      <dc:type><rdf:Description><rdf:value>Text</rdf:value></rdf:Description></dc:type>
      <dc:hasFormat><pg:file rdf:about="https://{host}/files/{number}/{number}-0.txt">
      <dc:extent>{size}</dc:extent><dc:format><rdf:Description><rdf:value>text/plain; charset=utf-8</rdf:value>
      </rdf:Description></dc:format></pg:file></dc:hasFormat>
      <dc:hasFormat><pg:file rdf:about="https://{host}/ebooks/{number}.epub.noimages">
      <dc:extent>20000</dc:extent><dc:format><rdf:Description><rdf:value>application/epub+zip</rdf:value>
      </rdf:Description></dc:format></pg:file></dc:hasFormat></pg:ebook></rdf:RDF>""".encode()


def archive(path: Path, records: list[bytes]) -> None:
    with tarfile.open(path, "w:bz2") as tar:
        for index, data in enumerate(records):
            info = tarfile.TarInfo(f"../../pg{index}.rdf")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def library(path: Path, rows: list[tuple[str, str, str, str, str]] | None = None) -> None:
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE books(id,title,author,work_id,status)")
        c.executemany("INSERT INTO books VALUES (?,?,?,?,?)", rows or [])


def test_official_format_extents_retained_and_no_image_variant_selected(tmp_path: Path) -> None:
    raw = record(10, "A Substantial Novel")
    row = next(_parse_rdf(raw))
    assert row["format_extents"]["https://www.gutenberg.org/files/10/10-0.txt"] == 60000
    source = tmp_path / "pg10.rdf"
    source.write_bytes(raw)
    db = tmp_path / "live.sqlite3"
    library(db)
    output = tmp_path / "queue.json"
    report = plan_scale_corpus(source, db, output, limit=1)
    entry = json.loads(output.read_text())[0]
    assert report["selected"] == 1 and report["eligible_candidate_groups"] == 1
    assert entry["format"] == "epub" and entry["download_url"] == "https://www.gutenberg.org/cache/epub/10/pg10.epub"
    assert entry["illustrations_included"] is False and entry["coverage_scope"] == "text_only_without_illustrations"
    assert entry["work_identity_status"] == "provisional"
    assert len(entry["catalog_record_sha256"]) == 64
    assert entry["resource_forecast"]["estimated_added_bytes"] == 60000 * 6 + 20000 + 65536


@pytest.mark.parametrize(
    "title",
    [
        "A Novel, Volume II",
        "A Novel, Tome 3",
        "A Novel, Band 2",
        "A Novel, Chapter IV",
        "Collected Works of Somebody",
        "An Anthology",
        "Stories of the Sea",
    ],
)
def test_explicit_components_and_collections_not_counted_as_single_work_candidates(title: str) -> None:
    row = next(_parse_rdf(record(10, title)))
    assert rejection_reason(row) in {"explicit_volume_or_component", "explicit_collection"}


def test_full_work_with_book_word_is_not_mistaken_for_volume() -> None:
    assert rejection_reason(next(_parse_rdf(record(10, "The Book of Friendship")))) is None
    assert rejection_reason(next(_parse_rdf(record(10, "An Introduction to Logic")))) is None


@pytest.mark.parametrize("rights", ["", "Public domain", "Copyrighted.", "CC BY4.0"])
def test_no_rights_inferred_from_provider_reputation(rights: str) -> None:
    assert rejection_reason(next(_parse_rdf(record(10, "A Novel", rights=rights)))) == "rights_unverified"


def test_exact_normalization_uses_dates_punctuation_and_coauthor_order() -> None:
    assert work_key("Writer,1870-1940; Coauthor,1860-1930", "A Novel: Alpha!") == work_key(
        "Coauthor; Writer", "A NOVEL Alpha"
    )
    assert work_key("Another Writer", "A Novel Alpha") != work_key("Writer", "A Novel Alpha")


def test_foreign_host_or_other_book_url_never_accepted() -> None:
    row = next(_parse_rdf(record(10, "A Novel", host="not-gutenberg.example")))
    assert _txt(row) is None
    row = next(_parse_rdf(record(10, "A Novel")))
    row["formats"] = [("https://www.gutenberg.org/files/11/11-0.txt", "text/plain; charset=utf-8")]
    assert _txt(row) is None


def test_existing_catalog_identity_seen_later_still_excludes_alias(tmp_path: Path) -> None:
    source = tmp_path / "catalog.tar.bz2"
    archive(source, [record(10, "Shared Original"), record(50, "Shared Original"), record(60, "A Different Novel")])
    db = tmp_path / "live.sqlite3"
    library(db, [("pg50-full", "Old Parser Title", "Someone", "pg50", "indexed")])
    output = tmp_path / "queue.json"
    report = plan_scale_corpus(source, db, output, limit=10)
    assert [e["id"] for e in json.loads(output.read_text())] == ["pg60-full"]
    assert report["records"]["duplicate_existing_author_title"] == 1
    proof = [json.loads(line) for line in output.with_suffix(".dedup.jsonl").read_text().splitlines()]
    assert proof[0]["source_id"] == "pg10-full" and proof[0]["representative"] == "pg50-full"
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1


def test_duplicate_candidate_chooses_smaller_artifact_and_reports_final_representative(tmp_path: Path) -> None:
    source = tmp_path / "catalog.tar.bz2"
    archive(source, [record(40, "One Novel", size=70000), record(41, "ONE NOVEL", size=50000)])
    db = tmp_path / "live.sqlite3"
    library(db)
    output = tmp_path / "queue.json"
    report = plan_scale_corpus(source, db, output, limit=10)
    assert report["selected"] == 1 and json.loads(output.read_text())[0]["id"] == "pg41-full"
    proof = json.loads(output.with_suffix(".dedup.jsonl").read_text())
    assert proof["source_id"] == "pg40-full" and proof["representative"] == "pg41-full"
    again = plan_scale_corpus(source, db, output, limit=10)
    assert again["reused_existing_queue"] and again["queue_sha256"] == report["queue_sha256"]


def test_disk_reserve_blocks_selection_before_any_live_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    source = tmp_path / "pg10.rdf"
    source.write_bytes(record(10, "A Novel"))
    db = tmp_path / "live.sqlite3"
    library(db)
    monkeypatch.setattr(
        "book_rag.corpus_scale.shutil.disk_usage",
        lambda path: shutil._ntuple_diskusage(5 * 1024**3, 1024**3, 4 * 1024**3),
    )
    report = plan_scale_corpus(source, db, tmp_path / "queue.json", limit=1)
    assert report["selected"] == 0 and report["budget_bytes"] == 0
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
