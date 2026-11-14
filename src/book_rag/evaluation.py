"""Small, reproducible retrieval evaluation with independently checked source anchors.

The fixture measures a deliberately small corpus. Scores are not a claim about
10–50k books or general question answering. Dataset entries never alter ranking.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import resource
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from book_rag.library import Library

_EVALUATION_ENV_LOCK = threading.RLock()
_CACHE_COPY_LIMIT = 1024 * 1024


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _database_files(directory: Path) -> dict[str, tuple[int, int, int, int, int]]:
    names = ("library.sqlite3", "library.sqlite3-wal")
    return {name: _file_identity(directory / name) for name in names if (directory / name).exists()}


def _capture_database(
    source: Path, destination: Path
) -> tuple[dict[str, str], dict[str, tuple[int, int, int, int, int]]]:
    """Copy a stable SQLite main/WAL pair without opening the source in SQLite.

    SQLite reconstructs its shared-memory file only inside the owned directory.
    A live writer changing either input during capture makes the run fail closed.
    """
    before = _database_files(source)
    if "library.sqlite3" not in before:
        raise FileNotFoundError(source / "library.sqlite3")
    hashes = {}
    for name in before:
        digest = hashlib.sha256()
        with (source / name).open("rb") as original, (destination / name).open("xb") as copied:
            while block := original.read(1024 * 1024):
                digest.update(block)
                copied.write(block)
        hashes[name] = digest.hexdigest()
    if _database_files(source) != before:
        raise RuntimeError("Source SQLite changed during evaluation capture; use a stable snapshot")
    return hashes, before


def _generation_identity(directory: Path) -> dict[str, tuple[int, int, int, int, int]]:
    return {
        str(path.relative_to(directory)): _file_identity(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _copy_model_cache(source: Path, destination: Path) -> dict[str, int]:
    """Own mutable cache metadata while reusing large, completed weight files."""
    reused = {"linked_files": 0, "linked_bytes": 0, "copied_files": 0, "copied_bytes": 0}
    if not source.is_dir():
        destination.mkdir()
        return reused

    def copy_file(original: str, copied: str) -> str:
        path = Path(original)
        size = path.stat().st_size
        if size > _CACHE_COPY_LIMIT:
            Path(copied).symlink_to(path.resolve())
            reused["linked_files"] += 1
            reused["linked_bytes"] += size
        else:
            shutil.copyfile(path, copied)
            reused["copied_files"] += 1
            reused["copied_bytes"] += size
        return copied

    shutil.copytree(
        source,
        destination,
        symlinks=False,
        ignore_dangling_symlinks=True,
        copy_function=copy_file,
        ignore=shutil.ignore_patterns(".locks", "*.lock", "*.incomplete"),
    )
    # Source directory permissions do not make this process's cache metadata readonly.
    for directory, _, _ in os.walk(destination):
        Path(directory).chmod(0o700)
    return reused


class EvaluationLibrary(Library):
    """An owned migrated database, read-only query connections and frozen vectors.

    Construct through ``evaluation_library``; the original database is never
    passed to ``Library.__init__``. Large arrays are consumed by semantic search's
    existing ``np.load(..., mmap_mode='r')`` path, without copying or rebuilding.
    """

    def __init__(self, directory: Path, input_provenance: dict[str, Any]) -> None:
        migrated = Library(directory)
        self.data_dir = migrated.data_dir
        self.db_path = migrated.db_path
        self.input_provenance = input_provenance

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.create_function("casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
        connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            connection.close()


@contextmanager
def evaluation_library(directory: Path, *, model_cache: Path | None = None) -> Generator[EvaluationLibrary]:
    """Isolate schema/cache writes and reuse the selected immutable generation.

    Temporary files are owned and removed by this context. Input SQLite bytes
    are copied once; large vector/weight bytes are never copied. Environment
    cache routing is restored on exit and contexts are serialized in-process.
    """
    source = directory.expanduser().resolve()
    with _EVALUATION_ENV_LOCK, tempfile.TemporaryDirectory(prefix="bookrag-evaluation-") as temporary:
        preparation_started = time.perf_counter()
        working = Path(temporary).resolve()
        database_hashes, database_identity = _capture_database(source, working)
        original_cache = os.environ.get("BOOKRAG_MODEL_CACHE")
        cache_source = (model_cache or Path(original_cache or source / "models")).expanduser().resolve()
        cache_reuse = _copy_model_cache(cache_source, working / "models")
        active = source / "vectors" / "active.json"
        active_bytes = active.read_bytes() if active.is_file() else None
        generation = None
        generation_path = None
        generation_identity = None
        if active_bytes is not None:
            metadata = json.loads(active_bytes)
            generation = metadata.get("generation")
            if generation is not None and (
                not isinstance(generation, str) or not re.fullmatch(r"gen-[A-Za-z0-9_.-]+", generation)
            ):
                raise ValueError("Invalid evaluation vector generation name")
            vectors = working / "vectors"
            vectors.mkdir()
            (vectors / "active.json").write_bytes(active_bytes)
            if generation is not None:
                candidate = (active.parent / generation).resolve()
                if not candidate.is_relative_to(active.parent.resolve()):
                    raise ValueError("Evaluation vector generation is outside its source directory")
                if candidate.is_dir():
                    generation_path = candidate
                    generation_identity = _generation_identity(generation_path)
                    (vectors / generation).symlink_to(generation_path, target_is_directory=True)
        provenance = {
            "source_directory": str(source),
            "source_database_sha256": database_hashes["library.sqlite3"],
            "source_sqlite_files_sha256": database_hashes,
            "copied_database_bytes": sum(identity[2] for identity in database_identity.values()),
            "database_access": "owned migrated copy; read-only evaluation connections",
            "vector_generation": generation,
            "active_metadata_sha256": hashlib.sha256(active_bytes).hexdigest() if active_bytes is not None else None,
            "vector_access": "selected generation reused without copying; mmap_mode=r"
            if generation_path is not None
            else "no ready selected generation",
            "reused_vector_bytes": sum(identity[2] for identity in (generation_identity or {}).values()),
            "model_cache_access": "owned metadata cache; large completed files reused by reference",
            "model_cache_reuse": cache_reuse,
        }
        library = EvaluationLibrary(working, provenance)
        provenance["preparation_seconds"] = round(time.perf_counter() - preparation_started, 6)
        os.environ["BOOKRAG_MODEL_CACHE"] = str(working / "models")
        try:
            yield library
            if _database_files(source) != database_identity:
                raise RuntimeError("Source SQLite changed during evaluation; discard this run")
            current_active = active.read_bytes() if active.is_file() else None
            if current_active != active_bytes:
                raise RuntimeError("Source vector selection changed during evaluation; discard this run")
            if generation_path is not None and _generation_identity(generation_path) != generation_identity:
                raise RuntimeError("Source vector generation changed during evaluation; discard this run")
        finally:
            if original_cache is None:
                os.environ.pop("BOOKRAG_MODEL_CACHE", None)
            else:
                os.environ["BOOKRAG_MODEL_CACHE"] = original_cache


class SearchLibrary(Protocol):
    """The read-only surface needed by the evaluator."""

    def search(
        self,
        query: str,
        mode: str = "lexical",
        book_id: str | None = None,
        language: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]: ...

    def get_book(self, book_id: str) -> dict[str, Any] | None: ...

    def stats(self) -> dict[str, Any]: ...

    def books(self, language: str | None = None, q: str = "") -> list[dict[str, Any]]: ...


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def verify_result(result: Mapping[str, Any], book: Mapping[str, Any] | None) -> dict[str, bool]:
    """Validate a quote against canonical section text, without trusting `verified`.

    Offsets are zero-based Python Unicode code points in the parsed section.
    This validates extraction integrity, not OCR fidelity to a physical edition.
    """
    checks = {"quote_exact": False, "metadata_exact": False, "reader_anchor_exact": False}
    if book is None or result.get("book_id") != book.get("id"):
        return checks
    section = next((item for item in book.get("sections", []) if item.get("id") == result.get("section_id")), None)
    if section is None:
        return checks
    start, end, text = result.get("start"), result.get("end"), result.get("text")
    source = section.get("text", "")
    valid_offsets = (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end <= len(source)
    )
    checks["quote_exact"] = bool(valid_offsets and isinstance(text, str) and source[start:end] == text)
    citation = result.get("citation", "")
    checks["metadata_exact"] = (
        all(result.get(key) == book.get(key) and bool(book.get(key)) for key in ("title", "author", "edition"))
        and isinstance(citation, str)
        and all(str(book[key]) in citation for key in ("title", "author", "edition"))
        and result.get("section_title") == section.get("title")
    )
    parameters = parse_qs(urlparse(str(result.get("reader_url", ""))).query)
    checks["reader_anchor_exact"] = all(
        parameters.get(key) == [str(value)]
        for key, value in (("book", book.get("id")), ("section", section.get("id")), ("passage", result.get("id")))
    )
    return checks


def _is_relevant(result: Mapping[str, Any], book: Mapping[str, Any] | None, case: Mapping[str, Any]) -> bool:
    relevant_works = set(case.get("relevant_work_ids", []))
    relevant_books = set(case.get("relevant_book_ids", []))
    identity_matches = result.get("book_id") in relevant_books or (
        book is not None and book.get("work_id") in relevant_works
    )
    required = case.get("evidence_contains", [])
    text = str(result.get("text", "")).casefold()
    return identity_matches and (not required or any(fragment.casefold() in text for fragment in required))


def load_dataset(path: Path) -> dict[str, Any]:
    """Read and validate a versioned query fixture."""
    dataset = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(dataset, dict) or dataset.get("version") != 1 or not isinstance(dataset.get("queries"), list):
        raise ValueError("Expected evaluation dataset version 1 with a queries list")
    if dataset.get("split") not in {"dev", "heldout"}:
        raise ValueError("Dataset split must be dev or heldout")
    ids: set[str] = set()
    for case in dataset["queries"]:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not isinstance(case.get("query"), str):
            raise ValueError("Every evaluation case needs string id and query")
        if case["id"] in ids or not case["query"].strip():
            raise ValueError("Query IDs must be unique and queries nonempty")
        ids.add(case["id"])
        if not isinstance(case.get("answerable"), bool):
            raise ValueError("Every query needs explicit boolean answerable")
        if case["answerable"] and not (case.get("relevant_work_ids") or case.get("relevant_book_ids")):
            raise ValueError("Answerable queries need relevant work or book IDs")
        for key in ("relevant_work_ids", "relevant_book_ids", "evidence_contains"):
            if key in case and (
                not isinstance(case[key], list) or not all(isinstance(value, str) and value for value in case[key])
            ):
                raise ValueError(f"{key} must be a list of nonempty strings")
        filters = case.get("filters", {})
        if not isinstance(filters, dict) or set(filters) - {"book_id", "language"}:
            raise ValueError("Only book_id and language query filters are supported")
        if not all(isinstance(value, str) and value for value in filters.values()):
            raise ValueError("Query filter values must be nonempty strings")
    return dataset


def evaluate(library: SearchLibrary | Path, dataset: Path, mode: str = "lexical", limit: int = 5) -> dict[str, Any]:
    """Evaluate retrieval, work coverage, source anchors, abstention and latency.

    Reads both books and sections independently of the search response. An empty
    result is the retrieval baseline's abstention signal. This does not measure
    correctness of a generated answer or treat semantic similarity as evidence.
    Pass a data-directory Path to isolate a frozen input automatically. Existing
    SearchLibrary callers remain supported, including in-memory test doubles.
    """
    if isinstance(library, Path):
        with evaluation_library(library) as isolated:
            return evaluate(isolated, dataset, mode, limit)
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    fixture = load_dataset(dataset)
    books: dict[str, dict[str, Any] | None] = {}
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    citation_checks: Counter[str] = Counter()
    actual_modes: Counter[str] = Counter()
    model_names: set[str] = set()
    result_count = 0
    started = time.perf_counter()
    for case in fixture["queries"]:
        query_started = time.perf_counter()
        response = library.search(case["query"], mode=mode, limit=limit, **case.get("filters", {}))
        actual_modes[str(response.get("mode", mode))] += 1
        if response.get("model"):
            model_names.add(str(response["model"]))
        elapsed_ms = (time.perf_counter() - query_started) * 1000
        latencies.append(elapsed_ms)
        results = response["results"][:limit]
        first_relevant_rank = None
        retrieved_work_ids: set[str] = set()
        retrieved_book_ids: set[str] = set()
        relevant_results = 0
        for rank, result in enumerate(results, start=1):
            book_id = str(result["book_id"])
            if book_id not in books:
                books[book_id] = library.get_book(book_id)
            book = books[book_id]
            checks = verify_result(result, book)
            citation_checks.update({key: int(value) for key, value in checks.items()})
            result_count += 1
            if book:
                retrieved_work_ids.add(str(book.get("work_id", "")))
            retrieved_book_ids.add(book_id)
            if _is_relevant(result, book, case):
                relevant_results += 1
                if first_relevant_rank is None:
                    first_relevant_rank = rank
        relevant_works = set(case.get("relevant_work_ids", []))
        relevant_books = set(case.get("relevant_book_ids", []))
        target_count = len(relevant_works) + len(relevant_books)
        covered = len(relevant_works & retrieved_work_ids) + len(relevant_books & retrieved_book_ids)
        rows.append(
            {
                "id": case["id"],
                "query": case["query"],
                "type": case.get("type", "unspecified"),
                "language": case.get("language", "und"),
                "answerable": case["answerable"],
                "reciprocal_rank": 1 / first_relevant_rank if first_relevant_rank else 0.0,
                "work_recall_at_k": covered / target_count if target_count else None,
                "hit_at_k": first_relevant_rank is not None,
                "precision_at_k": relevant_results / limit,
                "correct_abstention": not results if not case["answerable"] else None,
                "results": [result["id"] for result in results],
                "elapsed_ms": round(elapsed_ms, 3),
                "warnings": response.get("warnings", []),
            }
        )
    positives = [row for row in rows if row["answerable"]]
    negatives = [row for row in rows if not row["answerable"]]

    def mean(key: str, selected: list[dict[str, Any]]) -> float | None:
        values = [row[key] for row in selected if row[key] is not None]
        return sum(values) / len(values) if values else None

    by_type = {
        kind: {
            "queries": len(selected),
            "mrr_at_k": mean("reciprocal_rank", selected),
            "hit_at_k": mean("hit_at_k", selected),
        }
        for kind in sorted({row["type"] for row in positives})
        if (selected := [row for row in positives if row["type"] == kind])
    }
    by_language = {
        language: {"queries": len(selected), "mrr_at_k": mean("reciprocal_rank", selected)}
        for language in sorted({row["language"] for row in positives})
        if (selected := [row for row in positives if row["language"] == language])
    }
    snapshot_fields = (
        "id",
        "work_id",
        "title",
        "author",
        "language",
        "edition",
        "status",
        "artifact_sha256",
        "content_sha256",
        "source_url",
        "rights",
        "rights_url",
    )
    corpus_snapshot = [
        {key: book.get(key) for key in snapshot_fields} for book in sorted(library.books(), key=lambda book: book["id"])
    ]
    corpus_sha256 = hashlib.sha256(
        json.dumps(corpus_snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_rss_mib = peak_rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
    return {
        "schema_version": 1,
        **({"evaluation_input": library.input_provenance} if isinstance(library, EvaluationLibrary) else {}),
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": dataset.name,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "split": fixture["split"],
        "mode": mode,
        "actual_search_modes": dict(actual_modes),
        "embedding_models": sorted(model_names),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "implementation_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(__file__).parent.glob("*.py"))
        },
        "k": limit,
        "corpus": library.stats(),
        "corpus_sha256": corpus_sha256,
        "corpus_snapshot": corpus_snapshot,
        "queries": len(rows),
        "answerable_queries": len(positives),
        "negative_queries": len(negatives),
        "metrics": {
            "mrr_at_k": mean("reciprocal_rank", positives),
            "hit_at_k": mean("hit_at_k", positives),
            "work_recall_at_k": mean("work_recall_at_k", positives),
            "precision_at_k": mean("precision_at_k", positives),
            "negative_abstention_accuracy": mean("correct_abstention", negatives),
            "quote_exact_rate": citation_checks["quote_exact"] / result_count if result_count else None,
            "metadata_exact_rate": citation_checks["metadata_exact"] / result_count if result_count else None,
            "reader_anchor_exact_rate": citation_checks["reader_anchor_exact"] / result_count if result_count else None,
            "returned_passages": result_count,
            "latency_p50_ms": round(_percentile(latencies, 0.5), 3),
            "latency_p95_ms": round(_percentile(latencies, 0.95), 3),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "process_peak_rss_mib": round(peak_rss_mib, 2),
        },
        "by_type": by_type,
        "by_language": by_language,
        "cases": rows,
        "limitations": [
            "Small curated excerpts; not representative of a 10–50k-work library.",
            "Ground truth is manually authored and can be incomplete.",
            "Quote integrity is relative to parsed text, not a facsimile or OCR audit.",
            "Process peak RSS includes Python, imported libraries, and earlier work in this process.",
            "No generated-answer correctness, index-build cost, or paid API spending is measured here.",
        ],
    }


def compare_reports(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Compare compatible runs, retaining direction and absolute deltas.

    Dataset digest, corpus metadata/content digest, split, k and indexed counts
    must match. This prevents comparing runs built from different sources.
    """
    for key in ("schema_version", "dataset_sha256", "corpus_sha256", "split", "k"):
        if baseline.get(key) != candidate.get(key):
            raise ValueError(f"Cannot compare runs with different {key}")
    for key in ("books", "works", "passages"):
        if baseline.get("corpus", {}).get(key) != candidate.get("corpus", {}).get(key):
            raise ValueError(f"Cannot compare different corpus {key}")
    deltas = {}
    lower_is_better = {"latency_p50_ms", "latency_p95_ms", "elapsed_seconds", "process_peak_rss_mib"}
    for key, before in baseline.get("metrics", {}).items():
        after = candidate.get("metrics", {}).get(key)
        if isinstance(before, (float, int)) and isinstance(after, (float, int)) and key != "returned_passages":
            deltas[key] = {
                "baseline": before,
                "candidate": after,
                "delta": after - before,
                "higher_is_better": key not in lower_is_better,
            }
    return {
        "schema_version": 1,
        "dataset_sha256": baseline.get("dataset_sha256"),
        "split": baseline.get("split"),
        "k": baseline.get("k"),
        "baseline_mode": baseline.get("mode"),
        "candidate_mode": candidate.get("mode"),
        "metrics": deltas,
        "corpus_sha256": baseline.get("corpus_sha256"),
    }
