"""Opt-in scoped, source-bound compressed semantics; native FAISS stays in a child.

The primary global index is untouched. Installation is explicit and immutable;
changed covered source never causes automatic rebinding.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from book_rag.embedding_profiles import MINILM_MODEL, MINILM_PROFILE, MINILM_REVISION
from book_rag.experimental_ivfpq import file_sha256
from book_rag.incremental_ivfpq import _shard_mapping, _shard_name, _shard_receipt, digest
from book_rag.index_coverage import bind_coverage, capture_coverage, validate_coverage
from book_rag.library import Library
from book_rag.semantic_pilot import ReadOnlyLibrary, _publish_json, manifest_windows, selected_signature

WORKER_TIMEOUT = 15.0
WORKER_RSS = 512 * 1024**2
_WORKERS: set[subprocess.Popen[Any]] = set()
_WORKER_LOCK = threading.RLock()
_SLOT = threading.BoundedSemaphore(1)
_ENCODER_LOCK = threading.Lock()


class InstallationTarget(ReadOnlyLibrary):
    """Existing target with a writer-reservation lock, but no permitted SQL writes.

    query_only forbids BEGIN IMMEDIATE. A mode=rw connection plus authorizer
    allows that lock while rejecting every DML/DDL statement and write PRAGMA.
    This constructor inherits the migration-free read-only adapter initializer.
    """

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        allowed = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_TRANSACTION,
            sqlite3.SQLITE_SAVEPOINT,
            sqlite3.SQLITE_RECURSIVE,
        }

        def authorize(
            action: int, first: str | None, second: str | None, database: str | None, trigger: str | None
        ) -> int:
            if action in allowed or (action == sqlite3.SQLITE_PRAGMA and first == "schema_version" and second is None):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(authorize)
        try:
            yield conn
        finally:
            conn.rollback()
            conn.close()


class _SnapshotLibrary(Library):
    def __init__(self, library: Library, connection: sqlite3.Connection) -> None:
        self.data_dir, self.db_path, self.connection = library.data_dir, library.db_path, connection

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        yield self.connection


@contextmanager
def _snapshot(library: Library, *, installing: bool = False) -> Generator[_SnapshotLibrary]:
    with library.connect() as conn:
        owns = not conn.in_transaction
        if owns:
            conn.execute("BEGIN IMMEDIATE" if installing else "BEGIN")
        try:
            yield _SnapshotLibrary(library, conn)
        finally:
            if owns:
                conn.rollback()


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024**2:
        raise ValueError("Expected bounded regular overlay metadata")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Overlay metadata must be an object")
    return value


def _space(value: dict[str, Any]) -> None:
    if (
        value.get("model") != MINILM_MODEL
        or value.get("revision") != MINILM_REVISION
        or value.get("dimensions") != 384
        or value.get("profile") != MINILM_PROFILE.metadata(MINILM_REVISION)
        or value.get("strategy") != "token-windows-v2"
    ):
        raise ValueError("Overlay requires the exact pinned MiniLM model/profile space")
    hashes = value.get("model_files_sha256")
    if (
        not isinstance(hashes, list)
        or not hashes
        or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in hashes)
    ):
        raise ValueError("Overlay model space must pin acquired model-file SHAs")


def _generation(root: Path) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    root = root.resolve(strict=True)
    report = _read(root / "report.json")
    original_path = Path(report["resumed_phase"]["result"]["directory"])
    if original_path.is_symlink():
        raise ValueError("Pilot generation cannot be a symlink")
    directory = original_path.resolve(strict=True)
    if not directory.is_relative_to(root):
        raise ValueError("Pilot generation must remain inside its owned pilot directory")
    ready = _read(directory / "ready.json")
    descriptor = _read(directory / "descriptor.json")
    snapshot = _read(root / "canonical" / "snapshot.json")
    _space(descriptor["model_space"])
    if (
        file_sha256(directory / "ready.json") != report["resumed_phase"]["result"]["ready_sha256"]
        or file_sha256(directory / "descriptor.json") != ready["descriptor_sha256"]
        or ready["identity"] != descriptor["identity"]
        or ready["source_passage_coverage_verified"] is not True
        or ready["source_passage_fingerprint"] != descriptor["source_coverage"]["passage_fingerprint"]
        or descriptor["source_coverage"] != snapshot["source_coverage"]
        or descriptor["covered_book_ids"] != descriptor["source_coverage"]["covered_book_ids"]
        or descriptor["covered_book_ids"] != snapshot["book_ids"]
        or descriptor["model_space_sha256"] != digest(descriptor["model_space"])
        or ready["model_space_sha256"] != descriptor["model_space_sha256"]
        or digest({key: value for key, value in descriptor.items() if key != "identity"}) != descriptor["identity"]
        or file_sha256(root / "canonical" / "library.sqlite3") != snapshot["snapshot_db_sha256"]
        or not 1 <= len(descriptor["covered_book_ids"]) <= 25
        or not 1 <= descriptor["rows"] <= 50000
    ):
        raise ValueError("Pilot has no consistent sealed ready/source/model identity")
    return directory, ready, descriptor, snapshot


def _linked(source: Path, target: Path, expected: str, *, copy_only: bool = False) -> None:
    if source.stat().st_size > 64 * 1024**2:
        raise ValueError("Pilot artifact exceeds bounded64MiB file size")
    if file_sha256(source) != expected:
        raise ValueError("Pilot artifact checksum differs before linking")
    try:
        if copy_only:
            raise OSError("Small shared control is copied to preserve its link metadata")
        os.link(source, target)
    except OSError:
        with source.open("rb") as original, target.open("xb") as copied:
            shutil.copyfileobj(original, copied)
            copied.flush()
            os.fsync(copied.fileno())
    if file_sha256(target) != expected or file_sha256(source) != expected:
        raise ValueError("Pilot artifact changed during overlay publication")


def _guard(snapshot: _SnapshotLibrary, installation: dict[str, Any]) -> dict[str, Any]:
    state = validate_coverage(snapshot, installation["coverage"], installation["coverage_binding"])
    if not state["valid"]:
        raise ValueError("Covered canonical source or epoch binding changed")
    if (
        selected_signature(snapshot.connection, installation["coverage"]["covered_book_ids"])
        != installation["selected_signature"]
    ):
        raise ValueError("Covered edition, attribution or provenance changed")
    return dict(state)


def _installation(path: Path) -> tuple[dict[str, Any], Path]:
    value = _read(path)
    root = path.resolve().parent
    if value.get("schema_version") != 1 or value.get("kind") != "ru21-scoped-semantic-overlay":
        raise ValueError("Unknown overlay installation")
    generation = root / "generation"
    if generation.is_symlink() or not generation.is_dir():
        raise ValueError("Invalid overlay generation directory")
    if file_sha256(generation / "ready.json") != value["ready_sha256"]:
        raise ValueError("Overlay ready metadata changed")
    proof = _read(root / "proof.json")
    if file_sha256(root / "proof.json") != value["proof_sha256"]:
        raise ValueError("Overlay installation proof changed")
    if value["coverage_binding"]["receipt_sha256"] != value["proof_sha256"]:
        raise ValueError("Overlay binding refers to another proof")
    if any(value[key] != proof[key] for key in ("coverage", "selected_signature", "model_space", "target_db_path")):
        raise ValueError("Overlay proof and installation metadata differ")
    _space(value["model_space"])
    return value, generation


def prepare_overlay(library: Library, pilot_directory: Path, destination: Path) -> dict[str, Any]:
    """Explicitly install an immutable ready pilot against the actual target DB.

    No model is loaded, source schema is never migrated, and no active pointer or
    environment variable changes. Existing installations are not overwritten.
    """
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Choose a new owned overlay destination; automatic rebinding is forbidden")
    source, ready, descriptor, snapshot = _generation(pilot_directory)
    if destination.resolve().is_relative_to(pilot_directory.resolve()) or destination.resolve().is_relative_to(
        (library.data_dir / "vectors").resolve()
    ):
        raise ValueError("Overlay destination must be outside the pilot and primary vector storage")
    if sum(path.stat().st_size for path in source.iterdir() if path.is_file()) > 128 * 1024**2:
        raise ValueError("Ready pilot exceeds the128MiB installation payload budget")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".overlay-staging-" + uuid.uuid4().hex)
    staging.mkdir()
    generation = staging / "generation"
    generation.mkdir()
    try:
        # Full input chain and exact window->passage/book assignments must agree
        # with the immutable mapping stream before any live binding is created.
        expected_ranges = [
            (start, min(start + descriptor["shard_rows"], descriptor["rows"]))
            for start in range(0, descriptor["rows"], descriptor["shard_rows"])
        ]
        if [(item["start"], item["end"]) for item in ready["shards"]] != expected_ranges:
            raise ValueError("Ready pilot shard order or boundaries differ")
        windows = iter(manifest_windows(pilot_directory.resolve() / "windows.sqlite3"))
        manifest = _read(pilot_directory.resolve() / "windows.sqlite3.receipt.json")
        if (
            manifest["descriptor"]["model_space"] != descriptor["model_space"]
            or manifest["descriptor"]["source_coverage"] != descriptor["source_coverage"]
            or manifest["input_chain"] != ready["input_chain"]
            or manifest["descriptor"]["window_recipe_sha256"] != descriptor["model_space"]["window_recipe_sha256"]
        ):
            raise ValueError("Window producer receipt differs from the ready source/model")
        count, chain = 0, "0" * 64
        shard_records = []
        for item in ready["shards"]:
            name = _shard_name(item["start"], item["end"])
            receipt = _shard_receipt(source, item["start"], item["end"])
            if receipt is None or file_sha256(source / (name + ".receipt.json")) != item["receipt_sha256"]:
                raise ValueError("Pilot shard receipt changed")
            if (
                receipt["identity"] != descriptor["identity"]
                or receipt["model_space_sha256"] != descriptor["model_space_sha256"]
            ):
                raise ValueError("Pilot shard belongs to another source/model")
            passages, books = _shard_mapping(source, receipt)
            for passage, book in zip(passages, books, strict=True):
                window = next(windows)
                if (
                    window.window_id != count
                    or window.passage_id != int(passage)
                    or window.book_id != descriptor["covered_book_ids"][int(book)]
                ):
                    raise ValueError("Native mapping differs from exact input manifest")
                chain = hashlib.sha256(bytes.fromhex(chain) + window.key()).hexdigest()
                count += 1
            _linked(source / (name + ".faiss"), generation / (name + ".faiss"), receipt["index_sha256"])
            _linked(source / (name + ".mapping.npz"), generation / (name + ".mapping.npz"), receipt["mapping_sha256"])
            _linked(source / (name + ".receipt.json"), generation / (name + ".receipt.json"), item["receipt_sha256"])
            for chunk in receipt["chunks"]:
                filename = f"chunk-{chunk['number']:08d}.json"
                if not (generation / filename).exists():
                    _linked(source / filename, generation / filename, chunk["receipt_sha256"])
            shard_records.append((item, receipt))
        if next(windows, None) is not None or count != ready["rows"] or chain != ready["input_chain"]:
            raise ValueError("Ready pilot lacks the complete exact input stream")
        _linked(source / "descriptor.json", generation / "descriptor.json", ready["descriptor_sha256"])
        _linked(source / "codec.faiss", generation / "codec.faiss", descriptor["codec_sha256"], copy_only=True)
        proof = {
            "source_ready_sha256": file_sha256(source / "ready.json"),
            "coverage": descriptor["source_coverage"],
            "selected_signature": snapshot["source_signature"],
            "model_space": descriptor["model_space"],
            "target_db_path": str(library.db_path.resolve()),
        }
        _publish_json(staging / "proof.json", proof)
        proof_sha = file_sha256(staging / "proof.json")
        installed_ready = dict(ready)
        installed_shards = []
        with _snapshot(library, installing=True) as coherent:
            languages = coherent.connection.execute(
                "SELECT language,status FROM books WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(descriptor["covered_book_ids"]),),
            ).fetchall()
            if len(languages) != len(descriptor["covered_book_ids"]) or any(
                row[0] != "ru" or row[1] != "indexed" for row in languages
            ):
                raise ValueError("Overlay coverage must contain indexed Russian editions only")
            if selected_signature(coherent.connection, descriptor["covered_book_ids"]) != snapshot["source_signature"]:
                raise ValueError("Live edition/provenance differs from the measured pilot")
            if capture_coverage(coherent.connection, descriptor["covered_book_ids"]) != descriptor["source_coverage"]:
                raise ValueError("Live canonical source differs from pilot coverage")
            overall = bind_coverage(coherent, descriptor["source_coverage"], receipt_sha256=proof_sha)
            for item, receipt in shard_records:
                binding = bind_coverage(coherent, receipt["coverage"], receipt_sha256=item["receipt_sha256"])
                name = _shard_name(item["start"], item["end"])
                path = generation / (name + ".binding.json")
                _publish_json(path, binding)
                installed_shards.append(item | {"binding_sha256": file_sha256(path)})
        installed_ready["shards"] = installed_shards
        _publish_json(generation / "ready.json", installed_ready)
        installation = proof | {
            "schema_version": 1,
            "kind": "ru21-scoped-semantic-overlay",
            "coverage_binding": overall,
            "proof_sha256": proof_sha,
            "ready_sha256": file_sha256(generation / "ready.json"),
            "input_chain": ready["input_chain"],
            "rows": ready["rows"],
            "production_global_index_changed": False,
        }
        _publish_json(staging / "installation.json", installation)
        with _snapshot(library) as coherent:
            _guard(coherent, installation)
        # Native validation happens only in the isolated subprocess.
        _run_worker(
            {
                "operation": "validate",
                "installation": str(staging / "installation.json"),
                "installation_sha256": file_sha256(staging / "installation.json"),
            }
        )
        with _snapshot(library, installing=True) as coherent:
            _guard(coherent, installation)
            staging.rename(destination)
            descriptor_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor_fd)
            finally:
                os.close(descriptor_fd)
        return installation
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def close_overlay_workers() -> None:
    with _WORKER_LOCK:
        processes = list(_WORKERS)
    for process in processes:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


atexit.register(close_overlay_workers)


def _run_worker(request: dict[str, Any]) -> dict[str, Any]:
    """Bounded fresh JSON subprocess; no pickle and no inherited native state."""
    if not _SLOT.acquire(timeout=1):
        raise RuntimeError("Compressed semantic worker is busy")
    process = None
    try:
        payload = json.dumps(request, allow_nan=False).encode()
        if len(payload) > 65536:
            raise ValueError("Overlay worker request exceeds64KiB")
        env = os.environ.copy()
        env["BOOKRAG_IVFPQ_WORKER"] = "1"
        for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            env[name] = "2"
        process = subprocess.Popen(
            [sys.executable, "-m", "book_rag.semantic_overlay", "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        with _WORKER_LOCK:
            _WORKERS.add(process)
        began = time.monotonic()
        supplied = False
        while True:
            try:
                stdout, _ = process.communicate(input=None if supplied else payload, timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                supplied = True
                if time.monotonic() - began > WORKER_TIMEOUT:
                    raise TimeoutError("Compressed semantic worker exceeded15s") from None
                rss = subprocess.run(
                    ["ps", "-o", "rss=", "-p", str(process.pid)], capture_output=True, text=True, check=False
                )
                if int(rss.stdout.strip() or "0") * 1024 > WORKER_RSS:
                    raise RuntimeError("Compressed semantic worker exceeded512MiB RSS") from None
        if process.returncode or len(stdout) > 1024**2:
            raise RuntimeError("Compressed semantic worker exited or exceeded response budget")
        response = json.loads(stdout)
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise RuntimeError("Compressed semantic worker rejected its source/index")
        return response["result"]
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate()
            with _WORKER_LOCK:
                _WORKERS.discard(process)
        _SLOT.release()


def _model_file_state(files: list[Path]) -> tuple[tuple[Any, ...], ...]:
    result = []
    for path in files:
        info = path.stat()
        result.append((str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return tuple(result)


@lru_cache(maxsize=2)
def _model_hashes(state: tuple[tuple[Any, ...], ...]) -> tuple[str, ...]:
    return tuple(sorted(file_sha256(Path(row[0])) for row in state))


def _encode_query(library: Library, query: str, space: dict[str, Any]) -> np.ndarray:
    # Import only in the normal parent; the native worker never reaches here.
    from book_rag.encoder_ipc import model_snapshot
    from book_rag.semantic import _model, _model_cache

    _space(space)
    cache = _model_cache(library)
    snapshot, files = model_snapshot(cache)
    before = _model_file_state(files)
    if list(_model_hashes(before)) != sorted(space["model_files_sha256"]):
        raise ValueError("Cached model bytes differ from the acquired overlay model space")
    if not _ENCODER_LOCK.acquire(timeout=1):
        raise RuntimeError("Scoped semantic encoder is busy")
    try:
        model = _model(str(snapshot), str(cache), space["revision"])
        if model.max_seq_length != 128:
            raise ValueError("Loaded MiniLM query budget differs from the indexed profile")
        vector = np.asarray(model.encode([query], normalize_embeddings=True, convert_to_numpy=True), dtype=np.float32)
    finally:
        _ENCODER_LOCK.release()
    if _model_file_state(files) != before:
        raise ValueError("Cached model files changed during encoding")
    if (
        vector.shape != (1, 384)
        or not np.isfinite(vector).all()
        or np.max(np.abs(np.linalg.norm(vector, axis=1) - 1)) > 0.001
    ):
        raise ValueError("Invalid normalized MiniLM query vector")
    return vector


def overlay_search(
    library: Library, query: str, mode: str, book_id: str | None, language: str | None, limit: int
) -> dict[str, Any] | None:
    """Return None outside the opt-in scoped target; never change global search."""
    configured = os.getenv("BOOKRAG_SEMANTIC_OVERLAY")
    if not configured or mode not in ("semantic", "hybrid") or (book_id is None and language != "ru"):
        return None
    path = Path(configured).expanduser()
    try:
        raw = _read(path)
        if raw.get("target_db_path") != str(library.db_path.resolve()):
            return None
        identities = raw["coverage"]["covered_book_ids"]
        if (book_id is not None and book_id not in identities) or language not in (None, "ru"):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    began = time.perf_counter()
    coverage: dict[str, Any] = {}

    def fallback() -> dict[str, Any]:
        value = library.search(query, "lexical", book_id, language, limit)
        value["warnings"].append("Дополнительный смысловой индекс недоступен или изменился; показан поиск по словам.")
        if coverage:
            value["semantic_coverage"] = coverage | {"active": False}
        return value

    try:
        installation, _ = _installation(path)
        installation_sha = file_sha256(path)
        with _snapshot(library) as coherent:
            conn = coherent.connection
            count = conn.execute("SELECT count(*) FROM books WHERE status='indexed'").fetchone()[0]
            russian = conn.execute("SELECT count(*) FROM books WHERE status='indexed' AND language='ru'").fetchone()[0]
            coverage = {
                "indexed_books": installation["coverage"]["books"],
                "searchable_books": count,
                "active": True,
                "requested_book_covered": book_id in identities if book_id else None,
                "scope": "ru21-scoped-overlay",
                "scope_language": "ru",
                "scope_searchable_books": russian,
            }
            before = _guard(coherent, installation)
        vector = _encode_query(library, query, installation["model_space"])
        response = _run_worker(
            {
                "operation": "query",
                "installation": str(path.resolve()),
                "installation_sha256": installation_sha,
                "vector": vector.tolist(),
                "book_id": book_id,
                "language": language,
                "limit": 100,
            }
        )
        if response.get("fallback_required") or response.get("source_changed"):
            return fallback()
        candidates = response["results"]
        if not isinstance(candidates, list) or len(candidates) > 100:
            raise ValueError("Invalid compressed candidate pool")
        candidate_ids = [hit["passage_id"] for hit in candidates]
        if any(type(identity) is not int or identity < 1 for identity in candidate_ids) or len(
            set(candidate_ids)
        ) != len(candidate_ids):
            raise ValueError("Compressed passage IDs must be positive and unique")
        from book_rag.retrieval import prepare_query, rerank_results

        plan = prepare_query(query)
        with _snapshot(library) as coherent:
            hydrate_state = _guard(coherent, installation)
            if hydrate_state["state_token"] != before["state_token"]:
                raise ValueError("Covered source changed before evidence hydration")
            found = {item["id"]: item for item in coherent.passages(candidate_ids)}
            vectors = []
            for hit in candidates:
                item = found.get(hit["passage_id"])
                score = float(hit["score"])
                if (
                    item is None
                    or item.get("verified") is not True
                    or item["book_id"] != hit["book_id"]
                    or item["book_id"] not in identities
                    or item["language"] != "ru"
                    or book_id is not None
                    and item["book_id"] != book_id
                    or not np.isfinite(score)
                ):
                    raise ValueError("Compressed candidate failed live scope/source verification")
                if plan.phrases and not rerank_results(plan, [item], limit=1):
                    continue
                vectors.append(item | {"score": score, "semantic_score": score, "semantic_score_approximate": True})
            results = vectors
            if mode == "hybrid":
                lexical = coherent.search(query, "lexical", book_id, language, 50)["results"]
                merged, scores = {}, {}
                for ranking in (lexical, vectors):
                    for rank, item in enumerate(ranking, 1):
                        merged.setdefault(item["id"], item)
                        scores[item["id"]] = scores.get(item["id"], 0) + 1 / (60 + rank)
                results = [
                    merged[key] | {"score": score} for key, score in sorted(scores.items(), key=lambda pair: -pair[1])
                ]
            _guard(coherent, installation)
        # A fresh transaction catches writers that committed during the coherent
        # evidence snapshot; its own repeated reads cannot observe newer WAL data.
        with _snapshot(library) as coherent:
            if _guard(coherent, installation)["state_token"] != before["state_token"]:
                raise ValueError("Covered source changed during evidence hydration")
        if _installation(path)[0] != installation or file_sha256(path) != installation_sha:
            raise ValueError("Overlay installation changed during query")
        return {
            "query": query,
            "mode": mode,
            "results": results[:limit],
            "warnings": [
                f"Дополнительный приближённый смысловой поиск охватывает {len(identities)} из {count} изданий "
                f"и включён только для выбранной книги или русского языка ({len(identities)}/{russian}). "
                "Оценка близости не доказывает наличие ответа; поиск по словам сохраняет выбранную область."
            ],
            "elapsed_ms": round((time.perf_counter() - began) * 1000, 2),
            "model": MINILM_MODEL,
            "retrieval_strategy": "scoped-IVFPQ-approximate-RRF-v1",
            "semantic_coverage": coverage,
            "candidate_pool": {
                "passage_limit": 100,
                "unique_passages": len(candidates),
                "verified_passages": len(vectors),
                "lexical_limit": 50 if mode == "hybrid" else 0,
            },
        }
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, MemoryError, ImportError, sqlite3.Error):
        return fallback()


def _native(request: dict[str, Any]) -> dict[str, Any]:
    if "torch" in sys.modules or "book_rag.semantic" in sys.modules:
        raise RuntimeError("Native worker must not import Torch or semantic model code")
    installation_path = Path(request["installation"])
    if file_sha256(installation_path) != request["installation_sha256"]:
        raise ValueError("Overlay installation changed before native query")
    installation, generation = _installation(installation_path)
    source = ReadOnlyLibrary(Path(installation["target_db_path"]).parent)
    with _snapshot(source) as coherent:
        before = _guard(coherent, installation)
    if request.get("operation") == "validate":
        from book_rag.experimental_ivfpq import faiss_backend, read_verified_index

        backend = faiss_backend()
        descriptor = _read(generation / "descriptor.json")
        codec = read_verified_index(generation / "codec.faiss", descriptor["codec_sha256"])
        for item in _read(generation / "ready.json")["shards"]:
            receipt = _shard_receipt(generation, item["start"], item["end"])
            if receipt is None:
                raise ValueError("Missing native shard receipt")
            index = read_verified_index(
                generation / (_shard_name(item["start"], item["end"]) + ".faiss"), receipt["index_sha256"]
            )
            if (
                not isinstance(index, backend.IndexIVFPQ)
                or index.d != 384
                or index.ntotal != item["end"] - item["start"]
            ):
                raise ValueError("Native shard shape/type differs")
            empty = backend.clone_index(index)
            empty.reset()
            if not np.array_equal(backend.serialize_index(empty), backend.serialize_index(codec)):
                raise ValueError("Native shard uses another metric/codebook")
            seen = set()
            for cell in range(index.nlist):
                size = index.invlists.list_size(cell)
                if size:
                    rows = backend.rev_swig_ptr(index.invlists.get_ids(cell), size)
                    if any(int(row) in seen or not item["start"] <= int(row) < item["end"] for row in rows):
                        raise ValueError("Native shard IDs differ")
                    seen.update(map(int, rows))
            if len(seen) != index.ntotal or index.nlist != descriptor["nlist"]:
                raise ValueError("Native shard omits window IDs")
        result = {"validated": True, "torch_imported": False}
    elif request.get("operation") == "query":
        from book_rag.incremental_ivfpq import query_incremental

        if request.get("book_id") is None and request.get("language") != "ru":
            raise ValueError("Overlay native queries require an explicit covered book or Russian scope")
        result = query_incremental(
            source,
            generation,
            np.asarray(request["vector"], dtype=np.float32),
            installation["model_space"],
            book_id=request.get("book_id"),
            language=request.get("language"),
            limit=100,
            nprobe=min(32, _read(generation / "descriptor.json")["nlist"]),
        )
    else:
        raise ValueError("Unknown native overlay operation")
    with _snapshot(source) as coherent:
        if _guard(coherent, installation)["state_token"] != before["state_token"]:
            raise ValueError("Covered source changed in native worker")
    if "torch" in sys.modules or "book_rag.semantic" in sys.modules:
        raise RuntimeError("Native worker imported a forbidden model module")
    if file_sha256(installation_path) != request["installation_sha256"]:
        raise ValueError("Overlay installation changed during native query")
    return result


if __name__ == "__main__":
    try:
        if sys.argv[1:] != ["--worker"]:
            raise ValueError("This module only exposes the bounded private native worker")
        data = sys.stdin.buffer.read(65537)
        if len(data) > 65536:
            raise ValueError("Native request exceeds64KiB")
        answer = {"ok": True, "result": _native(json.loads(data))}
    except BaseException as error:
        answer = {"ok": False, "error": type(error).__name__ + ": " + str(error)[:500]}
    print(json.dumps(answer, allow_nan=False))
