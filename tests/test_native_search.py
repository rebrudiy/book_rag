"""Native lexical rank preserves CTE evidence/order and a single read snapshot."""

from __future__ import annotations

import runpy
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from book_rag import library as library_module
from book_rag.library import Library

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
PROTOTYPE = runpy.run_path(str(ROOT / "scripts/benchmark_lexical_plan.py"))
Case = PROTOTYPE["Case"]
Candidate = Library


class Control(Library):
    """Pre-integration CTE gold plan, with identical strict source eligibility."""

    @staticmethod
    def _lexical_rows(conn, expression, book_id, language, limit):
        case = Case("gold", "unused", book_id=book_id, language=language, mode="baseline", limit=limit)
        sql, parameters = PROTOTYPE["statement"](conn, case, "materialized_envelope")
        sql = sql.replace(
            "JOIN sections s ON s.id=p.section_id ", "JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
        )
        return conn.execute(sql, (expression, *parameters[1:])).fetchall()


def capture(library: Any, case: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    original = type(library)._result

    def observe(row: sqlite3.Row) -> dict[str, Any]:
        rows.append(dict(row))
        return original(row)

    with patch.object(type(library), "_result", new=staticmethod(observe)):
        response = library.search(case.query, case.mode, case.book_id, case.language, case.limit)
    return response, rows


@pytest.mark.parametrize("mode", ["lexical", "baseline"])
@pytest.mark.parametrize("scope", ["global", "book", "language", "missing", "wrong-language", "scattered"])
def test_ids_bm25_metadata_citations_and_prefixes(tmp_path: Path, mode: str, scope: str) -> None:
    PROTOTYPE["seed"](tmp_path / "library.sqlite3", passages_per_book=40)
    control, candidate = Control(tmp_path), Candidate(tmp_path)
    filters = {
        "global": {},
        "book": {"book_id": "book-2"},
        "language": {"language": "de"},
        "missing": {"book_id": "absent"},
        "wrong-language": {"book_id": "book-2", "language": "de"},
        "scattered": {"book_id": "scattered"},
    }[scope]
    prior: list[int] = []
    for limit in (5, 20, 50):
        case = Case(scope, "ambercommon", mode=mode, limit=limit, **filters)
        expected, raw_before = capture(control, case)
        actual, raw_after = capture(candidate, case)
        assert PROTOTYPE["guard"](case, raw_before, raw_after)["all_exact"]
        assert {key: value for key, value in expected.items() if key != "elapsed_ms"} == {
            key: value for key, value in actual.items() if key != "elapsed_ms"
        }
        ids = [row["id"] for row in actual["results"]]
        assert ids[: len(prior)] == prior
        prior = ids


@pytest.mark.parametrize(
    "query", ['"ambercommon copper"', '"Пример 🦉 café"', '"absent phrase"', "the and", "cobaltneedle"]
)
def test_phrase_unicode_empty_and_rare_query_semantics(tmp_path: Path, query: str) -> None:
    PROTOTYPE["seed"](tmp_path / "library.sqlite3", passages_per_book=12)
    control, candidate = Control(tmp_path), Candidate(tmp_path)
    case = Case("query-kind", query)
    expected, raw_before = capture(control, case)
    actual, raw_after = capture(candidate, case)
    assert PROTOTYPE["guard"](case, raw_before, raw_after)["all_exact"]
    assert actual["results"] == expected["results"] and actual["warnings"] == expected["warnings"]


def test_reversed_cutoff_ties_and_bounded_heap(tmp_path: Path) -> None:
    PROTOTYPE["seed"](tmp_path / "library.sqlite3", passages_per_book=40)
    control, candidate = Control(tmp_path), Candidate(tmp_path)
    expected = control.search("ambercommon", mode="baseline", limit=5)
    connect = candidate.connect
    maximum = []
    original_push, original_replace = library_module.heapq.heappush, library_module.heapq.heapreplace

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
            self.rows = sorted(cursor.fetchall(), key=lambda row: (row["score"], -row["id"]))

        def __iter__(self):
            return iter(self.rows)

        def close(self):
            self.cursor.close()

    class Proxy:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            cursor = self.connection.execute(sql, parameters)
            return Cursor(cursor) if sql.startswith("SELECT passage_fts.rowid id,rank score") else cursor

    @contextmanager
    def wrapped():
        with connect() as connection:
            yield Proxy(connection)

    def push(heap, entry):
        original_push(heap, entry)
        maximum.append(len(heap))

    def replace(heap, entry):
        original_replace(heap, entry)
        maximum.append(len(heap))

    with (
        patch.object(candidate, "connect", wrapped),
        patch.object(library_module.heapq, "heappush", push),
        patch.object(library_module.heapq, "heapreplace", replace),
    ):
        actual = candidate.search("ambercommon", mode="baseline", limit=5)
    assert actual["results"] == expected["results"] and maximum and max(maximum) == 5


def test_snapshot_survives_reindex_after_rank_cursor_closed(tmp_path: Path) -> None:
    PROTOTYPE["seed"](tmp_path / "library.sqlite3", passages_per_book=12)
    writer, reader = Library(tmp_path), Candidate(tmp_path)
    expected = writer.search("ambercommon", book_id="scattered")["results"]
    connect = reader.connect
    triggered = []
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("New original CC0 replacement with a bronze bookmark.")

    class Proxy:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            if sql.startswith("WITH selected(id,score)"):
                assert self.connection.in_transaction
                assert not triggered
                writer.import_book(
                    replacement, {"id": "scattered", "title": "Replacement CC0", "author": "CC0 fixture"}
                )
                triggered.append(True)
            return self.connection.execute(sql, parameters)

    @contextmanager
    def wrapped() -> Generator[Any]:
        with connect() as connection:
            yield Proxy(connection)

    with patch.object(reader, "connect", wrapped):
        actual = reader.search("ambercommon", book_id="scattered")["results"]
    assert triggered and actual == expected
    assert reader.search("ambercommon", book_id="scattered")["results"] == []


def test_existing_caller_transaction_is_preserved(tmp_path: Path) -> None:
    PROTOTYPE["seed"](tmp_path / "library.sqlite3", passages_per_book=12)
    candidate = Candidate(tmp_path)
    with candidate.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE books SET title='Uncommitted caller title' WHERE id='book-2'")
        rows = candidate._lexical_rows(connection, '"ambercommon"', "book-2", None, 5)
        assert rows and all(row["title"] == "Uncommitted caller title" for row in rows)
        assert connection.in_transaction
        connection.rollback()


@pytest.mark.parametrize("scores", [[float("nan")], [float("inf")], [-1.0, -2.0]])
def test_invalid_rank_closes_cursor_and_owned_read_transaction(tmp_path: Path, scores: list[float]) -> None:
    PROTOTYPE["seed"](tmp_path / "library.sqlite3", passages_per_book=12)
    candidate = Candidate(tmp_path)
    closed = []

    class Cursor:
        def __iter__(self):
            return iter([{"id": index + 1, "score": score} for index, score in enumerate(scores)])

        def close(self):
            closed.append(True)

    class Proxy:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            if sql.startswith("SELECT passage_fts.rowid id,rank score"):
                return Cursor()
            return self.connection.execute(sql, parameters)

    with candidate.connect() as connection:
        with pytest.raises(ValueError, match="Invalid lexical rank"):
            candidate._lexical_rows(cast(sqlite3.Connection, Proxy(connection)), '"ambercommon"', None, None, 5)
        assert closed and not connection.in_transaction


@pytest.mark.parametrize("mode", ["baseline", "lexical"])
def test_ownership_corruption_cannot_poison_candidate_limit(tmp_path: Path, mode: str) -> None:
    root = runpy.run_path(str(ROOT / "tests/test_passage_ownership.py"))
    function = root["test_misowned_high_rank_hits_cannot_consume_pool_before_limit"]
    with patch.dict(function.__globals__, {"Library": Candidate}):
        function(tmp_path, mode)
