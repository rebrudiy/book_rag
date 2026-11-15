"""Owned CC0 mechanics proof with precomputed vectors, not semantic quality.

No live DB or model is opened. Native FAISS is confined to the child process.
Original vectors/codecs are read only; all metadata, failures and outputs are
new owned fixtures. The old-vector-to-CC0 assignment is explicitly synthetic.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import resource
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

if __name__ == "__main__":
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ[variable] = "2"

import numpy as np  # noqa: E402

from book_rag.experimental_ivfpq import PQConfig, faiss_backend, file_sha256, read_verified_index, train_codec  # noqa: E402
from book_rag.incremental_ivfpq import Window, build_incremental, query_incremental  # noqa: E402
from book_rag.index_coverage import ensure_epoch_schema  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RESERVE = int(4.5 * 1024**3)
MAX_RSS = 1024**3
MAX_DISK = 300 * 1024**2
FAILPOINTS = (
    "chunk-payload-published",
    "chunk-receipt-published",
    "checkpoint-published",
    "shard-index-published",
    "shard-receipt-published",
    "before-ready-publication",
)


class ReadOnlySource:
    def __init__(self, path: Path):
        self.db_path = path.resolve()

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            connection.close()


def create_source(path: Path, rows: int) -> ReadOnlySource:
    if path.exists() or rows % 4:
        raise ValueError("Fixture needs a fresh owned database and four equal books")
    connection = sqlite3.connect(path)
    connection.executescript("""
    CREATE TABLE books(id TEXT PRIMARY KEY,title TEXT,author TEXT,language TEXT,status TEXT);
    CREATE TABLE sections(id INTEGER PRIMARY KEY,book_id TEXT,ordinal INTEGER,title TEXT,text TEXT);
    CREATE TABLE passages(id INTEGER PRIMARY KEY,book_id TEXT,section_id INTEGER,
      start INTEGER,end INTEGER,text TEXT,sha256 TEXT);
    """)
    for number, language in enumerate(("en", "ru", "de", "fr")):
        book = f"cc0-proof-{number}"
        connection.execute(
            "INSERT INTO books VALUES(?,?,?,?,?)",
            (book, f"CC0 proof book {number}", "CC0 harness", language, "indexed"),
        )
        fragments = [text_for(row) for row in range(number * rows // 4, (number + 1) * rows // 4)]
        text = "\n".join(fragments)
        section = number + 1
        connection.execute("INSERT INTO sections VALUES(?,?,?,?,?)", (section, book, 0, "Synthetic chapter", text))
        start = 0
        for row, fragment in zip(range(number * rows // 4, (number + 1) * rows // 4), fragments, strict=True):
            connection.execute(
                "INSERT INTO passages VALUES(?,?,?,?,?,?,?)",
                (
                    row + 1,
                    book,
                    section,
                    start,
                    start + len(fragment),
                    fragment,
                    hashlib.sha256(fragment.encode()).hexdigest(),
                ),
            )
            start += len(fragment) + 1
    connection.execute("INSERT INTO books VALUES('cc0-outside','Outside fixture','CC0 harness','es','indexed')")
    outside = "CC0 outside-coverage fixture; no vector is assigned to this text."
    connection.execute("INSERT INTO sections VALUES(5,'cc0-outside',0,'Outside',?)", (outside,))
    connection.execute(
        "INSERT INTO passages VALUES(?,'cc0-outside',5,0,?,?,?)",
        (rows + 1, len(outside), outside, hashlib.sha256(outside.encode()).hexdigest()),
    )
    ensure_epoch_schema(connection)
    connection.commit()
    connection.close()
    return ReadOnlySource(path)


def text_for(row: int) -> str:
    return f"CC0 synthetic mechanics fixture {row:06d}. Frozen vector assignment is not a semantic claim."


def windows(rows: int) -> Generator[Window]:
    for row in range(rows):
        yield Window(row, row + 1, f"cc0-proof-{row // (rows // 4)}", text_for(row))


def model_space(dimensions: int, *, synthetic: bool) -> dict[str, Any]:
    return {
        "model": "owned-CC0-random" if synthetic else "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "revision": "1" * 40 if synthetic else "e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
        "dimensions": dimensions,
        "profile": {} if synthetic else {"name": "minilm-v2", "query_prefix": "", "document_prefix": ""},
        "strategy": "owned-normalized-random" if synthetic else "token-windows-v2",
        "assignment": "TEST ONLY: external stored vectors assigned to CC0 records; no text embedding claim",
    }


def mutate(source: ReadOnlySource, sql: str) -> None:
    with sqlite3.connect(source.db_path) as connection:
        connection.execute(sql)


def atomic_case(
    output: Path, event: str, vectors: np.ndarray, codec: Path, codec_sha: str, space: dict[str, Any]
) -> dict[str, Any]:
    rows = 1024
    output.mkdir(parents=True)
    source = create_source(output / "owned.sqlite3", rows)
    calls: Counter[int] = Counter()

    def encode(records: list[Window]) -> np.ndarray:
        calls.update(record.window_id for record in records)
        return np.ascontiguousarray(vectors[[record.window_id for record in records]])

    fired = False

    def interrupt(actual: str, number: int) -> None:
        nonlocal fired
        if actual == event and not fired:
            fired = True
            raise OSError("Owned simulated crash at " + event)

    kwargs: dict[str, Any] = {
        "source": source,
        "output": output,
        "codec_path": codec,
        "codec_sha256": codec_sha,
        "model_space": space,
        "rows_by_book": {f"cc0-proof-{j}": rows // 4 for j in range(4)},
        "chunk_rows": 128,
        "shard_rows": 512,
    }
    try:
        build_incremental(windows=windows(rows), encode=encode, failpoint=interrupt, **kwargs)
    except OSError as error:
        if "Owned simulated crash" not in str(error):
            raise
    else:
        raise AssertionError("Failpoint did not interrupt")
    directory = next(output.glob("incremental-*"))
    if (directory / "ready.json").exists() or list(directory.glob(".incremental-owned-*")):
        raise AssertionError("Failed build leaked ready state or temporary publication")
    first_calls = sum(calls.values())
    resumed = build_incremental(windows=windows(rows), encode=encode, **kwargs)
    if not resumed["complete"] or list(directory.glob("chunk-*.npz")):
        raise AssertionError("Resume/committed chunk reclamation failed")
    repeated = sum(count - 1 for count in calls.values())
    expected_repeated = 128 if event == "chunk-payload-published" else 0
    if repeated != expected_repeated or set(calls) != set(range(rows)):
        raise AssertionError("Resume re-encoded durable inputs or omitted rows")
    return {
        "event": event,
        "passed": True,
        "first_attempt_encoded_rows": first_calls,
        "total_encoded_rows": sum(calls.values()),
        "repeated_uncommitted_rows": repeated,
        "resumed_chunks": resumed["resumed_chunks"],
    }


def main_proof(
    output: Path, vectors_for: Any, rows: int, codec: Path, codec_sha: str, space: dict[str, Any]
) -> dict[str, Any]:
    source = create_source(output / "owned-cc0.sqlite3", rows)
    calls: Counter[int] = Counter()

    def encode(records: list[Window]) -> np.ndarray:
        calls.update(record.window_id for record in records)
        return vectors_for(np.array([record.window_id for record in records], dtype=np.int64))

    kwargs: dict[str, Any] = {
        "source": source,
        "output": output,
        "codec_path": codec,
        "codec_sha256": codec_sha,
        "model_space": space,
        "rows_by_book": {f"cc0-proof-{j}": rows // 4 for j in range(4)},
        "chunk_rows": 256,
        "shard_rows": max(512, rows // 8),
    }
    started = time.perf_counter()
    first = build_incremental(windows=windows(rows), encode=encode, stop_after_chunks=2, **kwargs)
    assert not first["complete"]
    directory = Path(first["directory"])
    assert not (directory / "ready.json").exists()
    resumed = build_incremental(windows=windows(rows), encode=encode, **kwargs)
    build_seconds = time.perf_counter() - started
    assert resumed["complete"] and resumed["resumed_chunks"] == 2
    assert len(calls) == rows and max(calls.values()) == 1
    assert not list(directory.glob("chunk-*.npz"))
    checkpoint_bytes = (directory / "checkpoint.json").stat().st_size
    query = vectors_for(np.array([7], dtype=np.int64))
    nprobe = min(32, json.loads((directory / "descriptor.json").read_text())["nlist"])
    found = query_incremental(source, directory, query, space, nprobe=nprobe, limit=100)
    assert found["covered_books"] == 4 and found["indexed_books"] == 5 and len(found["results"]) == 100
    # Independently add original floats in small batches with the same codec;
    # standalone-code shards must return the same native approximate candidates.
    backend = faiss_backend()
    control = read_verified_index(codec, codec_sha)
    for start in range(0, rows, 256):
        control.add_with_ids(
            vectors_for(np.arange(start, min(rows, start + 256), dtype=np.int64)),
            np.arange(start, min(rows, start + 256), dtype=np.int64),
        )
    control.nprobe = nprobe
    scores, ids = control.search(query, min(4096, rows))
    expected = sorted(
        [(float(score), int(identity)) for score, identity in zip(scores[0], ids[0], strict=True) if identity >= 0],
        key=lambda value: (-value[0], value[1]),
    )[:100]
    actual = [(hit["score"], hit["window_id"]) for hit in found["results"]]
    assert [row for _, row in actual] == [row for _, row in expected]
    assert (
        np.max(np.abs(np.array([score for score, _ in actual]) - np.array([score for score, _ in expected]))) < 0.000001
    )
    small = query_incremental(source, directory, query, space, nprobe=nprobe, limit=5)
    large = query_incremental(source, directory, query, space, nprobe=nprobe, limit=20)
    assert small["results"] == large["results"][:5]
    scoped = query_incremental(source, directory, query, space, book_id="cc0-proof-3", nprobe=nprobe)
    assert scoped["results"] and all(hit["book_id"] == "cc0-proof-3" for hit in scoped["results"])
    ru = query_incremental(source, directory, query, space, language="ru", nprobe=nprobe)
    assert ru["results"] and all(hit["book_id"] == "cc0-proof-1" for hit in ru["results"])
    outside = query_incremental(source, directory, query, space, book_id="cc0-outside", nprobe=nprobe)
    assert outside["fallback_required"] and not outside["results"]
    mutate(source, "UPDATE books SET title='Outside changed' WHERE id='cc0-outside'")
    unchanged = query_incremental(source, directory, query, space, nprobe=nprobe, limit=100)
    assert unchanged["results"] == found["results"]
    mutate(source, "INSERT INTO books VALUES('cc0-new-outside','New outside','CC0','es','indexed')")
    expanded = query_incremental(source, directory, query, space, nprobe=nprobe)
    assert expanded["covered_books"] == 4 and expanded["indexed_books"] == 6
    mutate(source, "UPDATE books SET title='Covered changed' WHERE id='cc0-proof-0'")
    invalid = query_incremental(source, directory, query, space, book_id="cc0-proof-0", nprobe=nprobe)
    survivors = query_incremental(source, directory, query, space, nprobe=nprobe)
    assert invalid["fallback_required"] and not invalid["results"] and survivors["covered_books"] == 3
    assert all(hit["book_id"] != "cc0-proof-0" for hit in survivors["results"])
    changed = query_incremental(
        source,
        directory,
        query,
        space,
        nprobe=nprobe,
        after_search=lambda: mutate(source, "UPDATE books SET title='Concurrent changed' WHERE id='cc0-proof-1'"),
    )
    assert changed["source_changed"] and changed["fallback_required"] and not changed["results"]
    copied = output / "owned-corruption-copy"
    shutil.copytree(directory, copied)
    receipt = json.loads((copied / "ready.json").read_text())["shards"][-1]
    path = copied / f"shard-{receipt['start']:09d}-{receipt['end']:09d}.faiss"
    with path.open("ab") as handle:
        handle.write(b"owned corruption")
    try:
        query_incremental(source, copied, query, space, nprobe=nprobe)
    except ValueError as error:
        assert "checksum" in str(error)
    else:
        raise AssertionError("Corrupted sealed index was accepted")
    del backend
    return {
        "passed": True,
        "rows": rows,
        "build_seconds": build_seconds,
        "encoded_rows_total": sum(calls.values()),
        "resumed_chunks": resumed["resumed_chunks"],
        "checkpoint_bytes": checkpoint_bytes,
        "generation": directory.name,
        "standalone_vs_original_float_add_top100_equal": True,
        "source_assignment": space["assignment"],
        "scenarios": [
            "interruption-resume-zero-durable-reencode",
            "atomic-ready",
            "sealed-chunk-reclamation",
            "source-bound-4-of-5",
            "true-native-book-selector",
            "true-native-language-selector",
            "fixed-pool-limit-prefix",
            "outside-book-fallback",
            "outside-mutation-preserves-results",
            "outside-addition-4-of-6",
            "covered-change-invalidates-whole-affected-shards",
            "concurrent-covered-change-discards-results",
            "corrupt-index-fails-before-native-read",
        ],
    }


def worker(args: argparse.Namespace) -> None:
    os.environ["BOOKRAG_IVFPQ_WORKER"] = "1"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    implementations = [
        Path(__file__).resolve(),
        ROOT / "src/book_rag/incremental_ivfpq.py",
        ROOT / "src/book_rag/experimental_ivfpq.py",
        ROOT / "src/book_rag/index_coverage.py",
    ]
    implementation_before = {str(path.relative_to(ROOT)): file_sha256(path) for path in implementations}
    protected = []
    if args.self_test:
        rng = np.random.default_rng(20261115)
        vectors = rng.standard_normal((2048, 8), dtype=np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        codec = output / "owned-synthetic-codec.faiss"
        faiss_backend().write_index(
            train_codec(vectors[:1024], PQConfig(dimensions=8, nlist=4, subquantizers=2, bits=4, iterations=3)),
            str(codec),
        )
        codec_sha = file_sha256(codec)

        def vectors_for(selected: np.ndarray) -> np.ndarray:
            return np.ascontiguousarray(vectors[selected])

        space = model_space(8, synthetic=True)
        rows = 2048
        before = {}
    else:
        previous = json.loads(args.sample_report.read_text())
        folder = args.sample_report.parent
        source_rows = np.load(folder / "source_rows.npy", allow_pickle=False)[::4]
        source_dir = ROOT / "data/snapshots/library1308-v1/vectors"
        vector_path = source_dir / previous["source"]["generation"] / "embeddings.npy"
        matrix = np.load(vector_path, mmap_mode="r", allow_pickle=False)
        offset, dimensions = matrix.offset, matrix.shape[1]
        del matrix
        codec = Path(previous["result"]["build"]["directory"]) / "codec.faiss"
        codec_sha = file_sha256(codec)
        protected = [vector_path, codec, folder / "source_rows.npy", args.sample_report]
        before = {str(path): file_sha256(path) for path in protected}

        def vectors_for(selected: np.ndarray) -> np.ndarray:
            values = np.empty((len(selected), dimensions), dtype=np.float32)
            with vector_path.open("rb") as handle:
                for number, row in enumerate(source_rows[selected]):
                    raw = os.pread(handle.fileno(), dimensions * 4, offset + int(row) * dimensions * 4)
                    if len(raw) != dimensions * 4:
                        raise ValueError("Truncated frozen vector source")
                    values[number] = np.frombuffer(raw, dtype=np.float32)
            return values

        space = model_space(dimensions, synthetic=False)
        space["profile"] = previous["source"]["embedding_profile"]
        space["source_arrays_sha256"] = previous["source"]["arrays_sha256"]
        rows = len(source_rows)
    if args.case:
        report = atomic_case(
            output / "case", args.case, vectors_for(np.arange(1024, dtype=np.int64)), codec, codec_sha, space
        )
    else:
        report = main_proof(output, vectors_for, rows, codec, codec_sha, space)
        report["atomic_failures"] = [
            atomic_case(
                output / ("failure-" + event),
                event,
                vectors_for(np.arange(1024, dtype=np.int64)),
                codec,
                codec_sha,
                space,
            )
            for event in FAILPOINTS
        ]
    after = {str(path): file_sha256(path) for path in protected}
    assert before == after and "torch" not in sys.modules
    implementation_after = {str(path.relative_to(ROOT)): file_sha256(path) for path in implementations}
    if implementation_before != implementation_after:
        raise ValueError("Proof implementation changed during measurement; discard run")
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss = int(rss if sys.platform == "darwin" else rss * 1024)
    used = sum(path.stat().st_size for path in output.rglob("*") if path.is_file())
    if rss > MAX_RSS or used > MAX_DISK or shutil.disk_usage(output).free < RESERVE:
        raise ValueError("Owned proof exceeded resource budget")
    payload = {
        "schema_version": 1,
        "kind": "synthetic-CC0-metadata-mechanics-only",
        "result": report,
        "original_sources_before": before,
        "original_sources_after": after,
        "original_sources_unchanged": before == after,
        "original_database_opened": False,
        "torch_imported": False,
        "implementation_sha256": implementation_before,
        "faiss_version": faiss_backend().__version__,
        "peak_rss_bytes": rss,
        "owned_output_bytes": used,
        "limitations": [
            "Stored external vectors are assigned to synthetic CC0 records, not embeddings of those records.",
            "No relevance, RAG quality, production source admission or embedding throughput is measured here.",
        ],
    }
    (output / "report.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {"report": str(output / "report.json"), "passed": True, "peak_rss_bytes": rss, "owned_output_bytes": used}
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-report", type=Path, default=Path("data/bench/ivfpq-frozen1308-sample-v2/report.json"))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--case", choices=FAILPOINTS)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return
    if importlib.util.find_spec("faiss") is None:
        parser.error("Optional faiss-cpu unavailable")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Choose a new owned output folder")
    args.output.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.output).free < RESERVE + MAX_DISK:
        parser.error("Need 4.5GiB reserve plus300MiB bounded budget")
    command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = time.monotonic()
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                check = subprocess.run(
                    ["ps", "-o", "rss=", "-p", str(process.pid)], capture_output=True, text=True, check=False
                )
                if int(check.stdout.strip() or "0") * 1024 > MAX_RSS or time.monotonic() - started > 300:
                    raise RuntimeError("Owned FAISS worker exceeded RSS or300s deadline") from None
                if shutil.disk_usage(args.output).free < RESERVE:
                    raise RuntimeError("Free space crossed4.5GiB reserve") from None
    except BaseException:
        process.kill()
        process.wait()
        raise
    if stderr:
        print(stderr, file=sys.stderr)
    print(stdout.strip())
    if process.returncode:
        raise SystemExit(process.returncode)


if __name__ == "__main__":
    main()
