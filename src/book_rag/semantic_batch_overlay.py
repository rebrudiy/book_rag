"""Opt-in explicit-book registry for complete direct-live compressed batches.

Registration never copies or rebinds a generation. Global/language-only search
and the existing RU21 route retain their own behaviour. Native FAISS is loaded
only by the bounded fresh JSON subprocess at the bottom of this module.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

from book_rag.experimental_ivfpq import canonical_bytes
from book_rag.experimental_ivfpq import file_sha256 as _file_sha256
from book_rag.incremental_ivfpq import _shard_mapping, _shard_name, _shard_receipt, digest
from book_rag.index_coverage import validate_coverage
from book_rag.library import Library
from book_rag.semantic_batches import KIND as PRODUCER_KIND
from book_rag.semantic_batches import batch_source_state
from book_rag.semantic_overlay import _encode_query, _snapshot, _space
from book_rag.semantic_pilot import ReadOnlyLibrary, _publish_json, _selected_rows, manifest_windows

KIND = "bookrag-explicit-book-semantic-batch-registry-v1"
MAX_BATCHES = 16
MAX_BOOKS = 8192
MAX_WINDOWS_PER_BATCH = 200_000
MAX_GENERATION_BYTES = 128 * 1024**2
WORKER_TIMEOUT = 15.0
WORKER_RSS = 512 * 1024**2
_SLOT = threading.BoundedSemaphore(1)
_WORKERS: set[subprocess.Popen[Any]] = set()
_LOCK = threading.RLock()
_HASH_CACHE: OrderedDict[tuple[Any, ...], str] = OrderedDict()
_HASH_CACHE_LIMIT = 256


def _file_identity(path: Path) -> tuple[Any, ...]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular nonsymlink registered artifact")
    info = path.stat()
    return str(path.absolute()), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def file_sha256(path: Path) -> str:
    """Cache bytes' digest, never a validity verdict; callers still compare SHA.

    Every hit rechecks inode/device/size/mtime/ctime and rejects missing/symlink
    files. Access time is intentionally absent because hashing itself changes it.
    A different expected receipt hash is still compared with this digest by each
    caller and cannot reuse a previous successful validation verdict.
    """
    before = _file_identity(path)
    with _LOCK:
        cached = _HASH_CACHE.get(before)
        if cached is not None:
            _HASH_CACHE.move_to_end(before)
            if _file_identity(path) != before:
                raise ValueError("Registered artifact changed during cached validation")
            return cached
    value = _file_sha256(path)
    if _file_identity(path) != before:
        raise ValueError("Registered artifact changed while hashing")
    with _LOCK:
        _HASH_CACHE[before] = value
        _HASH_CACHE.move_to_end(before)
        while len(_HASH_CACHE) > _HASH_CACHE_LIMIT:
            _HASH_CACHE.popitem(last=False)
    return value


def _read(path: Path, budget: int = 1024**2) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > budget:
        raise ValueError("Expected bounded regular batch metadata")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Batch metadata must be a JSON object")
    return value


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Expected metadata object: {name}")
    return value


def _source_guard(source: Library, manifest: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    expected = manifest["descriptor"]["source_state"]
    if full:
        actual = batch_source_state(source, manifest["descriptor"]["book_ids"])
        if actual != expected:
            raise ValueError("Direct batch source, provenance or original namespace changed")
        return actual
    # Registration performed the complete canonical scan. Runtime can reuse that
    # proof only while exact trigger DDL/schema/namespace/inode and every covered
    # epoch/header remain identical to the producer's SAME-snapshot state token.
    before = source.db_path.stat()
    with source.connect() as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN")

        class SnapshotSource:
            db_path = source.db_path

            def connect(self) -> Any:
                from contextlib import nullcontext

                return nullcontext(conn)

        state = validate_coverage(SnapshotSource(), expected["coverage"])
        if (
            not state["tracked"]
            or state["state_token"] != expected["state_token"]
            or str(source.data_dir) != expected["source_directory"]
            or before.st_dev != expected["database_device"]
            or before.st_ino != expected["database_inode"]
        ):
            raise ValueError("Direct batch canonical epochs, schema or original namespace changed")
        # These provenance/attribution tables are deliberately NOT covered by
        # embedding epochs. Compare their exact producer-format hashes/counts in
        # this same SQL snapshot; do not substitute timestamps or global versions.
        signature = expected["signature"]
        books = manifest["descriptor"]["book_ids"]
        if books != signature["book_ids"] or books != expected["coverage"]["covered_book_ids"]:
            raise ValueError("Direct batch metadata coverage differs")
        for table in ("books", "book_metadata", "index_versions", "section_roles"):
            checksum, count = hashlib.sha256(), 0
            for row in _selected_rows(conn, table, books):
                value = dict(row)
                if table == "books":
                    value = {
                        key: item
                        for key, item in value.items()
                        if key not in {"created_at", "indexed_at", "local_path", "error"}
                    }
                checksum.update(canonical_bytes(value) + b"\n")
                count += 1
            if checksum.hexdigest() != signature["table_sha256"][table] or count != signature["table_counts"][table]:
                raise ValueError("Direct batch edition, attribution or provenance changed")
    after = source.db_path.stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError("Direct batch database replaced during runtime proof")
    return expected


def _inspect_batch(directory: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if directory.is_symlink():
        raise ValueError("Batch directory cannot be a symlink")
    root = directory.expanduser().resolve(strict=True)
    report = _read(root / "report.json")
    native_report = _object(report.get("native"), "native")
    encoder_report = _object(report.get("encoder"), "encoder")
    native_result = _object(native_report.get("result"), "native.result")
    _object(report.get("manifest"), "report.manifest")
    if (
        report.get("kind") != "actual-complete-edition-semantic-batch"
        or report.get("status") != "complete"
        or report.get("source_unchanged") is not True
        or native_report.get("ok") is not True
        or native_report.get("torch_imported") is not False
        or native_result.get("complete") is not True
        or encoder_report.get("model_files_unchanged") is not True
        or encoder_report.get("faiss_imported") is not False
        or any(key in report for key in ("error", "resource_error", "encoder_close_error"))
    ):
        raise ValueError("Only a complete, closed and verified direct-live build may be registered")
    native = native_result
    original = Path(native["directory"])
    if original.is_symlink():
        raise ValueError("Batch generation cannot be a symlink")
    generation = original.resolve(strict=True)
    ready = _read(generation / "ready.json")
    descriptor = _read(generation / "descriptor.json")
    manifest = _read(root / "windows.sqlite3.receipt.json")
    producer = _object(manifest.get("descriptor"), "manifest.descriptor")
    state = _object(producer.get("source_state"), "source_state")
    _object(state.get("coverage"), "source_state.coverage")
    _object(state.get("signature"), "source_state.signature")
    _object(descriptor.get("source_coverage"), "source_coverage")
    _object(manifest.get("rows_by_book"), "rows_by_book")
    _object(descriptor.get("rows_by_book"), "descriptor.rows_by_book")
    _space(_object(descriptor.get("model_space"), "model_space"))
    identity = descriptor["identity"]
    if (
        generation != root / "compressed" / ("incremental-" + identity)
        or digest({key: value for key, value in descriptor.items() if key != "identity"}) != identity
        or native["identity"] != identity
        or ready["identity"] != identity
        or file_sha256(generation / "ready.json") != native["ready_sha256"]
        or file_sha256(generation / "descriptor.json") != ready["descriptor_sha256"]
        or producer["kind"] != PRODUCER_KIND
        or producer["book_ids"] != descriptor["covered_book_ids"]
        or producer["source_state"]["coverage"] != descriptor["source_coverage"]
        or producer["source_state"]["state_token"] != descriptor["build_source_token"]
        or producer["model_space"] != descriptor["model_space"]
        or report["encoder"].get("model_space") != descriptor["model_space"]
        or producer["window_recipe_sha256"] != descriptor["model_space"]["window_recipe_sha256"]
        or descriptor["model_space_sha256"] != digest(descriptor["model_space"])
        or ready["model_space_sha256"] != descriptor["model_space_sha256"]
        or manifest["source_database_copied"] is not False
        or manifest["embedding_vectors_stored"] is not False
        or ready.get("source_passage_coverage_verified") is not True
        or ready["source_passage_fingerprint"] != descriptor["source_coverage"]["passage_fingerprint"]
        or ready["input_chain"] != manifest["input_chain"]
        or descriptor["rows_by_book"] != manifest["rows_by_book"]
        or ready["rows"] != manifest["rows"]
        or descriptor["rows"] != manifest["rows"]
        or native["durable_rows"] != manifest["rows"]
        or not 1 <= manifest["rows"] <= MAX_WINDOWS_PER_BATCH
        or file_sha256(root / "windows.sqlite3") != manifest["database_sha256"]
        or file_sha256(generation / "codec.faiss") != descriptor["codec_sha256"]
        or report["codec_sha256"] != descriptor["codec_sha256"]
        or report["source"] != producer["source_state"]["source_directory"]
        or any(report["manifest"][key] != manifest[key] for key in report["manifest"])
    ):
        raise ValueError("Direct generation/producer/model/source receipts disagree")
    books = descriptor["covered_book_ids"]
    counts = manifest["rows_by_book"]
    if (
        not isinstance(books, list)
        or not 1 <= len(books) <= 2048
        or books != sorted(set(books))
        or set(counts) != set(books)
        or any(type(count) is not int or count < 1 for count in counts.values())
        or sum(counts.values()) != manifest["rows"]
    ):
        raise ValueError("Invalid complete per-edition window counts")
    ranges = [
        (start, min(start + descriptor["shard_rows"], descriptor["rows"]))
        for start in range(0, descriptor["rows"], descriptor["shard_rows"])
    ]
    if [(item["start"], item["end"]) for item in ready["shards"]] != ranges:
        raise ValueError("Ready batch omits or duplicates shard ranges")
    for item in ready["shards"]:
        name = _shard_name(item["start"], item["end"])
        receipt = _shard_receipt(generation, item["start"], item["end"])
        if receipt is None or file_sha256(generation / (name + ".receipt.json")) != item["receipt_sha256"]:
            raise ValueError("Immutable batch shard changed")
        binding = _read(generation / (name + ".binding.json"))
        if (
            file_sha256(generation / (name + ".binding.json")) != item["binding_sha256"]
            or binding["receipt_sha256"] != item["receipt_sha256"]
            or receipt["identity"] != identity
            or receipt["model_space_sha256"] != descriptor["model_space_sha256"]
            or receipt["coverage"]["covered_book_ids"] != item["covered_book_ids"]
        ):
            raise ValueError("Original live binding or shard identity differs")
    if sum(path.stat().st_size for path in generation.iterdir() if path.is_file()) > MAX_GENERATION_BYTES:
        raise ValueError("Batch generation exceeds the bounded registry budget")
    entry = {
        "batch_directory": str(root),
        "generation_directory": str(generation),
        "report_sha256": file_sha256(root / "report.json"),
        "ready_sha256": file_sha256(generation / "ready.json"),
        "descriptor_sha256": file_sha256(generation / "descriptor.json"),
        "manifest_receipt_sha256": file_sha256(root / "windows.sqlite3.receipt.json"),
        "manifest_sha256": manifest["database_sha256"],
        "model_space_sha256": descriptor["model_space_sha256"],
        "codec_sha256": descriptor["codec_sha256"],
        "source_state_sha256": digest(producer["source_state"]),
        "source_directory": producer["source_state"]["source_directory"],
        "covered_book_ids": books,
        "rows": manifest["rows"],
    }
    return entry, ready, descriptor, manifest


def _entry(
    registry: dict[str, Any], number: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    expected = registry["entries"][number]
    actual, ready, descriptor, manifest = _inspect_batch(Path(expected["batch_directory"]))
    if actual != expected:
        raise ValueError("Registered immutable batch identity changed")
    return actual, ready, descriptor, manifest


def _registry(path: Path) -> dict[str, Any]:
    value = _read(path, 4 * 1024**2)
    if value.get("kind") != KIND or value.get("schema_version") != 1:
        raise ValueError("Unknown semantic batch registry")
    entries, mapping = value["entries"], value["book_to_entry"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_BATCHES or not isinstance(mapping, dict):
        raise ValueError("Invalid bounded registry entries")
    expected = {}
    for number, entry in enumerate(entries):
        for identity in entry["covered_book_ids"]:
            if identity in expected:
                raise ValueError("An edition cannot route to multiple batches")
            expected[identity] = number
    if mapping != expected or not 1 <= len(mapping) <= MAX_BOOKS:
        raise ValueError("Registry routing differs from immutable batch coverage")
    return value


def _bindings(source: Library, entry: dict[str, Any], ready: dict[str, Any]) -> None:
    generation = Path(entry["generation_directory"])
    for item in ready["shards"]:
        name = _shard_name(item["start"], item["end"])
        receipt = _read(generation / (name + ".receipt.json"))
        binding = _read(generation / (name + ".binding.json"))
        if not validate_coverage(source, receipt["coverage"], binding)["valid"]:
            raise ValueError("Original live shard binding changed; registration never rebinds")


def _mapping_proof(source: Library, entry: dict[str, Any], ready: dict[str, Any], descriptor: dict[str, Any]) -> None:
    records = iter(manifest_windows(Path(entry["batch_directory"]) / "windows.sqlite3"))
    count = 0
    previous = 0
    with source.connect() as conn:
        expected = iter(
            conn.execute(
                "SELECT p.id,p.book_id FROM books b CROSS JOIN passages p ON p.book_id=b.id "
                "JOIN sections s ON s.id=p.section_id AND s.book_id=p.book_id "
                "WHERE b.id IN (SELECT value FROM json_each(?)) AND b.status='indexed' ORDER BY p.id",
                (json.dumps(entry["covered_book_ids"]),),
            )
        )
        for item in ready["shards"]:
            receipt = _read(
                Path(entry["generation_directory"]) / (_shard_name(item["start"], item["end"]) + ".receipt.json")
            )
            passages, books = _shard_mapping(Path(entry["generation_directory"]), receipt)
            for passage, book in zip(passages, books, strict=True):
                record = next(records)
                if (
                    record.window_id != count
                    or record.passage_id != int(passage)
                    or not 0 <= int(book) < len(entry["covered_book_ids"])
                    or record.book_id != entry["covered_book_ids"][int(book)]
                ):
                    raise ValueError("Window producer and native mapping disagree")
                if record.passage_id != previous:
                    current = next(expected)
                    if record.passage_id <= previous or (current[0], current[1]) != (record.passage_id, record.book_id):
                        raise ValueError("Manifest omits, reorders or misowns canonical passages")
                    previous = record.passage_id
                count += 1
        if next(records, None) is not None or next(expected, None) is not None or count != descriptor["rows"]:
            raise ValueError("Manifest does not completely cover selected canonical passages")


def install_batch_registry(library: Library, batch_directories: list[Path], destination: Path) -> dict[str, Any]:
    """Explicit immutable registry installation; no source/generation mutation."""
    if not isinstance(batch_directories, list) or not 1 <= len(batch_directories) <= MAX_BATCHES:
        raise ValueError("Select a bounded explicit batch list")
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Use a new immutable registry destination")
    if destination.resolve().is_relative_to(library.data_dir.resolve()):
        raise ValueError("Keep registry metadata outside the original library")
    entries = []
    for directory in batch_directories:
        entry, _, _, _ = _inspect_batch(directory)
        if destination.resolve().is_relative_to(Path(entry["batch_directory"])):
            raise ValueError("Keep registry outside immutable batch directories")
        entries.append(entry)
    mapping = {}
    for number, entry in enumerate(entries):
        for identity in entry["covered_book_ids"]:
            if identity in mapping:
                raise ValueError("Duplicate edition across registry entries")
            mapping[identity] = number
    if len(mapping) > MAX_BOOKS:
        raise ValueError("Registry edition budget exceeded")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".owned-staging-" + uuid.uuid4().hex)
    staging.mkdir()
    try:
        registry = {
            "kind": KIND,
            "schema_version": 1,
            "target_db_path": str(library.db_path.resolve()),
            "entries": entries,
            "book_to_entry": mapping,
            "source_bindings_rewritten": False,
        }
        for number in range(len(entries)):
            entry, ready, descriptor, manifest = _entry(registry, number)
            with _snapshot(library) as coherent:
                _source_guard(coherent, manifest, full=True)
                _bindings(coherent, entry, ready)
                _mapping_proof(coherent, entry, ready, descriptor)
        _publish_json(staging / "registry.json", registry)
        registry_sha = file_sha256(staging / "registry.json")
        for number in range(len(entries)):
            _run_worker(
                {
                    "operation": "validate",
                    "registry": str(staging / "registry.json"),
                    "registry_sha256": registry_sha,
                    "entry": number,
                }
            )
        with _snapshot(library, installing=True) as coherent:
            for number in range(len(entries)):
                entry, ready, _, manifest = _entry(registry, number)
                _source_guard(coherent, manifest, full=True)
                _bindings(coherent, entry, ready)
            if file_sha256(staging / "registry.json") != registry_sha:
                raise ValueError("Registry changed before publication")
            staging.rename(destination)
            fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return registry
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def close_batch_workers() -> None:
    with _LOCK:
        processes = list(_WORKERS)
    for process in processes:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


atexit.register(close_batch_workers)


def _run_worker(request: dict[str, Any]) -> dict[str, Any]:
    if not _SLOT.acquire(timeout=1):
        raise RuntimeError("Batch native worker is busy")
    process = None
    try:
        payload = json.dumps(request, allow_nan=False).encode()
        if len(payload) > 65536:
            raise ValueError("Bounded native request exceeded")
        env = os.environ.copy()
        env["BOOKRAG_IVFPQ_WORKER"] = "1"
        for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            env[key] = "2"
        process = subprocess.Popen(
            [sys.executable, "-m", "book_rag.semantic_batch_overlay", "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        with _LOCK:
            _WORKERS.add(process)
        started, supplied = time.monotonic(), False
        while True:
            try:
                stdout, _ = process.communicate(input=None if supplied else payload, timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                supplied = True
                if time.monotonic() - started > WORKER_TIMEOUT:
                    raise TimeoutError("Batch native query timed out") from None
                rss = subprocess.run(
                    ["ps", "-o", "rss=", "-p", str(process.pid)], capture_output=True, text=True, check=False, timeout=2
                )
                if int(rss.stdout.strip() or "0") * 1024 > WORKER_RSS:
                    raise RuntimeError("Batch native worker exceeded memory budget") from None
        if process.returncode or len(stdout) > 1024**2:
            raise RuntimeError("Batch native worker failed or exceeded response budget")
        answer = json.loads(stdout)
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            raise RuntimeError("Batch native worker rejected its index/source")
        return answer["result"]
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate()
            with _LOCK:
                _WORKERS.discard(process)
        _SLOT.release()


def batch_overlay_search(
    library: Library, query: str, mode: str, book_id: str | None, language: str | None, limit: int
) -> dict[str, Any] | None:
    configured = os.getenv("BOOKRAG_SEMANTIC_BATCH_REGISTRY")
    if not configured or book_id is None or mode not in ("semantic", "hybrid"):
        return None
    path = Path(configured).expanduser()
    try:
        raw = _read(path, 4 * 1024**2)
        if raw.get("target_db_path") != str(library.db_path.resolve()) or book_id not in raw["book_to_entry"]:
            return None
    except (OSError, ValueError, TypeError, KeyError):
        return None
    coverage: dict[str, Any] = {}
    started = time.perf_counter()

    def fallback() -> dict[str, Any]:
        response = library.search(query, "lexical", book_id, language, limit)
        response["warnings"].append(
            "Дополнительный поиск в выбранной книге недоступен или изменился; показан поиск по словам."
        )
        if coverage:
            response["semantic_coverage"] = coverage | {"active": False}
        return response

    try:
        registry_sha = file_sha256(path)
        registry = _registry(path)
        number = registry["book_to_entry"][book_id]
        entry, ready, descriptor, manifest = _entry(registry, number)
        with _snapshot(library) as coherent:
            before = _source_guard(coherent, manifest)
            _bindings(coherent, entry, ready)
            row = coherent.connection.execute(
                "SELECT language FROM books WHERE id=? AND status='indexed'", (book_id,)
            ).fetchone()
            if row is None or (language is not None and row[0] != language):
                return None
            total = coherent.connection.execute("SELECT count(*) FROM books WHERE status='indexed'").fetchone()[0]
            source_language = row[0]
        coverage = {
            "indexed_books": len(entry["covered_book_ids"]),
            "searchable_books": total,
            "active": True,
            "requested_book_covered": True,
            "scope": "book-batch-overlay",
            "scope_book_id": book_id,
            "scope_language": source_language,
            "scope_searchable_books": 1,
        }
        vector = _encode_query(library, query, descriptor["model_space"])
        native = _run_worker(
            {
                "operation": "query",
                "registry": str(path.resolve()),
                "registry_sha256": registry_sha,
                "entry": number,
                "vector": vector.tolist(),
                "book_id": book_id,
                "language": language,
            }
        )
        if native.get("fallback_required") or native.get("source_changed"):
            return fallback()
        hits = native["results"]
        if not isinstance(hits, list) or len(hits) > 100 or len({item["passage_id"] for item in hits}) != len(hits):
            raise ValueError("Invalid fixed native passage pool")
        from book_rag.retrieval import prepare_query, rerank_results

        plan = prepare_query(query)
        with _snapshot(library) as coherent:
            if _source_guard(coherent, manifest) != before:
                raise ValueError("Source changed before hydration")
            found = {item["id"]: item for item in coherent.passages([hit["passage_id"] for hit in hits])}
            vectors: list[dict[str, Any]] = []
            for hit in hits:
                passage = found.get(hit["passage_id"])
                score = float(hit["score"])
                if (
                    passage is None
                    or passage.get("verified") is not True
                    or passage["book_id"] != book_id
                    or hit["book_id"] != book_id
                    or (language is not None and passage["language"] != language)
                    or not np.isfinite(score)
                ):
                    raise ValueError("Native evidence failed live source or scope verification")
                if plan.phrases and not rerank_results(plan, [passage], limit=1):
                    continue
                vectors.append(passage | {"score": score, "semantic_score": score, "semantic_score_approximate": True})
            results = vectors
            warnings = []
            if mode == "hybrid":
                lexical = coherent.search(query, "lexical", book_id, language, 50)
                warnings.extend(lexical["warnings"])
                merged: dict[int, dict[str, Any]] = {}
                scores: dict[int, float] = {}
                for ranking in (lexical["results"], vectors):
                    for rank, passage in enumerate(ranking, 1):
                        merged.setdefault(passage["id"], passage)
                        scores[passage["id"]] = scores.get(passage["id"], 0) + 1 / (60 + rank)
                results = [
                    merged[identity] | {"score": score}
                    for identity, score in sorted(scores.items(), key=lambda item: -item[1])
                ]
            _source_guard(coherent, manifest)
        with _snapshot(library) as coherent:
            if _source_guard(coherent, manifest) != before:
                raise ValueError("Source changed during hydration")
        if _registry(path) != registry or file_sha256(path) != registry_sha:
            raise ValueError("Registry changed during query")
        _entry(registry, number)
        warnings.append(
            "Смысловой поиск включён для выбранной книги из явно зарегистрированного пакета; "
            "приближённая близость фрагментов не доказывает наличие ответа."
        )
        return {
            "query": query,
            "mode": mode,
            "results": results[:limit],
            "warnings": list(dict.fromkeys(warnings)),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "model": descriptor["model_space"]["model"],
            "retrieval_strategy": "explicit-book-batch-IVFPQ-RRF-v1",
            "semantic_coverage": coverage,
            "candidate_pool": {
                "passage_limit": 100,
                "unique_passages": len(hits),
                "verified_passages": len(vectors),
                "lexical_limit": 50 if mode == "hybrid" else 0,
            },
        }
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        ImportError,
        MemoryError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ):
        return fallback()


def _native(request: dict[str, Any]) -> dict[str, Any]:
    if "torch" in sys.modules or "book_rag.semantic" in sys.modules:
        raise RuntimeError("Native batch worker cannot import Torch or semantic model code")
    path = Path(request["registry"])
    if file_sha256(path) != request["registry_sha256"]:
        raise ValueError("Registry bytes changed before native work")
    registry = _registry(path)
    entry, ready, descriptor, manifest = _entry(registry, request["entry"])
    source = ReadOnlyLibrary(Path(registry["target_db_path"]).parent)
    with _snapshot(source) as coherent:
        before = _source_guard(coherent, manifest)
        _bindings(coherent, entry, ready)
    if request["operation"] == "validate":
        from book_rag.experimental_ivfpq import faiss_backend, read_verified_index

        backend = faiss_backend()
        directory = Path(entry["generation_directory"])
        codec = read_verified_index(directory / "codec.faiss", descriptor["codec_sha256"])
        for item in ready["shards"]:
            receipt = _read(directory / (_shard_name(item["start"], item["end"]) + ".receipt.json"))
            index = read_verified_index(
                directory / (_shard_name(item["start"], item["end"]) + ".faiss"), receipt["index_sha256"]
            )
            if (
                not isinstance(index, backend.IndexIVFPQ)
                or index.d != 384
                or index.ntotal != item["end"] - item["start"]
            ):
                raise ValueError("Native batch index shape/type differs")
            empty = backend.clone_index(index)
            empty.reset()
            if not np.array_equal(backend.serialize_index(empty), backend.serialize_index(codec)):
                raise ValueError("Native batch uses another metric/codebook")
            seen = set()
            for cell in range(index.nlist):
                count = index.invlists.list_size(cell)
                if count:
                    ids = backend.rev_swig_ptr(index.invlists.get_ids(cell), count)
                    if any(
                        int(identity) in seen or not item["start"] <= int(identity) < item["end"] for identity in ids
                    ):
                        raise ValueError("Native batch window IDs differ")
                    seen.update(map(int, ids))
            if len(seen) != index.ntotal:
                raise ValueError("Native batch omits window IDs")
        result = {"validated": True, "torch_imported": False}
    elif request["operation"] == "query":
        from book_rag.incremental_ivfpq import query_incremental

        if request.get("book_id") not in entry["covered_book_ids"]:
            raise ValueError("Native query must name one covered edition")
        result = query_incremental(
            source,
            Path(entry["generation_directory"]),
            np.asarray(request["vector"], dtype=np.float32),
            descriptor["model_space"],
            book_id=request["book_id"],
            language=request.get("language"),
            limit=100,
            nprobe=min(32, descriptor["nlist"]),
        )
    else:
        raise ValueError("Unknown native batch operation")
    with _snapshot(source) as coherent:
        if _source_guard(coherent, manifest) != before:
            raise ValueError("Original live batch source changed in native worker")
    if file_sha256(path) != request["registry_sha256"] or "torch" in sys.modules or "book_rag.semantic" in sys.modules:
        raise ValueError("Native worker source/configuration/isolation changed")
    return result


if __name__ == "__main__":
    try:
        if sys.argv[1:] != ["--worker"]:
            raise ValueError("Only the bounded private native worker is supported")
        payload = sys.stdin.buffer.read(65537)
        if len(payload) > 65536:
            raise ValueError("Native request exceeds its byte budget")
        response = {"ok": True, "result": _native(json.loads(payload))}
    except BaseException as error:
        response = {"ok": False, "error": type(error).__name__}
    print(json.dumps(response, allow_nan=False))
