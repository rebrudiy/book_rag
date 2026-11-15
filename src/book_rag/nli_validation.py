"""Explicit pinned NLI acquisition and offline CPU claim experiments.

Experimental scores are not truth guarantees or question-coverage judgments.
No production answer code imports this module.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import resource
import shutil
import struct
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

REPOSITORY = "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli"
REVISION = "8adb042d524ecd5c26d3e3ba0e3fbcf7e2d0864c"
DEFAULT_DIRECTORY = Path("data/models/mdeberta-nli") / REVISION
GIB = 1024**3
DISK_FLOOR = int(4.5 * GIB)
MAX_MEMORY = int(2.5 * GIB)
LABELS = {0: "entailment", 1: "neutral", 2: "contradiction"}


@dataclass(frozen=True, slots=True)
class FilePin:
    name: str
    size: int
    digest: str
    algorithm: str = "sha256"


PINS = (
    FilePin("README.md", 6514, "79c1d900c4aa86ad9263c6eb90577608831d5468", "git-sha1"),
    FilePin("config.json", 1066, "25b875998a15441ca06ee6d96b4cae362514549d", "git-sha1"),
    FilePin("model.safetensors", 557652046, "65af59b1ff4450b09ecbf13ca35c840dbf038b26ff8e10e5ea89ca724828ed1e"),
    FilePin("special_tokens_map.json", 286, "2c9cb07c8fdeeb5ac3ceafb170592e990b204dcd", "git-sha1"),
    FilePin("spm.model", 4305025, "13c8d666d62a7bc4ac8f040aab68e942c861f93303156cc28f5c7e885d86d6e3"),
    FilePin("tokenizer.json", 16331396, "3aca3ce69a0a35aeb144a52c4f1d41c4246b8785f8f398315cc8fb6b24057810"),
    FilePin("tokenizer_config.json", 1256, "173d68ceeb1e02125bda27f0867401b1deaad258", "git-sha1"),
)


class NLIUnavailable(ValueError):
    pass


def peak_resident_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def memory_guard() -> None:
    if peak_resident_bytes() > MAX_MEMORY:
        raise NLIUnavailable("NLI process exceeded2.5GiB measured peak RSS")


def _save(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    if path.is_symlink() or temporary.is_symlink():
        raise NLIUnavailable("NLI metadata paths must not be symlinks")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def file_identity(path: Path, pin: FilePin) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != pin.size:
        raise NLIUnavailable("Missing, unsafe or incorrectly sized pinned file: " + pin.name)
    sha256 = hashlib.sha256()
    expected = hashlib.sha1() if pin.algorithm == "git-sha1" else sha256
    if pin.algorithm == "git-sha1":
        expected.update(f"blob {pin.size}\0".encode())
    with path.open("rb") as stream:
        while block := stream.read(1024**2):
            sha256.update(block)
            if expected is not sha256:
                expected.update(block)
    if expected.hexdigest() != pin.digest:
        raise NLIUnavailable("Pinned file hash mismatch: " + pin.name)
    return asdict(pin) | {"sha256": sha256.hexdigest()}


def verify_model(directory: Path) -> dict[str, Any]:
    if directory.is_symlink():
        raise NLIUnavailable("NLI model directory must not be a symlink")
    files = [file_identity(directory / pin.name, pin) for pin in PINS]
    config = json.loads((directory / "config.json").read_text())
    if config.get("id2label") != {str(k): v for k, v in LABELS.items()} or config.get("max_position_embeddings") != 512:
        raise NLIUnavailable("Pinned NLI label/position configuration differs")
    weights = directory / "model.safetensors"
    with weights.open("rb") as stream:
        header_length = struct.unpack("<Q", stream.read(8))[0]
        if not 1 <= header_length <= 16 * 1024**2 or header_length + 8 >= weights.stat().st_size:
            raise NLIUnavailable("Invalid bounded safetensors header")
        header = json.loads(stream.read(header_length))
    if not isinstance(header, dict) or not any(
        isinstance(v, dict) and v.get("dtype") == "F16" for v in header.values()
    ):
        raise NLIUnavailable("Expected pinned F16 safetensors, not pickle weights")
    return {"repository": REPOSITORY, "revision": REVISION, "files": files, "license": "MIT", "verified": True}


def _defer(directory: Path, error: str, retry_after: str | None, access_state: Path) -> None:
    until = time.time() + 3600
    if retry_after:
        try:
            delta = float(retry_after)
            if not math.isfinite(delta) or delta < 0:
                raise ValueError("Invalid Retry-After")
            until = time.time() + max(60, delta)
        except ValueError:
            try:
                until = max(time.time() + 60, parsedate_to_datetime(retry_after).timestamp())
            except (ValueError, TypeError, OverflowError):
                pass
    targets = {directory / "deferred.json", access_state}
    for target in targets:
        if target.exists():
            previous = json.loads(target.read_text())
            until = max(until, previous["retry_at_epoch"])
    value = {
        "error": error,
        "retry_at_epoch": until,
        "retry_at": datetime.fromtimestamp(until, UTC).isoformat(),
        "automatic_retry": False,
    }
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        _save(target, value)


def acquire_nli(
    directory: Path = DEFAULT_DIRECTORY,
    *,
    disk_floor: int = DISK_FLOOR,
    access_state: Path = Path("data/models/.nli-huggingface-deferred.json"),
) -> dict[str, Any]:
    """One owned cache, verified pins, resumable partials; no Hugging Face hub cache."""
    import fcntl

    if disk_floor < DISK_FLOOR:
        raise NLIUnavailable("NLI acquisition requires at least4.5GiB free disk after remaining bytes")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise NLIUnavailable("NLI directory must not be a symlink")
    lock_path = directory / ".acquisition.lock"
    if lock_path.is_symlink():
        raise NLIUnavailable("NLI acquisition lock must not be a symlink")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise NLIUnavailable("Another NLI acquirer owns this directory") from error
        receipts = []
        started = time.monotonic()
        deadline = started + 1800
        with httpx.Client(
            headers={
                "User-Agent": "BookRAG/0.1 (https://github.com/rebrudiy/book_rag; explicit pinned MIT NLI acquisition)",
                "Accept-Encoding": "identity",
            },
            timeout=httpx.Timeout(60, connect=15),
            follow_redirects=True,
        ) as client:
            for ordinal, pin in enumerate(PINS):
                path = directory / pin.name
                sidecar = directory / (pin.name + ".source.json")
                url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{pin.name}"
                if path.exists():
                    identity = file_identity(path, pin)
                    receipts.append(identity | {"cached": True, "source_url": url})
                    continue
                for defer in {directory / "deferred.json", access_state}:
                    if defer.exists():
                        until = json.loads(defer.read_text())["retry_at_epoch"]
                        if not isinstance(until, (int, float)) or not math.isfinite(until):
                            raise NLIUnavailable("Malformed NLI provider deferral; inspect " + str(defer))
                        if until > time.time():
                            raise NLIUnavailable("NLI provider deferred; preserve and respect " + str(defer))
                partial = directory / (pin.name + ".partial")
                if partial.is_symlink() or path.is_symlink() or sidecar.is_symlink():
                    raise NLIUnavailable("Unsafe NLI download path")
                offset = partial.stat().st_size if partial.exists() else 0
                if not 0 <= offset <= pin.size:
                    raise NLIUnavailable("Oversized partial preserved for explicit inspection")
                remaining = (
                    pin.size - offset + sum(p.size for p in PINS[ordinal + 1 :] if not (directory / p.name).exists())
                )
                if shutil.disk_usage(directory).free - remaining < disk_floor:
                    raise NLIUnavailable("Pinned NLI acquisition would breach4.5GiB disk reserve")
                response = None
                try:
                    if offset < pin.size:
                        headers = {"Range": f"bytes={offset}-"} if offset else {}
                        with client.stream("GET", url, headers=headers) as response:
                            if urlsplit(str(response.url)).scheme != "https":
                                raise NLIUnavailable("NLI redirect must remain HTTPS")
                            if response.status_code not in ({206, 200} if offset else {200}):
                                raise NLIUnavailable(f"NLI download HTTP{response.status_code}")
                            if (
                                response.status_code == 206
                                and response.headers.get("content-range") != f"bytes {offset}-{pin.size - 1}/{pin.size}"
                            ):
                                raise NLIUnavailable("NLI partial response range differs from pinned file")
                            if response.status_code == 200:
                                offset = 0
                            with partial.open("ab" if offset else "wb") as stream:
                                for block in response.iter_bytes(1024**2):
                                    if offset + len(block) > pin.size or time.monotonic() > deadline:
                                        raise NLIUnavailable("NLI file/deadline acquisition bound reached")
                                    if shutil.disk_usage(directory).free - len(block) < disk_floor:
                                        raise NLIUnavailable("NLI disk reserve changed during transfer")
                                    stream.write(block)
                                    offset += len(block)
                    identity = file_identity(partial, pin)
                    partial.replace(path)
                    receipt = identity | {
                        "source_url": url,
                        "final_url": str(response.url) if response else None,
                        "retrieved_at": datetime.now(UTC).isoformat(),
                        "repository": REPOSITORY,
                        "revision": REVISION,
                        "license": "MIT",
                    }
                    _save(sidecar, receipt)
                    receipts.append(receipt | {"cached": False})
                except (httpx.HTTPError, OSError, ValueError) as error:
                    _defer(
                        directory,
                        str(error),
                        response.headers.get("retry-after") if response is not None else None,
                        access_state,
                    )
                    raise NLIUnavailable(str(error)) from error
        result = verify_model(directory) | {
            "acquired_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "total_bytes": sum(pin.size for pin in PINS),
            "new_files": sum(not r["cached"] for r in receipts),
            "free_bytes": shutil.disk_usage(directory).free,
            "receipts": receipts,
        }
        _save(directory / "acquisition.json", result)
        return result


class NLIPredictor:
    """Lazy, offline CPU float32 inference with explicit pair-length rejection."""

    def __init__(self, directory: Path = DEFAULT_DIRECTORY, *, max_tokens: int = 512) -> None:
        if not 1 <= max_tokens <= 512:
            raise ValueError("NLI token limit must be1–512")
        self.directory = directory
        self.max_tokens = max_tokens
        self.tokenizer: Any = None
        self.model: Any = None
        self.load_seconds = 0.0

    def _load(self) -> None:
        if self.model is not None:
            return
        verify_model(self.directory)
        started = time.monotonic()
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        torch = importlib.import_module("torch")
        transformers = importlib.import_module("transformers")

        torch.set_num_threads(2)
        if torch.get_num_interop_threads() != 1:
            torch.set_num_interop_threads(1)
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(
            str(self.directory.resolve()), local_files_only=True, trust_remote_code=False, use_fast=True
        )
        self.model = transformers.AutoModelForSequenceClassification.from_pretrained(
            str(self.directory.resolve()),
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
            dtype=torch.float32,
            device_map=None,
        )
        self.model.to("cpu").eval()
        if self.model.config.id2label != LABELS:
            raise NLIUnavailable("Loaded NLI label mapping differs")
        memory_guard()
        self.load_seconds = time.monotonic() - started

    def score(self, premise: str, hypothesis: str) -> dict[str, Any]:
        if not premise.strip() or not hypothesis.strip():
            return {"status": "invalid_empty_pair", "scores": None, "truncated": False}
        if len(premise) + len(hypothesis) > 200000:
            return {"status": "input_character_limit", "scores": None, "truncated": False}
        self._load()
        encoded = self.tokenizer(premise, hypothesis, truncation=False, add_special_tokens=True)
        tokens = len(encoded["input_ids"])
        if tokens > self.max_tokens:
            return {
                "status": "context_limit",
                "tokens": tokens,
                "token_limit": self.max_tokens,
                "scores": None,
                "truncated": False,
            }
        torch = importlib.import_module("torch")

        started = time.monotonic()
        inputs = {key: torch.tensor([value], dtype=torch.long, device="cpu") for key, value in encoded.items()}
        with torch.inference_mode():
            logits = self.model(**inputs).logits[0]
            values = torch.softmax(logits.float(), dim=-1).tolist()
        memory_guard()
        if len(values) != 3 or not all(math.isfinite(v) for v in values):
            raise NLIUnavailable("Invalid NLI output logits/probabilities")
        return {
            "status": "scored",
            "scores": {LABELS[i]: float(v) for i, v in enumerate(values)},
            "predicted_label": LABELS[max(range(3), key=lambda i: values[i])],
            "tokens": tokens,
            "token_limit": self.max_tokens,
            "truncated": False,
            "inference_seconds": round(time.monotonic() - started, 6),
            "semantic_truth_verified": False,
            "question_axis_coverage_verified": False,
        }
