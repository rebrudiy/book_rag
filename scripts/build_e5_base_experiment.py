"""Prepare/resume a gated E5-base index experiment on an owned frozen20 copy.

Default is preparation only. --build requires a completed validated large v3
development report whose summary proves the semantic candidate was exercised.
The profile is process-local; package defaults and original/live active pointers
are never changed. No evaluation queries or sealed labels are opened here.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import resource
import runpy
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_rag.embedding_profiles import PROFILES, EmbeddingProfile

ROOT = Path(__file__).resolve().parents[1]
MODEL = "intfloat/multilingual-e5-base"
REVISION = "d128750597153bb5987e10b1c3493a34e5a4502a"
SOURCE_SHA256 = "ea1feab7c1d1c58acd7457529ef70885f2767ceeab6050ec12db0fa4f74bb4b8"
RESERVE = 4 * 1024**3
PROFILE = EmbeddingProfile(
    "experiment-multilingual-e5-base-v1",
    MODEL,
    REVISION,
    query_prefix="query: ",
    document_prefix="passage: ",
    max_tokens=512,
    strategy="token-windows-e5-v1",
)
MARKER = "e5-base-experiment.json"


def file_sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(path: Path) -> tuple[int, int, int, int, int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular nonsymlink input file")
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def quiet(database: Path) -> None:
    wal = database.with_name(database.name + "-wal")
    if wal.is_symlink() or (wal.exists() and wal.stat().st_size):
        raise ValueError("Source must be checkpointed with no nonempty WAL")


def prepare_copy(source: Path, target: Path, expected_sha: str = SOURCE_SHA256) -> dict[str, Any]:
    source = source.expanduser().resolve(strict=True)
    target = target.expanduser().absolute()
    if target.is_symlink() or target.resolve().is_relative_to(source) or source.is_relative_to(target.resolve()):
        raise ValueError("Use a distinct owned experiment directory outside the source")
    database = source / "library.sqlite3"
    quiet(database)
    before = identity(database)
    if file_sha(database) != expected_sha:
        raise ValueError("Original source database differs from the pinned frozen20 SHA256")
    if target.exists():
        marker = target / MARKER
        if not marker.is_file() or marker.is_symlink():
            raise ValueError("Refuse to overwrite an existing unowned target")
        receipt = json.loads(marker.read_text())
        if (
            receipt.get("kind") != "owned_e5_base_frozen20_copy_v1"
            or receipt.get("source_database_sha256") != expected_sha
            or receipt.get("profile") != PROFILE.metadata(REVISION)
            or file_sha(target / "library.sqlite3") != expected_sha
            or identity(target / "library.sqlite3")[:2] == before[:2]
        ):
            raise ValueError("Existing target ownership/profile/database proof differs")
        if identity(database) != before:
            raise RuntimeError("Original source changed during preparation validation")
        return receipt
    target.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(target.parent).free < before[2] + RESERVE:
        raise OSError("Owned copy requires its bytes plus 4GiB free reserve")
    with tempfile.TemporaryDirectory(prefix=".e5-base-owned-", dir=target.parent) as temporary:
        staging = Path(temporary)
        copied = staging / "library.sqlite3"
        shutil.copyfile(database, copied)
        if file_sha(copied) != expected_sha or file_sha(database) != expected_sha or identity(database) != before:
            raise RuntimeError("Copy or original database bytes changed during capture")
        copied_identity = identity(copied)
        if copied_identity[:2] == before[:2]:
            raise ValueError("Owned corpus must have a new inode, never a source hardlink")
        quiet(database)
        receipt = {
            "schema_version": 1,
            "kind": "owned_e5_base_frozen20_copy_v1",
            "created_at": datetime.now(UTC).isoformat(),
            "source": str(source),
            "source_database_sha256": expected_sha,
            "copied_database_sha256": expected_sha,
            "source_identity": before,
            "copied_identity": copied_identity,
            "copy_method": "independent shutil.copyfile, identical bytes and distinct inode verified",
            "profile": PROFILE.metadata(REVISION),
            "status": "prepared",
        }
        atomic_json(staging / MARKER, receipt)
        if target.exists():
            raise FileExistsError("Target appeared while capturing the owned copy")
        staging.rename(target)
    return receipt


def validated_gate(directory: Path) -> dict[str, Any]:
    state_path, summary_path = directory / "state.json", directory / "summary.json"
    state = json.loads(state_path.read_text())
    summary = json.loads(summary_path.read_text())
    if (
        state.get("status") != "complete"
        or state.get("validated") is not True
        or summary.get("semantic_candidate_exercised") is not True
        or not state.get("dataset_sha256")
        or state.get("dataset_sha256") != summary.get("dataset_sha256")
    ):
        raise ValueError("Gate pending: require completed validated v3 with semantic_candidate_exercised=true")
    return {
        "directory": str(directory),
        "status": "complete",
        "validated": True,
        "semantic_candidate_exercised": True,
        "dataset_sha256": state["dataset_sha256"],
        "state_sha256": file_sha(state_path),
        "summary_sha256": file_sha(summary_path),
    }


@contextmanager
def experimental_profile(cache: Path) -> Generator[None]:
    previous_profile = PROFILES.get(PROFILE.name)
    configuration = {
        "BOOKRAG_MODEL_CACHE": str(cache.resolve()),
        "BOOKRAG_EMBEDDING_MODEL": MODEL,
        "BOOKRAG_EMBEDDING_PROFILE": PROFILE.name,
        "BOOKRAG_EMBEDDING_REVISION": REVISION,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        "PYTORCH_MPS_HIGH_WATERMARK_RATIO": "0.65",
        "PYTORCH_MPS_LOW_WATERMARK_RATIO": "0.5",
    }
    previous_environment = {key: os.environ.get(key) for key in configuration}
    try:
        PROFILES[PROFILE.name] = PROFILE
        os.environ.update(configuration)
        yield
    finally:
        if previous_profile is None:
            PROFILES.pop(PROFILE.name, None)
        else:
            PROFILES[PROFILE.name] = previous_profile
        for key, value in previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def protected_inputs(source: Path) -> dict[Path, str]:
    paths = [source / "library.sqlite3", ROOT / "data/snapshots/curated20-e5/library.sqlite3"]
    paths.extend(
        directory / "vectors/active.json"
        for directory in [source, ROOT / "data/snapshots/curated20-e5", ROOT / "data/full", ROOT / "data"]
    )
    return {path: file_sha(path) for path in paths if path.exists()}


def checkpoint_progress(target: Path) -> dict[str, Any]:
    checkpoints = list((target / "vectors").glob("staging-*/checkpoint.json"))
    active = target / "vectors/active.json"
    if not checkpoints and active.exists():
        metadata = json.loads(active.read_text())
        if metadata.get("embedding_profile") == PROFILE.metadata(REVISION):
            return {
                "committed_vectors": metadata.get("vectors"),
                "planned_vectors": metadata.get("vectors"),
                "ready": True,
            }
    for path in sorted(checkpoints, key=lambda item: item.stat().st_mtime, reverse=True):
        data = json.loads(path.read_text())
        if data.get("descriptor", {}).get("embedding_profile") == PROFILE.metadata(REVISION):
            return {
                "committed_vectors": data.get("completed_vectors", 0),
                "planned_vectors": data.get("descriptor", {}).get("vectors"),
                "checkpoint": str(path.relative_to(target)),
                "ready": False,
            }
    return {"committed_vectors": 0, "planned_vectors": None, "ready": False}


def build(source: Path, target: Path, gate_directory: Path, cache: Path) -> dict[str, Any]:
    gate = validated_gate(gate_directory)
    preparation = prepare_copy(source, target)
    protected = protected_inputs(source)
    if shutil.disk_usage(target).free < RESERVE:
        raise OSError("Four GiB free reserve is required before loading the model")
    snapshot = cache / "models--intfloat--multilingual-e5-base/snapshots" / REVISION
    if not (snapshot / "model.safetensors").is_file():
        raise ValueError("Acquire the pinned safetensors snapshot locally before this offline experiment")
    started = time.monotonic()
    receipt: dict[str, Any] = {
        "kind": "owned_frozen20_e5_base_index_experiment_no_promotion",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "pid": os.getpid(),
        "gate": gate,
        "model": MODEL,
        "revision": REVISION,
        "profile": PROFILE.metadata(REVISION),
        "batch_size": 8,
        "model_cache": str(cache),
        "offline": True,
        "mps_high_watermark_ratio": 0.65,
        "mps_low_watermark_ratio": 0.5,
        "source_database_sha256_before": preparation["source_database_sha256"],
        "owned_corpus": str(target),
        "copy_has_new_inode": True,
    }
    receipt_path = target / "build-receipt.json"
    atomic_json(receipt_path, receipt)
    telemetry_path = target / "resources.jsonl"
    stopped = threading.Event()
    write_lock = threading.Lock()
    runtime: dict[str, Any] = {"torch": None, "phase": "loading_model"}
    previous = {"time": started, "completed": checkpoint_progress(target)["committed_vectors"]}

    def sample() -> None:
        with write_lock:
            now = time.monotonic()
            progress = checkpoint_progress(target)
            done = progress["committed_vectors"] or 0
            record = {
                "observed_at": datetime.now(UTC).isoformat(),
                "pid": os.getpid(),
                "phase": runtime["phase"],
                "wall_seconds": round(now - started, 3),
                **progress,
                "interval_vectors_per_second": round((done - previous["completed"]) / (now - previous["time"]), 3)
                if now > previous["time"]
                else None,
                "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1 if sys.platform == "darwin" else 1024),
                "free_disk_bytes": shutil.disk_usage(target).free,
            }
            torch = runtime["torch"]
            if torch is not None and torch.backends.mps.is_available():
                record.update(
                    mps_current_bytes=torch.mps.current_allocated_memory(),
                    mps_driver_bytes=torch.mps.driver_allocated_memory(),
                    mps_recommended_bytes=torch.mps.recommended_max_memory(),
                )
            with telemetry_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")
            previous.update(time=now, completed=done)
            print(json.dumps(record), flush=True)

    def monitor() -> None:
        while not stopped.wait(30):
            sample()

    worker = threading.Thread(target=monitor, daemon=True)
    sample()
    worker.start()
    try:
        with experimental_profile(cache):
            # This is the first possible runtime import; gate validation precedes it.
            torch = importlib.import_module("torch")
            runtime["torch"] = torch
            if not torch.backends.mps.is_available():
                raise RuntimeError("This experiment requires MPS; no implicit CPU/device substitution")
            semantic = importlib.import_module("book_rag.semantic")
            model = semantic._model(MODEL, str(cache.resolve()), REVISION)
            model.to("mps")
            if model.max_seq_length != 512 or model.get_embedding_dimension() != 768:
                raise ValueError("Loaded E5-base dimensions or token limit differ from the experimental contract")
            receipt["device"] = str(model.device)
            receipt["dtype"] = str(next(model.parameters()).dtype)
            runtime["phase"] = "embedding"
            frozen_class = runpy.run_path(str(ROOT / "scripts/embed_frozen.py"))["FrozenEmbeddingSource"]
            result = semantic.build_index(frozen_class(target), model_name=MODEL, batch_size=8)
            metadata = json.loads((target / "vectors/active.json").read_text())
            if metadata.get("embedding_profile") != PROFILE.metadata(REVISION) or metadata.get("dimensions") != 768:
                raise ValueError("Completed index metadata differs from the frozen model input contract")
            if file_sha(target / "library.sqlite3") != SOURCE_SHA256:
                raise RuntimeError("Owned source bytes changed during embedding")
            if any(file_sha(path) != checksum for path, checksum in protected.items()):
                raise RuntimeError("An original database or protected active pointer changed during the experiment")
            receipt.update(
                status="complete",
                result=result,
                active_metadata=metadata,
                source_database_sha256_after=file_sha(source / "library.sqlite3"),
                original_and_live_active_guards_passed=True,
                owned_database_sha256_after=file_sha(target / "library.sqlite3"),
            )
            runtime["phase"] = "complete"
    except BaseException as error:
        runtime["phase"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        receipt.update(
            status=runtime["phase"], error=f"{type(error).__name__}: {error}", progress=checkpoint_progress(target)
        )
        raise
    finally:
        stopped.set()
        worker.join(timeout=2)
        sample()
        receipt["runtime_seconds"] = round(time.monotonic() - started, 3)
        receipt["telemetry"] = str(telemetry_path)
        receipt["free_disk_bytes_after"] = shutil.disk_usage(target).free
        atomic_json(receipt_path, receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/snapshots/curated20-v3")
    parser.add_argument("--target", type=Path, default=ROOT / "data/snapshots/curated20-e5base-v1")
    parser.add_argument("--gate", type=Path, default=ROOT / "experiments/large-subset-dev-v3")
    parser.add_argument("--model-cache", type=Path, default=ROOT / "data/models")
    parser.add_argument("--build", action="store_true", help="Load MPS model and build only after validated gate")
    arguments = parser.parse_args()
    source, target, cache = arguments.source.resolve(), arguments.target.absolute(), arguments.model_cache.resolve()
    preparation = prepare_copy(source, target)
    if not arguments.build:
        print(json.dumps(preparation, ensure_ascii=False, indent=2))
        return
    receipt = build(source, target, arguments.gate.resolve(), cache)
    print(
        json.dumps(
            {"status": receipt["status"], "result": receipt["result"], "receipt": str(target / "build-receipt.json")}
        )
    )


if __name__ == "__main__":
    main()
