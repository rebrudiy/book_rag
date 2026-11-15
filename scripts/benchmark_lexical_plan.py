"""Compare lexical SQL plans without changing Library or a live corpus.

Always creates an owned synthetic CC0 fixture. --frozen20 optionally reads a
checkpointed twenty-edition source with immutable/query-only SQLite, never
Library(source). No models, downloads, live 4k corpus or corpus migrations.
Same-query ordered IDs, bit-exact BM25, hydrated fields and final citations must
match before a timing is accepted. Query plans and quote-evaluation counts are
diagnostics; single-process warm timings are not a throughput benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sqlite3
import statistics
import tempfile
import time
import unicodedata
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from book_rag.library import SCHEMA, Library
from book_rag.retrieval import prepare_query, rerank_results

BM25 = "bm25(passage_fts,1.0,2.0,1.5,1.2)"
QUOTE = "substr(s.text,p.start+1,p.end-p.start)"
ELIGIBILITY = "b.status='indexed' AND (? IS NULL OR b.id=?) AND (? IS NULL OR b.language=?)"
METADATA_JOINS = (
    "JOIN passages p ON p.id=passage_fts.rowid JOIN books b ON b.id=p.book_id JOIN sections s ON s.id=p.section_id "
)
VARIANTS = ("control", "materialized", "materialized_envelope", "pure_fts_eligible")


@dataclass(frozen=True)
class Case:
    name: str
    query: str
    book_id: str | None = None
    language: str | None = None
    mode: str = "lexical"
    limit: int = 5

    @property
    def expression(self) -> str:
        if self.mode == "lexical":
            return prepare_query(self.query).expression
        tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", self.query), re.UNICODE)[:40]
        return " OR ".join('"' + token + '"' for token in tokens)

    @property
    def candidate_limit(self) -> int:
        return max(200, self.limit) if self.mode == "lexical" else self.limit


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def state(path: Path) -> tuple[int, int, int, int, int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular nonsymlink database")
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def readonly(database: Path) -> Generator[sqlite3.Connection]:
    database = database.resolve(strict=True)
    before = state(database)
    wal = database.with_name(database.name + "-wal")
    if wal.is_symlink() or (wal.exists() and wal.stat().st_size):
        raise ValueError("Use a frozen SQLite source without a pending WAL")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA temp_store=MEMORY")
    try:
        yield connection
    finally:
        connection.close()
        if state(database) != before or (wal.exists() and wal.stat().st_size):
            raise RuntimeError("Frozen database changed during comparison")


def projection(connection: sqlite3.Connection, score: str) -> tuple[str, str, str]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {row[1] for row in connection.execute("PRAGMA table_info(section_roles)")}
    translator = "a.section_translator" if "section_translator" in columns else "'' AS section_translator"
    pipeline = (
        "LEFT JOIN source_pipeline i ON i.book_id=b.id "
        if "source_pipeline" in tables
        else "LEFT JOIN (SELECT NULL book_id,NULL stage,NULL error WHERE 0) i ON i.book_id=b.id "
    )
    select = (
        "p.*,b.title,b.author,b.language,b.edition,b.work_id,b.source_url,b.rights,"
        "b.rights_url,b.artifact_sha256,b.content_sha256,"
        f"s.title section_title,{QUOTE} section_quote,"
        f"a.section_author,a.section_role,a.attribution_basis,{translator},{score} score,"
        "COALESCE(i.stage,b.status) ingestion_status,i.error ingestion_error "
    )
    return select, pipeline, "LEFT JOIN section_roles a ON a.section_id=s.id "


def statement(connection: sqlite3.Connection, case: Case, variant: str) -> tuple[str, tuple[Any, ...]]:
    filters = (case.book_id, case.book_id, case.language, case.language)
    if variant == "control":
        select, pipeline, roles = projection(connection, BM25)
        return (
            f"SELECT {select} FROM passage_fts JOIN passages p ON p.id=passage_fts.rowid "
            f"JOIN books b ON b.id=p.book_id {pipeline}JOIN sections s ON s.id=p.section_id {roles}"
            f"WHERE passage_fts MATCH ? AND {ELIGIBILITY} ORDER BY score,p.id LIMIT ?",
            (case.expression, *filters, case.candidate_limit),
        )
    if variant not in VARIANTS:
        raise ValueError("Unknown SQL variant")
    envelope = ""
    parameters: tuple[Any, ...] = (case.expression, *filters)
    if variant == "materialized_envelope" and case.book_id is not None:
        # Two ordered index seeks, not a combined MIN/MAX aggregate over the book.
        bounds = connection.execute(
            "SELECT (SELECT id FROM passages WHERE book_id=? ORDER BY id LIMIT 1),"
            "(SELECT id FROM passages WHERE book_id=? ORDER BY id DESC LIMIT 1)",
            (case.book_id, case.book_id),
        ).fetchone()
        low, high = bounds if bounds[0] is not None else (0, -1)
        envelope = " AND passage_fts.rowid>=? AND passage_fts.rowid<=?"
        parameters += (low, high)
    if variant == "pure_fts_eligible":
        # Eligibility must precede LIMIT: invalid/stale FTS rows cannot consume
        # the bounded pool. This EXISTS has no chapter text or ancillary joins.
        candidates = (
            f"SELECT passage_fts.rowid id,{BM25} score FROM passage_fts "
            "WHERE passage_fts MATCH ? AND EXISTS ("
            "SELECT 1 FROM passages p JOIN books b ON b.id=p.book_id "
            "JOIN sections s ON s.id=p.section_id "
            f"WHERE p.id=passage_fts.rowid AND {ELIGIBILITY}) "
            "ORDER BY score,passage_fts.rowid LIMIT ?"
        )
    else:
        candidates = (
            f"SELECT p.id,{BM25} score FROM passage_fts {METADATA_JOINS}"
            f"WHERE passage_fts MATCH ? AND {ELIGIBILITY}{envelope} ORDER BY score,p.id LIMIT ?"
        )
    select, pipeline, roles = projection(connection, "c.score")
    sql = (
        f"WITH candidates AS MATERIALIZED ({candidates}) SELECT {select} "
        "FROM candidates c JOIN passages p ON p.id=c.id JOIN books b ON b.id=p.book_id "
        f"{pipeline}JOIN sections s ON s.id=p.section_id {roles}ORDER BY c.score,p.id"
    )
    return sql, (*parameters, case.candidate_limit)


def execute(connection: sqlite3.Connection, case: Case, variant: str) -> list[sqlite3.Row]:
    # Statement construction includes the envelope lookup cost in timed runs.
    sql, parameters = statement(connection, case, variant)
    return connection.execute(sql, parameters).fetchall()


def final_results(case: Case, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    results = [Library._result(row) for row in rows]
    results = [result for result in results if result["verified"]]
    return rerank_results(prepare_query(case.query), results, case.limit) if case.mode == "lexical" else results


def guard(case: Case, reference: list[sqlite3.Row], candidate: list[sqlite3.Row]) -> dict[str, Any]:
    before = [dict(row) for row in reference]
    after = [dict(row) for row in candidate]
    if before != after or [(row["id"], row["score"].hex()) for row in reference] != [
        (row["id"], row["score"].hex()) for row in candidate
    ]:
        raise AssertionError(f"{case.name}: ordered IDs/scores or hydrated citation text changed")
    final = final_results(case, reference)
    if final != final_results(case, candidate):
        raise AssertionError(f"{case.name}: verified final result/citation changed")
    return {
        "ordered_ids": [row["id"] for row in reference],
        "bm25_hex_sha256": sha(json.dumps([row["score"].hex() for row in reference]).encode()),
        "hydrated_records_sha256": sha(json.dumps(before, sort_keys=True, ensure_ascii=False).encode()),
        "final_results_sha256": sha(json.dumps(final, sort_keys=True, ensure_ascii=False).encode()),
        "returned_candidates": len(reference),
        "verified_final_ids": [result["id"] for result in final],
        "all_exact": True,
    }


def compare(connection: sqlite3.Connection, case: Case, repeats: int) -> dict[str, Any]:
    variants = list(VARIANTS)
    if case.book_id is None:
        variants.remove("materialized_envelope")
    if case.book_id is not None or case.language is not None:
        variants.remove("pure_fts_eligible")
    reference = execute(connection, case, "control")
    report = {"case": case.__dict__, "candidate_limit": case.candidate_limit, "variants": {}}
    for variant in variants:
        rows = execute(connection, case, variant)
        verification = guard(case, reference, rows)
        sql, parameters = statement(connection, case, variant)
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)]
        quote_calls = 0

        def quote_probe(value: str | None) -> str | None:
            nonlocal quote_calls
            quote_calls += 1
            return value

        connection.create_function("quote_probe", 1, quote_probe)
        observed = connection.execute(sql.replace(QUOTE, f"quote_probe({QUOTE})"), parameters).fetchall()
        guard(case, reference, observed)
        report["variants"][variant] = {
            "guard": verification,
            "query_plan": plan,
            "sql_sha256": sha(sql.encode()),
            "sql": sql,
            "instrumented_quote_evaluations": quote_calls,
            "milliseconds": [],
        }
    generator = random.Random(20261115)
    for _ in range(repeats):
        generator.shuffle(variants)
        for variant in variants:
            started = time.perf_counter()
            rows = execute(connection, case, variant)
            elapsed = (time.perf_counter() - started) * 1000
            guard(case, reference, rows)
            report["variants"][variant]["milliseconds"].append(round(elapsed, 3))
    control = statistics.median(report["variants"]["control"]["milliseconds"])
    for measurement in report["variants"].values():
        median = statistics.median(measurement["milliseconds"])
        measurement["median_ms"] = median
        measurement["speedup_vs_same_query_control"] = round(control / median, 3) if median else None
    return report


def seed(database: Path, passages_per_book: int = 300) -> None:
    """Original CC0 synthetic data, including stale/orphan/corrupt edge cases."""
    connection = sqlite3.connect(database)
    connection.executescript(SCHEMA)
    connection.execute("PRAGMA foreign_keys=OFF")
    for book in range(9):
        identity = f"book-{book}" if book < 8 else "scattered"
        status = "failed" if book == 7 else "indexed"
        language = "de" if book % 2 else "en"
        connection.execute(
            "INSERT INTO books(id,title,author,language,work_id,edition,source_url,rights,status) "
            "VALUES(?,?,?,?,?,'CC0 synthetic SQL fixture','','CC0 1.0',?)",
            (identity, f"CC0 book {book}", "Book Atlas fixture", language, identity, status),
        )
        texts = []
        spans = []
        for ordinal in range(passages_per_book if book < 8 else 8):
            needle = "cobaltneedle" if ordinal % 97 == 0 else "copper"
            text = (
                f"Original CC0 ambercommon copper {needle} fixture {ordinal}. Пример 🦉 café. "
                + "reading context " * 28
            )
            start = sum(len(part) + 2 for part in texts)
            spans.append((start, start + len(text), text))
            texts.append(text)
        section_id = book + 1
        connection.execute(
            "INSERT INTO sections VALUES(?,?,0,?,?)", (section_id, identity, "CC0 chapter", "\n\n".join(texts))
        )
        connection.execute(
            "INSERT INTO section_roles VALUES(?,?,?,?,?)",
            (section_id, "CC0 editor", "editorial", "declared synthetic fixture", "CC0 translator"),
        )
        for ordinal, (start, end, text) in enumerate(spans):
            passage_id = book * 1000 + ordinal + 1 if book < 8 else ordinal * 1000 + 500
            connection.execute(
                "INSERT INTO passages(id,book_id,section_id,start,end,text,sha256) VALUES(?,?,?,?,?,?,?)",
                (passage_id, identity, section_id, start, end, text, sha(text.encode())),
            )
            connection.execute(
                "INSERT INTO passage_fts(rowid,text,title,author,section_title) VALUES(?,?,?,?,?)",
                (passage_id, text, f"CC0 book {book}", "Book Atlas fixture", "CC0 chapter"),
            )
    # Highest-score tiny FTS records must not starve eligibility before LIMIT.
    for identity, book, section in ((9001, None, None), (9002, "missing-book", 1), (9003, "book-0", 99999)):
        text = "ambercommon"
        if book is not None:
            connection.execute(
                "INSERT INTO passages(id,book_id,section_id,start,end,text,sha256) VALUES(?,?,?,?,?,?,?)",
                (identity, book, section, 0, len(text), text, sha(text.encode())),
            )
        connection.execute(
            "INSERT INTO passage_fts(rowid,text,title,author,section_title) VALUES(?,?,'','','')", (identity, text)
        )
    connection.execute("UPDATE passages SET sha256='corrupt-fixture' WHERE id=1001")
    connection.execute(
        "INSERT INTO source_pipeline(book_id,stage,error) VALUES('book-0','failed','Old edition remains available')"
    )
    connection.commit()
    connection.close()


def run(database: Path, cases: list[Case], repeats: int, *, frozen20: bool = False) -> dict[str, Any]:
    with readonly(database) as connection:
        counts = dict(connection.execute("SELECT status,COUNT(*) FROM books GROUP BY status"))
        passages = connection.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
        if frozen20 and (counts != {"indexed": 20} or passages != 12108):
            raise ValueError(
                "--frozen20 accepts only the fixed 20-edition/12,108-passage source, never the live library"
            )
        reports = [compare(connection, case, repeats) for case in cases]
    return {"indexed_status_counts": counts, "passages": passages, "cases": reports, "source_unchanged": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen20", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("data/bench-lexical-plan-v1/report.json"))
    arguments = parser.parse_args()
    if not 1 <= arguments.repeats <= 5:
        parser.error("Use 1..5 bounded repeats")
    if arguments.frozen20 and arguments.output.resolve().is_relative_to(arguments.frozen20.resolve()):
        parser.error("Output must remain outside the frozen source")
    synthetic_cases = [
        Case("global-common", "ambercommon"),
        Case("scoped-contiguous", "ambercommon", book_id="book-2"),
        Case("scoped-scattered", "ambercommon", book_id="scattered", mode="baseline"),
        Case("language-filter", "ambercommon", language="de"),
        Case("quoted-phrase", '"ambercommon copper"'),
        Case("no-match", "absentfixtureterm"),
    ]
    with tempfile.TemporaryDirectory(prefix="book-atlas-lexical-plan-") as temporary:
        database = Path(temporary) / "library.sqlite3"
        seed(database)
        report: dict[str, Any] = {
            "sqlite_version": sqlite3.sqlite_version,
            "synthetic_cc0": run(database, synthetic_cases, arguments.repeats),
        }
    if arguments.frozen20:
        database = arguments.frozen20 / "library.sqlite3"
        report["frozen20"] = run(
            database,
            [
                Case("global-man", "man"),
                Case("scoped-meditations-man", "man", book_id="pg2680-full"),
                Case("scoped-origin-struggle", "struggle for existence", book_id="pg1228-full"),
            ],
            min(arguments.repeats, 2),
            frozen20=True,
        )
    report["limitations"] = [
        "Warm same-process paired SQL timings; no cold-cache or live 4k throughput claim.",
        "Timed construction includes rowid-envelope index seeks and schema compatibility inspection.",
        "The legacy frozen source uses read-only empty/default projections for later schema columns.",
        "Indexed status and section eligibility precede LIMIT; ancillary roles/pipeline hydrate later.",
        "A rowid envelope is only a bound; exact book/language/status/section eligibility remains mandatory.",
        "Quote-probe counts use a separate instrumented pass, never the timed SQL.",
        "SQLite query-plan descriptions are version-specific diagnostics, not a stable application API.",
        "No core code, live corpus, final-test set, models or indexes were changed.",
    ]
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for label in ("synthetic_cc0", "frozen20"):
        for case in report.get(label, {}).get("cases", []):
            print(label, case["case"]["name"], {key: value["median_ms"] for key, value in case["variants"].items()})
    print("Report:", arguments.output)


if __name__ == "__main__":
    main()
