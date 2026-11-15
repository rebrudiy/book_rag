"""The integrated SQL must preserve the previous ordered evidence contract."""

from __future__ import annotations

import re
import runpy
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from book_rag.library import Library

PROTOTYPE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/benchmark_lexical_plan.py"))
Case = PROTOTYPE["Case"]
CASES = [
    Case("global", "ambercommon"),
    Case("baseline", "ambercommon", mode="baseline"),
    Case("scope", "ambercommon", book_id="book-2"),
    Case("scattered", "ambercommon", book_id="scattered", mode="baseline"),
    Case("language", "ambercommon", language="de"),
    Case("scope-language", "ambercommon", book_id="book-2", language="en"),
    Case("scope-wrong-language", "ambercommon", book_id="book-2", language="de"),
    Case("unavailable", "ambercommon", book_id="book-7"),
    Case("missing", "ambercommon", book_id="absent"),
    Case("empty-book-id", "ambercommon", book_id=""),
    Case("phrase", '"ambercommon copper"'),
    Case("unicode", "Пример café"),
    Case("absent-phrase", '"ambercommon absentphrase"'),
    Case("max-limit", "ambercommon", limit=50),
    Case("absent", "absentfixtureterm"),
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_search_matches_previous_raw_and_final_evidence(tmp_path: Path, case: Any) -> None:
    PROTOTYPE["seed"](tmp_path / "library.sqlite3", passages_per_book=40)
    library = Library(tmp_path)
    with library.connect() as connection:
        reference = PROTOTYPE["execute"](connection, case, "control")
    expected = PROTOTYPE["final_results"](case, reference)
    captured: list[dict[str, Any]] = []
    original_result = Library._result

    def result(row: sqlite3.Row) -> dict[str, Any]:
        captured.append(dict(row))
        return original_result(row)

    with patch.object(Library, "_result", side_effect=result):
        actual = library.search(case.query, case.mode, case.book_id, case.language, case.limit)
    assert PROTOTYPE["guard"](case, reference, captured)["all_exact"]
    assert actual["results"] == expected
    warnings = (
        ["Corrupt citation suppressed; rebuild the affected book"]
        if any(not original_result(row)["verified"] for row in reference)
        else []
    )
    Library._ingestion_warnings({"results": expected, "warnings": warnings})
    assert actual["warnings"] == warnings
    assert actual["mode"] == case.mode
    if case.name == "language":
        assert 1001 in [row["id"] for row in captured]
        assert 1001 not in [row["id"] for row in actual["results"]]
        assert "Corrupt citation suppressed; rebuild the affected book" in actual["warnings"]
    if actual["results"]:
        hit = actual["results"][0]
        assert hit["section_author"] == "CC0 editor" and hit["section_translator"] == "CC0 translator"
        assert "CC0 editor" in hit["citation"] and "CC0 translator" in hit["citation"]
        assert hit["text"] and hit["verified"]


def test_envelope_and_selection_share_one_read_snapshot(tmp_path: Path) -> None:
    """An autocommit bounds lookup before the FTS statement would race reindexing."""
    PROTOTYPE["seed"](tmp_path / "library.sqlite3", passages_per_book=12)
    library = Library(tmp_path)
    original_connect = library.connect
    statements: list[str] = []

    @contextmanager
    def traced() -> Generator[sqlite3.Connection]:
        with original_connect() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    with patch.object(library, "connect", traced):
        response = library.search("ambercommon", book_id="scattered")
    assert response["results"] and all(hit["book_id"] == "scattered" for hit in response["results"])
    # Ignore FTS5's own config/docsize reads; only canonical passage reads can
    # separate a rowid-envelope snapshot from candidate selection.
    reads = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
        and re.search(r"\b(?:FROM|JOIN)\s+passages\b", statement, re.IGNORECASE)
    ]
    if len(reads) != 1:
        # A future explicit transaction is also safe; split autocommit reads are not.
        begins = [index for index, statement in enumerate(statements) if statement.upper().startswith("BEGIN")]
        assert begins and begins[0] < statements.index(reads[0]), "Bounds and FTS must share a read snapshot"
