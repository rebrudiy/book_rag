"""Offline MiniLM service for isolated Torch-only pilot workers.

The coordinator and FAISS worker never import Torch. A trusted local pipe
transfers at most32 inputs/float32 vectors per request, with finite deadlines.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import inspect
import json
import math
import multiprocessing
import os
import resource
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from book_rag.embedding_profiles import MINILM_MODEL, MINILM_PROFILE, MINILM_REVISION
from book_rag.experimental_ivfpq import file_sha256


def plain_metadata(value: Any) -> Any:
    """Strip framework string subclasses before pickle crosses process borders.

    In particular TorchVersion unpickling imports Torch even when its value
    looks like a string. Only builtin JSON metadata may leave this worker.
    """
    payload = json.dumps(value, allow_nan=False, ensure_ascii=False)
    if len(payload.encode()) > 1024 * 1024:
        raise ValueError("Offline encoder metadata exceeds1MiB")
    return json.loads(payload)


def model_snapshot(cache: Path) -> tuple[Path, list[Path]]:
    cache = cache.resolve()
    snapshot = cache / ("models--" + MINILM_MODEL.replace("/", "--")) / "snapshots" / MINILM_REVISION
    if not snapshot.is_dir():
        raise ValueError("Pinned offline MiniLM cache is missing; downloads are disabled")
    files = sorted({path.resolve() for path in snapshot.rglob("*") if path.is_file()})
    if not files or any(not path.is_relative_to(cache) for path in files):
        raise ValueError("Unsafe model cache references")
    return snapshot, files


def rpc(connection: Connection, request: dict[str, Any], timeout: float = 45) -> Any:
    if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError("Offline encoder IPC timeout must be finite and within120s")
    started = time.monotonic()
    finished = threading.Event()
    errors = []

    def send() -> None:
        try:
            connection.send(request)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    threading.Thread(target=send, daemon=True).start()
    if not finished.wait(timeout):
        raise TimeoutError("Offline encoder IPC send timed out")
    if errors:
        raise RuntimeError("Offline encoder IPC send failed") from errors[0]
    if not connection.poll(max(0.001, timeout - (time.monotonic() - started))):
        raise TimeoutError("Offline encoder IPC request timed out")
    try:
        response = connection.recv()
    except (EOFError, OSError) as error:
        raise RuntimeError("Offline encoder disconnected before responding") from error
    if not isinstance(response, dict) or "result" not in response and response.get("ok") is True:
        raise RuntimeError("Offline encoder returned an invalid response")
    if response.get("ok") is not True:
        raise RuntimeError("Offline encoder failed: " + str(response.get("error", "invalid response")))
    return response["result"]


def _worker(connection: Connection, settings: dict[str, Any]) -> None:
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "2"
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
        os.environ[name] = "1"
    os.environ.pop("BOOKRAG_IVFPQ_WORKER", None)
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    started = time.perf_counter()
    try:
        snapshot, files = model_snapshot(Path(settings["cache"]))
        before = {str(path): file_sha256(path) for path in files}
        import numpy as np

        torch = importlib.import_module("torch")
        SentenceTransformer = importlib.import_module("sentence_transformers").SentenceTransformer

        from book_rag.semantic import _windows, token_windows
        from book_rag.semantic_pilot import prepare_windows

        torch.set_num_threads(2)
        if settings["device"] == "mps" and not torch.backends.mps.is_available():
            raise ValueError("Requested MPS is unavailable")
        model = SentenceTransformer(
            str(snapshot), device=settings["device"], local_files_only=True, trust_remote_code=False
        )
        if model.max_seq_length != 128 or "faiss" in sys.modules:
            raise ValueError("Unexpected model window budget or FAISS import")
        recipe = hashlib.sha256((inspect.getsource(_windows) + inspect.getsource(token_windows)).encode()).hexdigest()
        space = {
            "model": MINILM_MODEL,
            "revision": MINILM_REVISION,
            "dimensions": 384,
            "profile": MINILM_PROFILE.metadata(MINILM_REVISION),
            "strategy": "token-windows-v2",
            "assignment": "Actual canonical production _windows inputs, offline normalized MiniLM float32",
            "window_recipe_sha256": recipe,
            "model_files_sha256": sorted(before.values()),
            "torch_version": str(torch.__version__),
            "sentence_transformers_version": importlib.metadata.version("sentence-transformers"),
        }
        stats = {
            "device": settings["device"],
            "rows_encoded": 0,
            "encode_requests": 0,
            "encode_seconds": 0.0,
            "load_seconds": time.perf_counter() - started,
            "faiss_imported": False,
            "model_space": space,
        }
        connection.send({"ok": True, "result": plain_metadata(stats)})
        while True:
            request = connection.recv()
            operation = request.get("operation")
            if "faiss" in sys.modules:
                raise ValueError("FAISS entered Torch-only worker")
            if operation == "prepare":
                result = prepare_windows(Path(request["source"]), Path(request["output"]), model, space)
                connection.send({"ok": True, "result": plain_metadata(result)})
            elif operation == "prepare_batch":
                from book_rag.semantic_batches import prepare_batch_windows

                result = prepare_batch_windows(
                    Path(request["source"]),
                    Path(request["output"]),
                    request["book_ids"],
                    model,
                    space,
                    max_windows=request.get("max_windows", 200_000),
                    max_manifest_bytes=request.get("max_manifest_bytes", 512 * 1024**2),
                )
                connection.send({"ok": True, "result": plain_metadata(result)})
            elif operation == "encode":
                texts = request.get("texts")
                if (
                    not isinstance(texts, list)
                    or not 1 <= len(texts) <= 32
                    or any(not isinstance(text, str) or not text or len(text) > 32768 for text in texts)
                    or sum(map(len, texts)) > 65536
                ):
                    raise ValueError("Encoder IPC batch exceeds bounded input contract")
                if settings["device"] == "mps":
                    torch.mps.synchronize()
                began = time.perf_counter()
                vectors = model.encode(
                    texts, batch_size=32, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
                )
                if settings["device"] == "mps":
                    torch.mps.synchronize()
                elapsed = time.perf_counter() - began
                if vectors.dtype != np.float32 or vectors.shape != (len(texts), 384) or not np.isfinite(vectors).all():
                    raise ValueError("Invalid encoder output")
                if np.max(np.abs(np.linalg.norm(vectors, axis=1) - 1)) > 0.001:
                    raise ValueError("Encoder output is not unit normalized")
                stats["rows_encoded"] += len(texts)
                stats["encode_requests"] += 1
                stats["encode_seconds"] += elapsed
                connection.send({"ok": True, "result": {"vectors": vectors, "seconds": elapsed}})
            elif operation == "stats":
                connection.send({"ok": True, "result": plain_metadata(stats)})
            elif operation == "shutdown":
                after = {str(path): file_sha256(path) for path in files}
                if before != after:
                    raise ValueError("Pinned original model files changed during pilot")
                rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                stats["peak_rss_bytes"] = int(rss if sys.platform == "darwin" else rss * 1024)
                stats["model_files_unchanged"] = True
                connection.send({"ok": True, "result": plain_metadata(stats)})
                return
            else:
                raise ValueError("Unknown offline encoder operation")
    except BaseException as error:
        try:
            connection.send({"ok": False, "error": type(error).__name__ + ": " + str(error)[:1000]})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


class EncoderService:
    def __init__(self, cache: Path, device: str = "mps") -> None:
        if device not in ("cpu", "mps"):
            raise ValueError("Pilot encoder device must be CPU or MPS")
        model_snapshot(cache)
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self.connection = parent
        self.process = context.Process(target=_worker, args=(child, {"cache": str(cache.resolve()), "device": device}))
        self.process.start()
        child.close()
        started = time.monotonic()
        while not parent.poll(0.2):
            probe = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(self.process.pid)], capture_output=True, text=True, check=False
            )
            if int(probe.stdout.strip() or "0") * 1024 > 2 * 1024**3:
                self.abort()
                raise RuntimeError("Offline encoder startup exceeded2GiB RSS")
            if time.monotonic() - started < 45 and self.process.is_alive():
                continue
            self.abort()
            raise TimeoutError("Offline encoder startup timed out")
        try:
            response = parent.recv()
            if not isinstance(response, dict):
                raise RuntimeError("Invalid encoder startup response")
            if response.get("ok") is not True or "result" not in response:
                raise RuntimeError(str(response.get("error", "Encoder startup failed")))
            self.metadata = response["result"]
        except BaseException:
            self.abort()
            raise

    def close(self) -> dict[str, Any]:
        try:
            result = rpc(self.connection, {"operation": "shutdown"}, 20)
            self.process.join(timeout=3)
            return result
        finally:
            self.abort()

    def abort(self) -> None:
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=1)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=1)
        self.connection.close()
