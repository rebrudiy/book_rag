"""Independent metadata-only semantic book discovery; never passage evidence."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import stat
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import numpy as np

from book_rag.embedding_profiles import MINILM_MODEL, MINILM_PROFILE, MINILM_REVISION

DIMENSION = 384
STRATEGY = "book-metadata-mean-windows-v1"
FIELD_CAPS = {"title_chars": 400, "author_chars": 300, "subject_count": 32, "subject_chars": 200}
NOTICE = (
    "Metadata semantic discovery searches titles, authors and catalog subjects; "
    "results are books, not full-text passages or quotations."
)


class BookLibrary(Protocol):
    data_dir: Path
    db_path: Path


@dataclass(slots=True)
class ReadOnlyBookLibrary:
    """Existing database paths only; no Library constructor, migrations or writes."""

    data_dir: Path
    db_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.data_dir = self.data_dir.resolve(strict=True)
        self.db_path = self.data_dir / "library.sqlite3"
        if not self.db_path.is_file() or self.db_path.is_symlink():
            raise DiscoveryUnavailable("Expected an existing regular library database")


class DiscoveryUnavailable(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _save(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    if path.is_symlink() or temporary.is_symlink():
        raise DiscoveryUnavailable("Unsafe discovery metadata path")
    with temporary.open("wb") as stream:
        stream.write(_canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def _connection(library: BookLibrary) -> Generator[sqlite3.Connection]:
    db = sqlite3.connect(library.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    try:
        yield db
    finally:
        db.close()


def _source_stat(path: str | None) -> list[int] | None:
    if not path:
        return None
    try:
        value = os.lstat(path)
        if not stat.S_ISREG(value.st_mode):
            return None
        return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]
    except OSError:
        return None


def metadata_fields(row: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    subjects = metadata.get("subjects", [])
    if not isinstance(subjects, list) or any(not isinstance(s, str) for s in subjects):
        raise DiscoveryUnavailable("Catalog subjects must be a list of strings")
    title = str(row["title"])
    author = str(row["author"])
    used_subjects = [subject[: FIELD_CAPS["subject_chars"]] for subject in subjects[: FIELD_CAPS["subject_count"]]]
    used = {
        "title": title[: FIELD_CAPS["title_chars"]],
        "author": author[: FIELD_CAPS["author_chars"]],
        "subjects": used_subjects,
    }
    clipped = {
        "title": len(title) > FIELD_CAPS["title_chars"],
        "author": len(author) > FIELD_CAPS["author_chars"],
        "subjects": len(subjects) > FIELD_CAPS["subject_count"]
        or any(len(s) > FIELD_CAPS["subject_chars"] for s in subjects),
    }
    text = "Title: " + used["title"] + "\nAuthor: " + used["author"] + "\nSubjects: " + "; ".join(used_subjects)
    return {"indexed_values": used, "field_caps": FIELD_CAPS, "field_truncation": clipped, "text": text}


def snapshot(library: BookLibrary) -> dict[str, Any]:
    records = []
    unavailable = []
    cards = {}
    with _connection(library) as db:
        db.execute("BEGIN")
        rows = db.execute(
            "SELECT b.*,m.metadata,EXISTS(SELECT 1 FROM sections s WHERE s.book_id=b.id) section_available "
            "FROM books b JOIN book_metadata m ON m.book_id=b.id WHERE b.status='indexed' ORDER BY b.id"
        ).fetchall()
        for sqlrow in rows:
            row = dict(sqlrow)
            raw = row.pop("metadata")
            metadata = json.loads(raw)
            if not isinstance(metadata, dict):
                raise DiscoveryUnavailable("Indexed book metadata is not an object")
            available = _source_stat(row.get("local_path"))
            if available is None or not row["section_available"]:
                unavailable.append(row["id"])
                continue
            guard = hashlib.sha256(
                _canonical({"book": row, "metadata": metadata, "source_stat": available})
            ).hexdigest()
            fields = metadata_fields(row, metadata)
            records.append({"id": row["id"], "guard": guard, "fields": fields})
            cards[row["id"]] = row | {
                "coverage": metadata.get("coverage"),
                "work_type": metadata.get("work_type"),
                "work_identity_status": metadata.get("work_identity_status"),
                "subjects": metadata.get("subjects", []),
                "coverage_scope": metadata.get("coverage_scope"),
                "illustrations_included": metadata.get("illustrations_included"),
                "source_variant": metadata.get("source_variant"),
            }
        all_indexed = db.execute("SELECT count(*) FROM books WHERE status='indexed'").fetchone()[0]
    fingerprint = hashlib.sha256(_canonical(records)).hexdigest()
    return {
        "records": records,
        "cards": cards,
        "fingerprint": fingerprint,
        "searchable_editions": all_indexed,
        "unavailable_ids": unavailable,
    }


def cached_model_path(library: BookLibrary) -> Path:
    configured = os.getenv("BOOKRAG_MODEL_CACHE")
    candidates = (
        [Path(configured).expanduser()]
        if configured
        else [library.data_dir / "models", library.data_dir.parent / "models"]
    )
    for cache in candidates:
        path = cache / ("models--" + MINILM_MODEL.replace("/", "--")) / "snapshots" / MINILM_REVISION
        if (path / "model.safetensors").is_file() and (path / "tokenizer.json").is_file():
            return path.resolve()
    raise DiscoveryUnavailable("Pinned MiniLM snapshot is not cached; discovery never downloads model weights")


@lru_cache(maxsize=2)
def _encoder(path: str, device: str) -> Any:
    module = importlib.import_module("sentence_transformers")
    return module.SentenceTransformer(path, device=device, local_files_only=True, trust_remote_code=False)


def model_identity(path: Path) -> dict[str, Any]:
    files = {str(p.relative_to(path)): _sha(p) for p in sorted(path.rglob("*")) if p.is_file()}
    pooling = json.loads((path / "1_Pooling/config.json").read_text())
    if pooling.get("word_embedding_dimension") != DIMENSION or not pooling.get("pooling_mode_mean_tokens"):
        raise DiscoveryUnavailable("Cached model does not have reviewed384-dimensional mean pooling")
    return {
        "model": MINILM_MODEL,
        "revision": MINILM_REVISION,
        "embedding_profile": MINILM_PROFILE.metadata(MINILM_REVISION),
        "files": files,
    }


def metadata_windows(model: Any, text: str) -> tuple[list[str], dict[str, Any]]:
    maximum = min(int(model.max_seq_length), 128)
    if maximum < 32:
        raise DiscoveryUnavailable("MiniLM token budget is invalid")
    offsets = model.tokenizer(text, add_special_tokens=False, return_offsets_mapping=True, truncation=False)[
        "offset_mapping"
    ]
    offsets = [(start, end) for start, end in offsets if end > start]
    special = int(model.tokenizer.num_special_tokens_to_add(pair=False))
    budget = maximum - special
    windows = []
    spans = []
    cursor = 0
    for _ in range(3):
        if cursor >= len(offsets):
            break
        end = min(cursor + budget, len(offsets))
        while end > cursor:
            start_char, end_char = offsets[cursor][0], offsets[end - 1][1]
            piece = text[start_char:end_char]
            count = len(model.tokenizer.encode(piece, add_special_tokens=True, truncation=False))
            if count <= maximum:
                break
            end -= 1
        if end <= cursor:
            raise DiscoveryUnavailable("Metadata tokenizer cannot make a bounded exact window")
        windows.append(piece)
        spans.append({"start": start_char, "end": end_char, "tokens": count})
        cursor = end
    if not windows:
        raise DiscoveryUnavailable("Empty metadata embedding input")
    return windows, {
        "source_tokens": len(offsets),
        "included_source_tokens": cursor,
        "window_spans": spans,
        "token_truncated": cursor < len(offsets),
        "maximum_tokens_per_window": maximum,
    }


def _mean_vectors(model: Any, windows: list[list[str]]) -> np.ndarray:
    flat = [text for group in windows for text in group]
    encoded = np.asarray(
        model.encode(flat, batch_size=32, convert_to_numpy=True, normalize_embeddings=True, show_progress_bar=False),
        dtype=np.float32,
    )
    if encoded.shape != (len(flat), DIMENSION) or not np.isfinite(encoded).all():
        raise DiscoveryUnavailable("Invalid metadata embedding output")
    results = []
    offset = 0
    for group in windows:
        vector = encoded[offset : offset + len(group)].mean(axis=0)
        offset += len(group)
        norm = float(np.linalg.norm(vector))
        if norm <= 0:
            raise DiscoveryUnavailable("Zero metadata embedding vector")
        results.append(vector / norm)
    return np.asarray(results, dtype=np.float32)


def build_book_discovery(library: BookLibrary, *, device: str = "cpu", batch_size: int = 128) -> dict[str, Any]:
    import fcntl

    if device not in {"cpu", "mps"} or not 1 <= batch_size <= 256:
        raise ValueError("Invalid metadata build device/batch")
    directory = library.data_dir / "book-discovery"
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise DiscoveryUnavailable("Discovery directory must not be a symlink")
    with (directory / ".build.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DiscoveryUnavailable("A book discovery build is already active") from error
        started = time.monotonic()
        source = snapshot(library)
        if not source["records"]:
            raise DiscoveryUnavailable("No indexed books have available source/reader records")
        path = cached_model_path(library)
        identity = model_identity(path)
        contract = {
            "strategy": STRATEGY,
            "metadata_fingerprint": source["fingerprint"],
            "model": identity,
            "dimension": DIMENSION,
            "field_caps": FIELD_CAPS,
        }
        generation = hashlib.sha256(_canonical(contract)).hexdigest()
        ready = directory / ("generation-" + generation[:24])
        staging = directory / (".building-" + generation[:24])
        if ready.exists():
            receipt = json.loads((ready / "ready.json").read_text())
            if (
                receipt["contract"] != contract
                or _sha(ready / "vectors.npy") != receipt["vectors_sha256"]
                or _sha(ready / "records.json") != receipt["records_sha256"]
            ):
                raise DiscoveryUnavailable("Existing ready vector integrity failure")
            if snapshot(library)["fingerprint"] != source["fingerprint"]:
                raise DiscoveryUnavailable("Metadata changed before ready activation")
            _save(directory / "current.json", {"generation": ready.name, "ready_sha256": _sha(ready / "ready.json")})
            return receipt | {"reused_ready": True}
        staging.mkdir(exist_ok=True)
        matrix_path = staging / "vectors.npy"
        checkpoint_path = staging / "checkpoint.json"
        count = len(source["records"])
        prefix = hashlib.sha256()
        completed = 0
        if checkpoint_path.exists():
            checkpoint = json.loads(checkpoint_path.read_text())
            if checkpoint["contract"] != contract:
                raise DiscoveryUnavailable("Checkpoint metadata/model contract changed")
            completed = checkpoint["completed"]
            matrix = np.load(matrix_path, mmap_mode="r+", allow_pickle=False)
            if matrix.shape != (count, DIMENSION) or matrix.dtype != np.float32 or not 0 <= completed <= count:
                raise DiscoveryUnavailable("Invalid discovery checkpoint vector shape/count")
            prefix.update(matrix[:completed].tobytes())
            if prefix.hexdigest() != checkpoint["completed_vectors_sha256"]:
                raise DiscoveryUnavailable("Discovery checkpoint vector prefix is corrupt")
            token_info = checkpoint["token_info"]
        else:
            matrix = np.lib.format.open_memmap(matrix_path, mode="w+", dtype=np.float32, shape=(count, DIMENSION))
            token_info = []
        model = _encoder(str(path), device)
        for start in range(completed, count, batch_size):
            batch = source["records"][start : start + batch_size]
            groups = []
            for record in batch:
                pieces, info = metadata_windows(model, record["fields"]["text"])
                groups.append(pieces)
                token_info.append(info)
            vectors = _mean_vectors(model, groups)
            matrix[start : start + len(batch)] = vectors
            matrix.flush()
            with matrix_path.open("rb") as stream:
                os.fsync(stream.fileno())
            prefix.update(vectors.tobytes())
            completed = start + len(batch)
            _save(
                checkpoint_path,
                {
                    "contract": contract,
                    "completed": completed,
                    "completed_vectors_sha256": prefix.hexdigest(),
                    "token_info": token_info,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                },
            )
        if snapshot(library)["fingerprint"] != source["fingerprint"]:
            raise DiscoveryUnavailable(
                "Book metadata/source availability changed during discovery build; no activation"
            )
        records = [record | {"token_info": info} for record, info in zip(source["records"], token_info, strict=True)]
        _save(staging / "records.json", records)
        receipt = {
            "schema_version": 1,
            "kind": "metadata_semantic",
            "created_at": datetime.now(UTC).isoformat(),
            "contract": contract,
            "indexed_editions": count,
            "searchable_editions_at_build": source["searchable_editions"],
            "unavailable_source_ids": source["unavailable_ids"],
            "vectors_sha256": _sha(matrix_path),
            "records_sha256": _sha(staging / "records.json"),
            "vector_bytes": matrix_path.stat().st_size,
            "vector_shape": [count, DIMENSION],
            "dtype": "float32",
            "device": device,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "token_truncated_editions": sum(info["token_truncated"] for info in token_info),
            "field_truncated_editions": sum(any(record["fields"]["field_truncation"].values()) for record in records),
            "metadata_windows": sum(len(info["window_spans"]) for info in token_info),
            "notice": NOTICE,
            "database_modified": False,
            "passage_semantic_coverage_claimed": False,
        }
        _save(staging / "ready.json", receipt)
        del matrix
        staging.replace(ready)
        _save(directory / "current.json", {"generation": ready.name, "ready_sha256": _sha(ready / "ready.json")})
        return receipt


def discover_books(library: BookLibrary, query: str, language: str | None = None, limit: int = 12) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Book discovery limit must be1–50")
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError("Book discovery query must be nonempty and at most2000 characters")
    result: dict[str, Any] = {
        "kind": "metadata_semantic",
        "books": [],
        "coverage": {"indexed_editions": 0, "searchable_editions": 0},
        "warnings": [],
        "notice": NOTICE,
        "status": "unavailable",
    }
    try:
        current = snapshot(library)
        result["coverage"]["searchable_editions"] = current["searchable_editions"]
        directory = library.data_dir / "book-discovery"
        if directory.is_symlink():
            raise DiscoveryUnavailable("Discovery directory must not be a symlink")
        pointer = json.loads((directory / "current.json").read_text())
        name = pointer["generation"]
        if not isinstance(name, str) or not name.startswith("generation-") or Path(name).name != name:
            raise DiscoveryUnavailable("Invalid book discovery generation pointer")
        generation = directory / name
        if generation.is_symlink() or _sha(generation / "ready.json") != pointer["ready_sha256"]:
            raise DiscoveryUnavailable("Book discovery ready receipt changed")
        receipt = json.loads((generation / "ready.json").read_text())
        if receipt["kind"] != "metadata_semantic" or receipt["contract"]["strategy"] != STRATEGY:
            raise DiscoveryUnavailable("Unsupported metadata index kind/strategy")
        if (
            _sha(generation / "records.json") != receipt["records_sha256"]
            or _sha(generation / "vectors.npy") != receipt["vectors_sha256"]
        ):
            raise DiscoveryUnavailable("Book discovery generation integrity failure")
        generation_stats = {
            name: _source_stat(str(generation / name)) for name in ("ready.json", "records.json", "vectors.npy")
        }
        if any(value is None for value in generation_stats.values()):
            raise DiscoveryUnavailable("Metadata generation files must be regular owned files")
        records = json.loads((generation / "records.json").read_text())
        guards = {record["id"]: record["guard"] for record in records}
        if len(guards) != len(records):
            raise DiscoveryUnavailable("Duplicate metadata vector book IDs")
        live = {record["id"]: record["guard"] for record in current["records"]}
        if any(live.get(bid) != guard for bid, guard in guards.items()):
            raise DiscoveryUnavailable("Covered book metadata/status/source availability changed; rebuild required")
        result["coverage"]["indexed_editions"] = len(records)
        if len(records) < current["searchable_editions"]:
            result["warnings"].append("metadata_index_partial_coverage")
        path = cached_model_path(library)
        if model_identity(path) != receipt["contract"]["model"]:
            raise DiscoveryUnavailable("Cached discovery model identity changed")
        model = _encoder(str(path), "cpu")
        windows, query_info = metadata_windows(model, query.strip())
        result["query_token_info"] = query_info
        if query_info["token_truncated"]:
            result.update(status="query_limit")
            result["warnings"].append("metadata_query_exceeds_three_token_windows")
            return result
        vector = _mean_vectors(model, [windows])[0]
        matrix = np.load(generation / "vectors.npy", mmap_mode="r", allow_pickle=False)
        if (
            matrix.shape != (len(records), DIMENSION)
            or matrix.dtype != np.float32
            or not np.isfinite(matrix).all()
            or np.max(np.abs(np.linalg.norm(matrix, axis=1) - 1)) > 0.001
        ):
            raise DiscoveryUnavailable("Invalid metadata vector matrix")
        scores = matrix @ vector
        candidates = [
            i
            for i, record in enumerate(records)
            if not language or current["cards"][record["id"]]["language"] == language
        ]
        selected = sorted(candidates, key=lambda i: (-float(scores[i]), records[i]["id"]))[:limit]
        books = []
        with _connection(library) as db:
            for i in selected:
                record = records[i]
                card = current["cards"][record["id"]].copy()
                bid = card["id"]
                card.pop("section_available", None)
                card.update(
                    score=float(scores[i]),
                    matched_fields=[
                        field for field in ("title", "author", "subjects") if record["fields"]["indexed_values"][field]
                    ],
                    metadata_evidence={
                        "title": card["title"],
                        "author": card["author"],
                        "subjects": card["subjects"],
                        "indexed_values": record["fields"]["indexed_values"],
                        "field_caps": record["fields"]["field_caps"],
                        "field_truncation": record["fields"]["field_truncation"],
                        "token_info": record["token_info"],
                    },
                    reader_url="/?" + urlencode({"book": bid}),
                    ingestion_status=card["status"],
                    sections=db.execute("SELECT count(*) FROM sections WHERE book_id=?", (bid,)).fetchone()[0],
                    passages=db.execute("SELECT count(*) FROM passages WHERE book_id=?", (bid,)).fetchone()[0],
                )
                books.append(card)
        after = snapshot(library)
        after_guards = {record["id"]: record["guard"] for record in after["records"]}
        if any(after_guards.get(bid) != guard for bid, guard in guards.items()):
            raise DiscoveryUnavailable("Covered metadata/source changed during query; results discarded")
        if any(_source_stat(str(generation / name)) != value for name, value in generation_stats.items()):
            raise DiscoveryUnavailable("Metadata generation files changed during query; results discarded")
        result.update(books=books, status="ready", score_kind="cosine_of_combined_metadata_not_probability")
        result["coverage"]["searchable_editions"] = after["searchable_editions"]
        if len(records) < after["searchable_editions"] and "metadata_index_partial_coverage" not in result["warnings"]:
            result["warnings"].append("metadata_index_partial_coverage")
        return result
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError, sqlite3.Error) as error:
        result.update(books=[], status="stale" if isinstance(error, DiscoveryUnavailable) else "unavailable")
        result["coverage"]["indexed_editions"] = 0
        result["warnings"].append("metadata_discovery_unavailable_or_stale")
        result["error"] = str(error)
        return result
