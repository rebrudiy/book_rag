"""Canonical-source overlap and conservative proposals; tiny owned SQLite only."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from book_rag import content_overlap as co


def prose(number: int, group: str = "shared") -> str:
    return f"Paragraph {number} of {group} describes the ordinary journey of travellers through a very quiet valley."


def make_db(path: Path, books: dict[str, tuple[str, str]]) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE books(id,title,author,language,work_id,source_url,"
            "artifact_sha256,content_sha256,rights,status)"
        )
        db.execute("CREATE TABLE sections(book_id,ordinal,title,text,UNIQUE(book_id,ordinal))")
        for bid, (text, language) in books.items():
            db.execute(
                "INSERT INTO books VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    bid,
                    bid,
                    "Author",
                    language,
                    bid,
                    "https://www.gutenberg.org/ebooks/11",
                    "a" * 64,
                    hashlib.sha256(text.encode()).hexdigest(),
                    "Public domain in the USA.",
                    "indexed",
                ),
            )
            db.execute("INSERT INTO sections VALUES(?,?,?,?)", (bid, 0, "Original chapter", text))


def test_unicode_normalization_and_source_offsets_do_not_rewrite_code() -> None:
    original = '  Первый e\u0301лемент. \n\n\tcode = "a  b"\n\tprint(code)\n\nКонец'
    offsets = co.paragraphs(original)
    assert [original[start:end] for start, end in offsets] == [
        "Первый e\u0301лемент.",
        'code = "a  b"\n\tprint(code)',
        "Конец",
    ]
    assert co.normalized("E\u0301  É\n\tЕЩЁ") == "é é ещё"
    assert original[offsets[1][0] : offsets[1][1]] == 'code = "a  b"\n\tprint(code)'
    assert co.generated_credit("this ebook is for the use of anyone anywhere project gutenberg")
    assert not co.generated_credit("my preface discusses project gutenberg and its history")


def test_bottom_sample_is_deterministic_bounded_and_ignores_repeated_paragraphs() -> None:
    values = [prose(n) for n in range(100)]
    sample = co.bottom_sample(iter(values), 16)
    assert len(sample) == 16 and sample == co.bottom_sample(iter(reversed(values * 2)), 16)
    assert all(len(digest) == 8 for digest in sample)


def test_full_overlap_and_containment_are_distinct_review_proposals(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    paragraphs = [prose(n) for n in range(24)]
    text = "\n\n".join(paragraphs)
    make_db(
        source,
        {
            "a": (text, "en"),
            "b": ("\n\n".join(p.upper().replace(" ", "  ") for p in paragraphs), "en"),
            "c": (text + "\n\n" + "\n\n".join(prose(n, "other-collection") for n in range(100)), "en"),
            "d": ("\n\n".join(prose(n, "unrelated") for n in range(24)), "en"),
        },
    )
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    report = co.audit_near_duplicates(source, tmp_path / "audit")
    pairs = {(p["left"]["id"], p["right"]["id"]): p for p in report["pairs"]}
    assert pairs["a", "b"]["relationship_proposal"] == "high_overlap_edition_candidate"
    assert pairs["a", "b"]["left_character_overlap"] == pairs["a", "b"]["right_character_overlap"] == 1
    assert pairs["a", "c"]["relationship_proposal"] == "possible_containment_or_excerpt"
    assert pairs["a", "c"]["left_character_overlap"] == 1 and pairs["a", "c"]["right_character_overlap"] < 0.5
    assert not any("d" in pair for pair in pairs)
    for pair in pairs.values():
        assert pair["identity_verified"] is False and pair["manual_review"] == "pending"
        assert all(p["normalized_literal_equal"] for p in pair["source_examples"])
        for proof in pair["source_examples"]:
            for evidence in proof["sources"]:
                with sqlite3.connect(source) as db:
                    text = db.execute("SELECT text FROM sections WHERE book_id=?", (evidence["book_id"],)).fetchone()[0]
                assert (
                    hashlib.sha256(text[evidence["start"] : evidence["end"]].encode()).hexdigest()
                    == evidence["raw_sha256"]
                )
    assert report["source_guards_still_current"] and not report["database_modified"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert report["peak_memory_bytes"] < co.MAX_MEMORY and report["scratch_bytes"] < co.MAX_OUTPUT
    with pytest.raises(ValueError, match="new audit output"):
        co.audit_near_duplicates(source, tmp_path / "audit")


def test_cross_language_overlap_is_not_called_a_translation(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    text = "\n\n".join(prose(n) for n in range(12))
    make_db(source, {"a": (text, "en"), "b": (text, "fr")})
    report = co.audit_near_duplicates(source, tmp_path / "audit")
    assert report["pairs"][0]["relationship_proposal"] == "cross_language_shared_text_not_translation_proof"
    assert not report["identity_count_promotion_allowed"]


def test_common_sampled_boilerplate_is_capped_and_candidate_saturation_is_explicit(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    text = "\n\n".join(prose(n) for n in range(12))
    make_db(source, dict.fromkeys("abcd", (text, "en")))
    common = co.audit_near_duplicates(source, tmp_path / "common", max_document_frequency=2)
    assert not common["pairs"] and common["counters"]["common_sampled_hashes_excluded"] == 12
    capped = co.audit_near_duplicates(source, tmp_path / "cap", candidate_cap=1)
    assert capped["candidate_generation_truncated"] and len(capped["pairs"]) == 1


def test_unknown_rights_and_arbitrary_github_sources_are_not_read(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    text = "\n\n".join(prose(n) for n in range(12))
    make_db(source, {"a": (text, "en"), "b": (text, "en")})
    with sqlite3.connect(source) as db:
        db.execute('UPDATE books SET rights="unknown" WHERE id="a"')
        db.execute('UPDATE books SET source_url="https://github.com/personal/private" WHERE id="b"')
    report = co.audit_near_duplicates(source, tmp_path / "audit")
    assert report["public_books_audited"] == 0 and report["outside_public_source_scope_skipped"] == 2
    assert not report["counters"] and not report["pairs"]


def test_memory_guard_stops_without_touching_canonical_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.sqlite3"
    make_db(source, {"a": ("\n\n".join(prose(n) for n in range(12)), "en")})
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(co, "peak_memory", lambda: co.MAX_MEMORY + 1)
    with pytest.raises(ValueError, match="peak-memory bound"):
        co.audit_near_duplicates(source, tmp_path / "audit")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
