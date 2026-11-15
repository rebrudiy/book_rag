"""Native rank correctness is tested only on owned CC0 sources/iterators."""

from __future__ import annotations

import runpy
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

NATIVE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/benchmark_native_rank.py"))
PROTOTYPE = NATIVE["PROTOTYPE"]


def test_boundary_ties_all_scanned_but_only_n_keys_retained() -> None:
    def source():
        # Deliberately reverse equal-score IDs: relying on native tie ordering
        # or truncating at N would return entirely different source IDs.
        yield from ({"id": identity, "score": -1.0} for identity in range(10000, 0, -1))
        yield {"id": 10001, "score": 0.0}
        pytest.fail("Stop once native score is strictly worse than the boundary")

    selected, statistics = NATIVE["exact_pool"](source(), 5)
    assert selected == [(identity, -1.0) for identity in range(1, 6)]
    assert statistics["maximum_heap_entries"] == 5
    assert statistics["eligible_rank_rows_seen"] == 10001
    assert statistics["boundary_tie_rows_after_pool_filled"] == 9995
    assert statistics["stopped_on_strictly_worse_score"]


@pytest.mark.parametrize("scores", [[float("nan")], [-1.0, -2.0], [float("inf")]])
def test_invalid_native_score_stream_fails_closed(scores: list[float]) -> None:
    with pytest.raises(ValueError, match="nonfinite or not monotonically"):
        NATIVE["exact_pool"](({"id": identity + 1, "score": score} for identity, score in enumerate(scores)), 5)


def test_real_sql_native_matches_current_core_on_orphans_misownership_and_filters(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    PROTOTYPE["seed"](database, passages_per_book=40)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE passages SET book_id='book-2' WHERE id=1002")
    case = PROTOTYPE["Case"]
    reports = NATIVE["compare"](
        database,
        [
            case("global", "ambercommon"),
            case("baseline-ties", "ambercommon", mode="baseline"),
            case("scoped", "ambercommon", book_id="book-2"),
            case("scattered", "ambercommon", book_id="scattered"),
            case("unavailable", "ambercommon", book_id="book-7"),
            case("language", "ambercommon", language="de"),
            case("wrong-language", "ambercommon", book_id="book-2", language="de"),
            case("unicode-phrase", '"Пример 🦉 café"'),
            case("missing", "absentfixtureterm"),
        ],
    )
    for report in reports:
        assert report["guard"]["all_exact"]
        assert report["stream"]["maximum_heap_entries"] <= 200
        assert not {1002, 9001, 9002, 9003}.intersection(report["guard"]["ordered_ids"])


def test_explicit_transaction_survives_writer_between_closed_stream_and_hydration(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    PROTOTYPE["seed"](database, passages_per_book=12)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    case = PROTOTYPE["Case"]("snapshot", "ambercommon", book_id="book-0")
    real_hydrate = NATIVE["hydrate"]
    seen = []

    def write_then_hydrate(reader, selected_case, selected):
        assert reader.in_transaction
        with sqlite3.connect(database) as writer:
            writer.execute("UPDATE books SET author='Changed by separate writer' WHERE id='book-0'")
        seen.append(True)
        return real_hydrate(reader, selected_case, selected)

    try:
        with patch.dict(NATIVE["native"].__globals__, {"hydrate": write_then_hydrate}):
            rows, _ = NATIVE["native"](connection, case)
        assert seen and rows
        assert all(row["author"] == "Book Atlas fixture" for row in rows)
        assert not connection.in_transaction
        assert (
            connection.execute("SELECT author FROM books WHERE id='book-0'").fetchone()[0]
            == "Changed by separate writer"
        )
    finally:
        connection.close()
