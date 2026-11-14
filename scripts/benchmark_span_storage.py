"""Controlled span-only storage experiment on a new owned SQLite copy.

Never instantiate Library or write to source/live databases. The resulting
sentinel database is deliberately incompatible with current production readers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import resource
import shutil
import sqlite3
import statistics
import struct
import sys
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GIB = 1024**3
EXPECTED_BOOKS = 1308
EXPECTED_PASSAGES = 445466
QUERIES = ("ocean", "curiosity", '"natural selection"', "freedom", "society", "жизнь", "raison", "Verwandlung")


def file_sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(path: Path) -> tuple[int, int, int, int, int]:
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def feed(digest: Any, values: Any) -> None:
    for value in values:
        if value is None:
            kind, data = b"N", b""
        elif isinstance(value, bytes):
            kind, data = b"B", value
        elif isinstance(value, str):
            kind, data = b"S", value.encode()
        else:
            kind, data = b"V", repr(value).encode()
        digest.update(kind + struct.pack(">Q", len(data)))
        digest.update(data)


def readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA cache_size=-32768")
    connection.execute("PRAGMA temp_store=FILE")
    return connection


def untouched_tables(connection: sqlite3.Connection) -> dict[str, Any]:
    """Fingerprint all logical metadata/sections/FTS shadows without loading tables."""
    results = {}
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT IN ('passages','passage_fts') ORDER BY name"
    ):
        fields = connection.execute("PRAGMA table_info(" + quoted(name) + ")").fetchall()
        primary = [r[1] for r in sorted(fields, key=lambda r: r[5]) if r[5]]
        order = ",".join(quoted(n) for n in primary) if primary else "rowid"
        digest = hashlib.sha256()
        count = 0
        for row in connection.execute("SELECT * FROM " + quoted(name) + " ORDER BY " + order):
            feed(digest, row)
            count += 1
        results[name] = {"rows": count, "sha256": digest.hexdigest()}
    schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    digest = hashlib.sha256()
    for row in schema:
        feed(digest, row)
    results["_schema"] = {"rows": len(schema), "sha256": digest.hexdigest()}
    return results


def page_accounting(connection: sqlite3.Connection) -> dict[str, Any]:
    result = {
        name: connection.execute("PRAGMA " + name).fetchone()[0]
        for name in ("page_count", "page_size", "freelist_count")
    }
    try:
        result["btree_pages"] = {
            row[0]: {"bytes": row[1], "payload_bytes": row[2], "unused_bytes": row[3]}
            for row in connection.execute("SELECT name,SUM(pgsize),SUM(payload),SUM(unused) FROM dbstat GROUP BY name")
        }
    except sqlite3.OperationalError as error:
        result["dbstat_unavailable"] = str(error)
    return result


def verify_spans(connection: sqlite3.Connection, *, sentinel: bool) -> dict[str, Any]:
    books = {row[0] for row in connection.execute("SELECT id FROM books")}
    text_digest, metadata_digest = hashlib.sha256(), hashlib.sha256()
    current_id = None
    section_text, section_book = "", ""
    section_count = count = characters = utf8_bytes = nonascii = 0
    started = time.perf_counter()
    for pid, book_id, section_id, start, end, stored_text, stored_sha in connection.execute(
        "SELECT id,book_id,section_id,start,end,text,sha256 FROM passages ORDER BY section_id,start,end,id"
    ):
        if section_id != current_id:
            row = connection.execute("SELECT book_id,text FROM sections WHERE id=?", (section_id,)).fetchone()
            if row is None:
                raise AssertionError(f"Missing section for passage {pid}")
            section_book, section_text = row
            current_id = section_id
            section_count += 1
        assert book_id in books and book_id == section_book, f"Wrong book/section binding for {pid}"
        assert type(start) is int and type(end) is int and 0 <= start < end <= len(section_text), f"Invalid span {pid}"
        value = section_text[start:end]
        encoded = value.encode()
        assert hashlib.sha256(encoded).hexdigest() == stored_sha, f"Reconstructed SHA mismatch for passage {pid}"
        assert stored_text == ("" if sentinel else value), f"Stored text mismatch for passage {pid}"
        feed(metadata_digest, (pid, book_id, section_id, start, end, stored_sha))
        feed(text_digest, (pid, value))
        count += 1
        characters += len(value)
        utf8_bytes += len(encoded)
        nonascii += not value.isascii()
    assert count == EXPECTED_PASSAGES and len(books) == EXPECTED_BOOKS
    return {
        "passages": count,
        "books": len(books),
        "sections_with_passages": section_count,
        "reconstructed_characters": characters,
        "reconstructed_utf8_bytes": utf8_bytes,
        "nonascii_passages": nonascii,
        "passage_metadata_sha256": metadata_digest.hexdigest(),
        "passage_text_sha256": text_digest.hexdigest(),
        "all_bounds_books_sections_and_sha_verified": True,
        "seconds": time.perf_counter() - started,
    }


def query(connection: sqlite3.Connection, q: str, *, spans: bool) -> list[dict[str, Any]]:
    expression = "substr(s.text,p.start+1,p.end-p.start)" if spans else "p.text"
    rows = connection.execute(
        "SELECT p.id,bm25(passage_fts,1.0,2.0,1.5,1.2) AS score,p.book_id,p.section_id,p.start,p.end,p.sha256,"
        "b.title,b.author,b.edition,s.title," + expression + " FROM passage_fts "
        "JOIN passages p ON p.id=passage_fts.rowid JOIN books b ON b.id=p.book_id "
        "JOIN sections s ON s.id=p.section_id WHERE passage_fts MATCH ? AND b.status='indexed' "
        "ORDER BY score,p.id LIMIT 20",
        (q,),
    ).fetchall()
    keys = (
        "id",
        "score",
        "book_id",
        "section_id",
        "start",
        "end",
        "sha256",
        "title",
        "author",
        "edition",
        "section_title",
        "text",
    )
    return [dict(zip(keys, row, strict=True)) for row in rows]


class SectionLRU:
    """Keep at most 16 MiB of Python section strings, never the whole corpus."""

    def __init__(self, connection: sqlite3.Connection, budget: int = 16 * 1024**2) -> None:
        self.connection = connection
        self.budget = budget
        self.entries: OrderedDict[int, str] = OrderedDict()
        self.bytes = self.peak = self.hits = self.misses = 0

    def section(self, section_id: int) -> str:
        if section_id in self.entries:
            self.hits += 1
            self.entries.move_to_end(section_id)
            return self.entries[section_id]
        self.misses += 1
        text = self.connection.execute("SELECT text FROM sections WHERE id=?", (section_id,)).fetchone()[0]
        cost = sys.getsizeof(text)
        if cost <= self.budget:
            while self.entries and (self.bytes + cost > self.budget or len(self.entries) >= 128):
                _, old = self.entries.popitem(last=False)
                self.bytes -= sys.getsizeof(old)
            self.entries[section_id] = text
            self.bytes += cost
            self.peak = max(self.peak, self.bytes)
        return text


def samples(connection: sqlite3.Connection) -> list[int]:
    selected: set[int] = set()
    # Actual shortest/longest sections with passages, plus one sample per
    # original language and stable random passages across the entire snapshot.
    for direction in ("ASC", "DESC"):
        for (section_id,) in connection.execute(
            "SELECT s.id FROM sections s WHERE EXISTS(SELECT 1 FROM passages p WHERE p.section_id=s.id) "
            "ORDER BY length(s.text) " + direction + ",s.id LIMIT 20"
        ):
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM passages WHERE section_id=? ORDER BY start", (section_id,)
                )
            ]
            selected.update((ids[0], ids[len(ids) // 2], ids[-1]))
    for (language,) in connection.execute("SELECT DISTINCT language FROM books ORDER BY language"):
        selected.update(
            row[0]
            for row in connection.execute(
                "SELECT p.id FROM passages p JOIN books b ON b.id=p.book_id WHERE b.language=? ORDER BY p.id LIMIT 20",
                (language,),
            )
        )
    count = connection.execute("SELECT MAX(id) FROM passages").fetchone()[0]
    for value in random.Random(7304).sample(range(1, count + 1), 256):
        row = connection.execute("SELECT id FROM passages WHERE id>=? ORDER BY id LIMIT 1", (value,)).fetchone()
        if row:
            selected.add(row[0])
    return sorted(selected)


def reconstruct(
    connection: sqlite3.Connection, ids: list[int], mode: str, lru: SectionLRU | None = None
) -> list[tuple[int, str]]:
    placeholders = ",".join("?" for _ in ids)
    if mode in {"stored", "sql_substr"}:
        expression = "p.text" if mode == "stored" else "substr(s.text,p.start+1,p.end-p.start)"
        return connection.execute(
            "SELECT p.id," + expression + " FROM passages p JOIN sections s ON s.id=p.section_id "
            "WHERE p.id IN (" + placeholders + ") ORDER BY p.id",
            ids,
        ).fetchall()
    assert lru is not None
    rows = connection.execute(
        "SELECT id,section_id,start,end FROM passages WHERE id IN (" + placeholders + ") ORDER BY id", ids
    )
    return [(pid, lru.section(sid)[start:end]) for pid, sid, start, end in rows]


def timing(function: Any, repeats: int = 5) -> tuple[Any, dict[str, Any]]:
    milliseconds = []
    result = None
    for _ in range(repeats):
        start = time.perf_counter()
        result = function()
        milliseconds.append((time.perf_counter() - start) * 1000)
    return result, {"runs_ms": milliseconds, "median_ms": statistics.median(milliseconds), "max_ms": max(milliseconds)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/snapshots/library1308-v1/library.sqlite3"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/benchmarks/span-storage1308-v1"))
    parser.add_argument("--report", type=Path, default=Path("experiments/span-storage-v1.json"))
    args = parser.parse_args()
    source, output, report_path = args.source.resolve(), args.output_dir.resolve(), args.report.resolve()
    allowed = (Path.cwd() / "data/benchmarks").resolve()
    if (
        not output.is_relative_to(allowed)
        or output.exists()
        or report_path.exists()
        or report_path.is_relative_to(source.parent)
    ):
        parser.error("Use a new owned data/benchmarks directory and a new report outside the frozen source")
    wal = source.with_name(source.name + "-wal")
    if source.is_symlink() or wal.exists() and wal.stat().st_size:
        parser.error("Requires an immutable snapshot without a nonempty WAL")
    initial = identity(source)
    disk_free_before = shutil.disk_usage(allowed.parent).free
    required = 4 * GIB + 3 * initial[2]
    if disk_free_before < required:
        parser.error(f"Need {required} free bytes including 4 GiB reserve and copy/journal/VACUUM budget")
    started = time.perf_counter()

    def progress(stage: str, **data: Any) -> None:
        print(
            json.dumps({"stage": stage, "elapsed_seconds": round(time.perf_counter() - started, 3), **data}), flush=True
        )

    source_sha = file_sha(source)
    original = readonly(source)
    try:
        before = verify_spans(original, sentinel=False)
        pages_before = page_accounting(original)
        progress("source_all_spans_verified", passages=before["passages"], seconds=round(before["seconds"], 3))
        unchanged_before = untouched_tables(original)
        ids = samples(original)
        expected = reconstruct(original, ids, "stored")
        raw_outputs = {q: query(original, q, spans=False) for q in QUERIES}
        assert all(raw_outputs.values()), "Every ranking probe must hit actual passages"
        output.mkdir(parents=True)
        database = output / "library.sqlite3"
        shutil.copyfile(source, database)
        assert identity(source) == initial and file_sha(database) == source_sha
        assert database.stat().st_ino != source.stat().st_ino
        progress("owned_copy_captured", bytes=database.stat().st_size, samples=len(ids))
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA cache_size=-32768")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA journal_mode=DELETE")
        clear_started = time.perf_counter()
        with connection:
            connection.execute("UPDATE passages SET text=''")
        clear_seconds = time.perf_counter() - clear_started
        assert connection.execute("SELECT COUNT(*) FROM passages WHERE text!=''").fetchone()[0] == 0
        free_before_vacuum = shutil.disk_usage(output).free
        if free_before_vacuum < 4 * GIB + database.stat().st_size:
            raise RuntimeError("Stop before VACUUM: copy would breach the 4 GiB reserve")
        vacuum_started = time.perf_counter()
        connection.execute("VACUUM")
        vacuum_seconds = time.perf_counter() - vacuum_started
        progress("owned_copy_sentinel_vacuum_done", bytes=database.stat().st_size)
        after = verify_spans(connection, sentinel=True)
        pages_after = page_accounting(connection)
        unchanged_after = untouched_tables(connection)
        assert before["passage_metadata_sha256"] == after["passage_metadata_sha256"]
        assert before["passage_text_sha256"] == after["passage_text_sha256"]
        assert unchanged_before == unchanged_after, "Non-passage text/metadata/FTS shadow logical data changed"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        progress("all_reconstructed_spans_and_fts_shadows_verified", passages=after["passages"])
        rebuilt_outputs = {q: query(connection, q, spans=True) for q in QUERIES}
        assert raw_outputs == rebuilt_outputs, "Ranking IDs/scores/metadata or reconstructed query text changed"
        ranking_timings = []
        for q in QUERIES:
            _, raw_time = timing(lambda q=q: query(original, q, spans=False), 3)
            _, span_time = timing(lambda q=q: query(connection, q, spans=True), 3)
            ranking_timings.append({"query": q, "stored": raw_time, "sql_substr": span_time})
        baseline, raw_latency = timing(lambda: reconstruct(original, ids, "stored"))
        sql_result, sql_latency = timing(lambda: reconstruct(connection, ids, "sql_substr"))
        lru = SectionLRU(connection)
        python_result, python_latency = timing(lambda: reconstruct(connection, ids, "python_lru", lru))
        assert expected == baseline == sql_result == python_result
        languages = connection.execute(
            "SELECT DISTINCT b.language FROM passages p JOIN books b ON b.id=p.book_id WHERE p.id IN ("
            + ",".join("?" for _ in ids)
            + ") ORDER BY b.language",
            ids,
        ).fetchall()
        connection.close()
        assert identity(source) == initial and file_sha(source) == source_sha
        assert not wal.exists() or not wal.stat().st_size
        (output / "query-output-comparison.json").write_text(
            json.dumps({"stored": raw_outputs, "sql_substr": rebuilt_outputs}, ensure_ascii=False, indent=2) + "\n"
        )
        (output / "reconstruction-output-comparison.json").write_text(
            json.dumps(
                {"ids": ids, "stored": expected, "sql_substr": sql_result, "python_lru": python_result},
                ensure_ascii=False,
            )
            + "\n"
        )
        report = {
            "schema_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "source": str(source.relative_to(Path.cwd())),
            "source_sha256": source_sha,
            "source_unchanged": True,
            "owned_copy": str(database.relative_to(Path.cwd())),
            "sqlite_version": sqlite3.sqlite_version,
            "storage": {
                "before_bytes": initial[2],
                "after_bytes": database.stat().st_size,
                "saved_bytes": initial[2] - database.stat().st_size,
                "saved_percent": 100 * (initial[2] - database.stat().st_size) / initial[2],
            },
            "page_accounting": {"before": pages_before, "after": pages_after},
            "resource": {
                "disk_free_before": disk_free_before,
                "required_start_free": required,
                "disk_free_before_vacuum": free_before_vacuum,
                "disk_free_after": shutil.disk_usage(output).free,
                "reserve_bytes": 4 * GIB,
                "clear_seconds": clear_seconds,
                "vacuum_seconds": vacuum_seconds,
                "total_seconds": time.perf_counter() - started,
                "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1024 if sys.platform != "darwin" else 1),
                "sqlite_cache_kib_per_connection": 32768,
                "python_lru_budget_bytes": lru.budget,
                "python_lru_peak_string_bytes": lru.peak,
                "python_lru_hits": lru.hits,
                "python_lru_misses": lru.misses,
            },
            "before": before,
            "after": after,
            "all_non_passage_tables_and_fts_shadows_unchanged": True,
            "unchanged_table_fingerprints": unchanged_before,
            "query_outputs_identical": True,
            "bm25_ids_and_scores_identical": True,
            "query_probes": [
                {
                    "query": q,
                    "hits": [[r["id"], r["score"]] for r in raw_outputs[q]],
                    "text_output_sha256": hashlib.sha256(
                        json.dumps(raw_outputs[q], sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest(),
                }
                for q in QUERIES
            ],
            "query_latency": ranking_timings,
            "reconstruction": {
                "sample_passages": len(ids),
                "sample_languages": [r[0] for r in languages],
                "includes_short_and_long_sections": True,
                "all_sample_outputs_identical": True,
                "stored": raw_latency,
                "sql_substr": sql_latency,
                "python_lru": python_latency,
            },
            "production_compatible": False,
            "limitations": [
                "Owned copy only; current Library/embedding readers need passages.text and cannot use empty sentinels.",
                "Python reconstruction checked every passage; SQL substr checked bounded actual query/sample IDs only.",
                "Local warm-cache latency, stored then reconstructed runs; not a cold-disk or relevance benchmark.",
                "No schema migration, production/FTS change, book acquisition or embedding run is performed.",
            ],
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        progress("passed", storage=report["storage"], reconstruction_samples=len(ids))
    finally:
        original.close()


if __name__ == "__main__":
    main()
