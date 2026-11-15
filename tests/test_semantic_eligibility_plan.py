"""Exact scope eligibility and bounded metadata plans, without models or text reads."""

from __future__ import annotations

import sqlite3

import pytest

from book_rag.semantic import _scoped_passage_query

OLD_SQL = (
    "SELECT p.id FROM passages p JOIN books b ON b.id=p.book_id "
    "JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
    "WHERE (? IS NULL OR b.id=?) AND (? IS NULL OR b.language=?) AND b.status='indexed'"
)


def fixture() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE books(id TEXT PRIMARY KEY,language TEXT,status TEXT);"
        "CREATE TABLE passages(id INTEGER PRIMARY KEY,book_id TEXT,section_id INTEGER);"
        "CREATE INDEX passages_book ON passages(book_id);"
        "CREATE TABLE sections(id INTEGER PRIMARY KEY,book_id TEXT);"
        "CREATE TABLE source_pipeline(book_id TEXT PRIMARY KEY,stage TEXT);"
    )
    connection.executemany(
        "INSERT INTO books VALUES(?,?,?)",
        [
            ("amber", "en", "indexed"),
            ("bronze", "de", "indexed"),
            ("книга", "ru", "indexed"),
            ("failed", "en", "failed"),
        ],
    )
    connection.executemany(
        "INSERT INTO sections VALUES(?,?)", [(1, "amber"), (2, "bronze"), (3, "книга"), (4, "failed")]
    )
    connection.executemany(
        "INSERT INTO passages VALUES(?,?,?)",
        [
            (1, "amber", 1),
            (2, "bronze", 2),
            (3, "amber", 1),
            (4, "книга", 3),
            (5, "failed", 4),
            (6, "amber", 2),
            (7, "amber", 999),
            (8, "missing", 1),
        ],
    )
    connection.execute("INSERT INTO source_pipeline VALUES('amber','failed')")
    return connection


@pytest.mark.parametrize(
    ("book", "language", "expected"),
    [
        (None, None, [1, 2, 3, 4]),
        ("amber", None, [1, 3]),
        (None, "en", [1, 3]),
        (None, "de", [2]),
        ("bronze", "de", [2]),
        ("bronze", "en", []),
        ("книга", "ru", [4]),
        ("failed", None, []),
        ("missing", None, []),
        ("amber' OR 1=1 --", None, []),
        ("", None, []),
    ],
)
def test_filters_match_old_membership_with_owner_status_and_unicode(
    book: str | None, language: str | None, expected: list[int]
) -> None:
    connection = fixture()
    try:
        sql, parameters = _scoped_passage_query(book, language)
        actual = [row[0] for row in connection.execute(sql, parameters)]
        control = sorted(row[0] for row in connection.execute(OLD_SQL, (book, book, language, language)))
        assert actual == control == expected
        assert "IS NULL OR" not in sql
        assert len(parameters) == int(book is not None) + int(language is not None)
    finally:
        connection.close()


@pytest.mark.parametrize("book,language", [("amber", None), ("amber", "en"), (None, "de")])
def test_plan_seeks_book_passages_instead_of_scanning_passage_payloads(book: str | None, language: str | None) -> None:
    connection = fixture()
    try:
        sql, parameters = _scoped_passage_query(book, language)
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)]
        assert any("SEARCH p USING INDEX passages_book (book_id=?)" in step for step in plan), plan
        assert not any(step.startswith("SCAN p") for step in plan), plan
        if book is not None:
            assert any("SEARCH b" in step and "(id=?)" in step for step in plan), plan
        else:
            assert any(step == "SCAN b" for step in plan), plan
    finally:
        connection.close()
