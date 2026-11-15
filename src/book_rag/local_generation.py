"""Pinned local GGUF acquisition and bounded offline generation.

No retrieval, question-answer prompt, source validation or remote inference lives
here. The caller supplies messages and must validate any generated claims.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import multiprocessing
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import urllib.request
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

MODEL_REPOSITORY = "Qwen/Qwen2.5-1.5B-Instruct-GGUF"
MODEL_REVISION = "91cad51170dc346986eccefdc2dd33a9da36ead9"
MODEL_FILENAME = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
MODEL_SHA256 = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"
MODEL_BYTES = 1_117_320_736
MODEL_LICENSE = "Apache-2.0"
LICENSE_SHA256 = "832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e"
RUNTIME_VERSION = "0.3.19"
MODEL_URL = f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/{MODEL_FILENAME}"
LICENSE_URL = f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/LICENSE"
GIB = 1024**3
DEFAULT_MODEL_DIRECTORY = Path("data/models/local-generation")
CONTROL_TOKENS = ("<|im_start|>", "<|im_end|>", "<|endoftext|>")
GRAMMAR_REPEAT_LIMIT = 64
GRAMMAR_PATH_BUDGET = 256
GRAMMAR_TOTAL_BUDGET = 1024


class GenerationError(RuntimeError):
    """A local generation request could not produce a bounded result."""


class ModelUnavailable(GenerationError):
    """The optional runtime or the exact acquired model is unavailable."""


class GenerationTimeout(GenerationError):
    """The finite request deadline expired; no partial answer is returned."""


class ContextLimitExceeded(GenerationError):
    """The complete prompt and requested output do not fit the configured context."""


@dataclass(frozen=True)
class GenerationLimits:
    """Finite per-request limits, including queue, verification and cold loading."""

    max_tokens: int = 384
    timeout_seconds: float = 45
    max_output_chars: int = 12_000
    temperature: float = 0.0
    seed: int = 7

    def __post_init__(self) -> None:
        if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 1024:
            raise ValueError("max_tokens must be an integer between 1 and 1024")
        if type(self.timeout_seconds) not in {int, float} or not math.isfinite(self.timeout_seconds):
            raise ValueError("timeout_seconds must be finite")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        if type(self.max_output_chars) is not int or not 1 <= self.max_output_chars <= 32_000:
            raise ValueError("max_output_chars must be an integer between 1 and 32000")
        if (
            type(self.temperature) not in {int, float}
            or not math.isfinite(self.temperature)
            or not 0 <= self.temperature <= 1
        ):
            raise ValueError("temperature must be finite and between 0 and 1")
        if type(self.seed) is not int or not 0 <= self.seed < 2**31:
            raise ValueError("seed must be an integer between 0 and 2**31-1")


def _messages(messages: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not 1 <= len(messages) <= 32:
        raise ValueError("Expected 1 to 32 chat messages")
    clean: list[dict[str, str]] = []
    total = 0
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
            raise ValueError("Each chat message needs only role and content")
        role, content = message["role"], message["content"]
        if (
            not isinstance(role, str)
            or role not in {"system", "user", "assistant"}
            or (role == "system" and index != 0)
        ):
            raise ValueError("Unsupported role or misplaced system message")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message content must be a nonempty string")
        if any(token in content for token in CONTROL_TOKENS):
            raise ValueError("Message contains a reserved ChatML control token")
        total += len(content)
        if total > 32_768:
            raise ContextLimitExceeded("Chat text exceeds the finite input-character budget")
        clean.append({"role": role, "content": content})
    if clean[-1]["role"] != "user":
        raise ValueError("The final chat message must have user role")
    return clean


def _chatml(messages: Sequence[Mapping[str, str]]) -> str:
    return "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in messages) + "<|im_start|>assistant\n"


def _response_schema(schema: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if schema is None:
        return None
    if not isinstance(schema, Mapping):
        raise ValueError("response_schema must be a JSON-schema object")
    try:
        encoded = json.dumps(dict(schema), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 16_384:
            raise ValueError("response_schema exceeds its finite schema-byte budget")
        result = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError("response_schema must be finite JSON within 16384 bytes") from error
    pending = [(result, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 24:
            raise ValueError("response_schema nesting exceeds its finite depth budget")
        if isinstance(value, dict):
            for child in value.values():
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    _schema_grammar_budget(result)
    return result


def _pattern_budget(pattern: Any) -> int:
    """Conservative atom-count estimate, not a replacement regular-expression parser."""
    if not isinstance(pattern, str) or len(pattern) > 512:
        raise ValueError("JSON-schema pattern exceeds its finite grammar budget")
    # Each frame tracks this branch's cost, the last atom, and prior branches.
    frames = [[0, 0, 0]]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        frame = frames[-1]
        if character == "\\":
            index += 2
            frame[0] += 1
            frame[1] = 1
            continue
        if character == "[":
            index += 1
            while index < len(pattern) and pattern[index] != "]":
                index += 2 if pattern[index] == "\\" else 1
            frame[0] += 1
            frame[1] = 1
        elif character == "(":
            if len(frames) >= 16:
                raise ValueError("JSON-schema pattern nesting exceeds its grammar budget")
            frames.append([0, 0, 0])
        elif character == ")":
            if len(frames) == 1:
                raise ValueError("Unbalanced JSON-schema pattern")
            child = frames.pop()
            frames[-1][1] = max(1, child[0], child[2])
            frames[-1][0] += frames[-1][1]
        elif character == "|":
            frame[2] = max(frame[2], frame[0])
            frame[0] = frame[1] = 0
        elif character == "{":
            match = re.match(r"\{(\d+)(?:,(\d*))?\}", pattern[index:])
            if match is None or not frame[1]:
                raise ValueError("Unsupported bounded JSON-schema pattern repetition")
            minimum = int(match[1])
            maximum = int(match[2]) if match[2] else minimum
            if maximum < minimum or max(minimum, maximum) > GRAMMAR_REPEAT_LIMIT:
                raise ValueError("JSON-schema pattern repetition exceeds its grammar budget")
            frame[0] += frame[1] * (max(1, maximum) - 1)
            frame[1] *= max(1, maximum)
            index += len(match[0]) - 1
        elif character not in "^$?*+":
            frame[0] += 1
            frame[1] = 1
        if max(frame[0], frame[2]) > GRAMMAR_PATH_BUDGET:
            raise ValueError("Nested JSON-schema pattern repetition exceeds its grammar budget")
        index += 1
    if len(frames) != 1:
        raise ValueError("Unbalanced JSON-schema pattern")
    return max(1, frames[0][0], frames[0][2])


def _schema_grammar_budget(root: dict[str, Any]) -> None:
    """Reject small schemas that expand into huge grammar before starting native work."""
    total = 0

    def visit(node: Any, multiplier: int = 1, references: tuple[str, ...] = ()) -> None:
        nonlocal total
        if not isinstance(node, dict):
            return
        extent = 1
        for minimum_key, maximum_key in (("minLength", "maxLength"), ("minItems", "maxItems")):
            for key in (minimum_key, maximum_key):
                if key in node and (type(node[key]) is not int or not 0 <= node[key] <= GRAMMAR_REPEAT_LIMIT):
                    raise ValueError("JSON-schema repetition bounds must be integers between 0 and 64")
            minimum, maximum = node.get(minimum_key, 0), node.get(maximum_key)
            if maximum is not None and minimum > maximum:
                raise ValueError("JSON-schema minimum repetition exceeds maximum")
            extent = max(extent, maximum if maximum is not None else minimum)
        if "pattern" in node:
            extent = max(extent, _pattern_budget(node["pattern"]))
        multiplier *= extent
        if multiplier > GRAMMAR_PATH_BUDGET:
            raise ValueError("Nested JSON-schema repetition exceeds its grammar path budget")
        total += multiplier
        if total > GRAMMAR_TOTAL_BUDGET:
            raise ValueError("JSON-schema expansion exceeds its total grammar budget")
        for key in ("$ref", "$dynamicRef"):
            if key not in node:
                continue
            reference = node[key]
            if not isinstance(reference, str) or not reference.startswith("#/"):
                raise ValueError("Only local JSON-schema references are accepted; remote fetching is disabled")
            if reference in references or len(references) >= 16:
                raise ValueError("Recursive JSON-schema references are outside the finite grammar subset")
            target: Any = root
            try:
                for segment in reference[2:].split("/"):
                    target = target[segment.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError) as error:
                raise ValueError("Unresolved local JSON-schema reference") from error
            visit(target, multiplier, references + (reference,))
        for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
            children = node.get(key, {})
            if isinstance(children, dict):
                if len(children) > 32:
                    raise ValueError("JSON-schema property/definition fanout exceeds its grammar budget")
                for child in children.values():
                    visit(child, multiplier, references)
        for key in ("items", "prefixItems", "allOf", "anyOf", "oneOf", "additionalProperties", "contains", "not"):
            child = node.get(key)
            if isinstance(child, list):
                if len(child) > 32:
                    raise ValueError("JSON-schema tuple/alternative fanout exceeds its grammar budget")
                for item in child:
                    visit(item, multiplier, references)
            else:
                visit(child, multiplier, references)

    visit(root)


def _schema_grammar(native: Any, schema: dict[str, Any]) -> Any:
    """Budget Python-converted grammar before invoking the C/C++ grammar parser."""
    grammar = native.llama_grammar.json_schema_to_gbnf(json.dumps(schema, ensure_ascii=False))
    if len(grammar.encode("utf-8")) > 65_536 or len(grammar.splitlines()) > 256:
        raise ValueError("Converted JSON grammar exceeds its native parser budget")
    depth = 0
    quoted = character_class = escaped = False
    for character in grammar:
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif quoted:
            quoted = character != '"'
        elif character_class:
            character_class = character != "]"
        elif character == '"':
            quoted = True
        elif character == "[":
            character_class = True
        elif character == "(":
            depth += 1
            if depth > 64:
                raise ValueError("Converted JSON grammar exceeds native parser nesting budget")
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("Converted JSON grammar is unbalanced")
    if depth or quoted or character_class or escaped:
        raise ValueError("Converted JSON grammar has incomplete delimiters")
    return native.LlamaGrammar.from_string(grammar, verbose=False)


def _model_stamp(path: Path) -> tuple[int, int, int, int, int]:
    if path.is_symlink():
        raise ModelUnavailable("The local model must be an acquired regular file")
    try:
        value = path.stat()
    except OSError as error:
        raise ModelUnavailable("Pinned local model is missing; run the explicit acquisition command") from error
    if not stat.S_ISREG(value.st_mode) or value.st_size != MODEL_BYTES:
        raise ModelUnavailable("Local model has an unexpected file type or byte length")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _verify_model(path: Path, expected_stamp: tuple[int, int, int, int, int]) -> None:
    if _model_stamp(path) != expected_stamp:
        raise ModelUnavailable("Local model identity changed before verification")
    with path.open("rb") as stream:
        if stream.read(4) != b"GGUF":
            raise ModelUnavailable("Local artifact is not GGUF")
    if _sha256(path) != MODEL_SHA256 or _model_stamp(path) != expected_stamp:
        raise ModelUnavailable("Local model checksum or identity does not match the pinned official artifact")


def _complete(
    model: Any,
    messages: list[dict[str, str]],
    limits: dict[str, Any],
    context_tokens: int,
    grammar: Any = None,
) -> dict[str, Any]:
    """Count the exact ChatML token sequence; never truncate supplied evidence."""
    tokens = model.tokenize(_chatml(messages).encode("utf-8"), add_bos=False, special=True)
    if len(tokens) + limits["max_tokens"] > context_tokens:
        raise ContextLimitExceeded("Complete chat plus requested output exceeds the configured token context")
    model.reset()
    response = model.create_completion(
        prompt=tokens,
        max_tokens=limits["max_tokens"],
        temperature=limits["temperature"],
        top_p=1.0,
        min_p=0.0,
        top_k=0,
        repeat_penalty=1.0,
        stop=["<|im_end|>", "<|endoftext|>"],
        seed=limits["seed"],
        stream=False,
        echo=False,
        grammar=grammar,
    )
    choice = response["choices"][0]
    text = choice.get("text", "")
    if not isinstance(text, str):
        raise GenerationError("Local runtime returned an invalid text response")
    usage = response.get("usage", {})
    if usage.get("completion_tokens", 0) > limits["max_tokens"]:
        raise GenerationError("Local runtime exceeded the requested output-token budget")
    reason = choice.get("finish_reason", "stop")
    warnings: list[str] = []
    if len(text) > limits["max_output_chars"]:
        text = text[: limits["max_output_chars"]]
        reason = "character_limit"
        warnings.append("Local output reached the explicit character limit; it may be incomplete.")
    return {"text": text, "finish_reason": reason, "usage": usage, "warnings": warnings}


def _worker_main(connection: Connection, settings: dict[str, Any]) -> None:
    """One spawned native worker, terminated by the parent on a hard deadline."""
    model: Any = None
    try:
        path = Path(settings["model_path"])
        expected = tuple(settings["model_stamp"])
        _verify_model(path, expected)
        llama_cpp = importlib.import_module("llama_cpp")

        if llama_cpp.__version__ != RUNTIME_VERSION:
            raise ModelUnavailable("Optional local runtime does not match the pinned package version")

        model = llama_cpp.Llama(
            model_path=str(path),
            n_ctx=settings["context_tokens"],
            n_threads=settings["n_threads"],
            n_threads_batch=settings["n_threads"],
            n_gpu_layers=settings["gpu_layers"],
            n_batch=256,
            n_ubatch=128,
            use_mmap=True,
            use_mlock=False,
            verbose=False,
        )
        template = model.metadata.get("tokenizer.chat_template", "")
        if "<|im_start|>" not in template or "<|im_end|>" not in template:
            raise ModelUnavailable("Pinned model does not advertise the expected ChatML template")
        if _model_stamp(path) != expected:
            raise ModelUnavailable("Local model changed during loading")
        backend = {
            "gpu_offload_supported": bool(llama_cpp.llama_supports_gpu_offload()),
            "gpu_layers": settings["gpu_layers"],
            "runtime_version": RUNTIME_VERSION,
        }
        while True:
            request = connection.recv()
            if request is None:
                return
            try:
                schema = request.get("response_schema")
                grammar = _schema_grammar(llama_cpp, schema) if schema is not None else None
                result = _complete(model, request["messages"], request["limits"], settings["context_tokens"], grammar)
                connection.send({"ok": True, "result": result | {"backend": backend}})
            except ContextLimitExceeded:
                connection.send({"ok": False, "error": "context_limit"})
            except Exception:
                # Never send a prompt, native exception string or private context
                # to logs or a caller; kill/reload after a runtime failure.
                connection.send({"ok": False, "error": "generation_failed"})
                return
    except (EOFError, BrokenPipeError):
        return
    except Exception:
        try:
            connection.send({"ok": False, "error": "runtime_unavailable"})
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        if model is not None:
            model.close()
        connection.close()


class LocalGenerator:
    """A reusable single-worker runtime; keep one instance per application process."""

    def __init__(
        self,
        model_path: Path | None = None,
        *,
        context_tokens: int = 4096,
        n_threads: int = 4,
        gpu_layers: int = -1,
    ) -> None:
        if type(context_tokens) is not int or not 512 <= context_tokens <= 8192:
            raise ValueError("context_tokens must be an integer between 512 and 8192")
        if type(n_threads) is not int or not 1 <= n_threads <= 8:
            raise ValueError("n_threads must be an integer between 1 and 8")
        if type(gpu_layers) is not int or gpu_layers not in {-1, 0}:
            raise ValueError("gpu_layers must be -1 (offload) or 0 (CPU)")
        configured = model_path or Path(os.getenv("BOOKRAG_GENERATION_MODEL", DEFAULT_MODEL_DIRECTORY / MODEL_FILENAME))
        self.model_path = Path(os.path.abspath(configured))
        self.context_tokens = context_tokens
        self.n_threads = n_threads
        self.gpu_layers = gpu_layers
        self._lock = threading.Lock()
        self._process: Any = None
        self._connection: Connection | None = None
        self._stamp: tuple[int, int, int, int, int] | None = None

    @property
    def worker_pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def _stop_worker(self) -> None:
        process, connection = self._process, self._connection
        self._process = self._connection = self._stamp = None
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            if process.is_alive():
                self._process = process
                raise GenerationError(
                    "Local worker did not exit after bounded termination; another worker was not started"
                )
            process.close()
        if connection is not None:
            connection.close()

    def _start_worker(self, stamp: tuple[int, int, int, int, int]) -> None:
        self._stop_worker()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        settings = {
            "model_path": str(self.model_path),
            "model_stamp": stamp,
            "context_tokens": self.context_tokens,
            "n_threads": self.n_threads,
            "gpu_layers": self.gpu_layers,
        }
        process = context.Process(target=_worker_main, args=(child, settings), daemon=True)
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            raise
        child.close()
        self._process, self._connection, self._stamp = process, parent, stamp

    def generate(
        self,
        messages: Sequence[Mapping[str, str]],
        limits: GenerationLimits | None = None,
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generate offline with a finite deadline; timeout discards partial output."""
        started = time.monotonic()
        limits = GenerationLimits() if limits is None else limits
        if not isinstance(limits, GenerationLimits):
            raise TypeError("limits must be GenerationLimits")
        clean = _messages(messages)
        schema = _response_schema(response_schema)
        deadline = started + limits.timeout_seconds
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise GenerationTimeout("Local generation request expired while waiting for the single runtime")
        try:
            stamp = _model_stamp(self.model_path)
            if self._process is None or not self._process.is_alive() or self._stamp != stamp:
                self._start_worker(stamp)
            connection = self._connection
            assert connection is not None
            sent = threading.Event()
            send_errors: list[Exception] = []

            def send_request() -> None:
                try:
                    connection.send({"messages": clean, "limits": asdict(limits), "response_schema": schema})
                except Exception as error:
                    send_errors.append(error)
                finally:
                    sent.set()

            # A cold native worker has not reached recv() yet. Large prompt IPC
            # can itself block, so its send is covered by the same hard deadline.
            sender = threading.Thread(target=send_request, daemon=True, name="book-rag-local-generation-send")
            sender.start()
            if not sent.wait(max(0, deadline - time.monotonic())):
                self._stop_worker()
                sender.join(timeout=0.2)
                raise GenerationTimeout("Local generation expired during worker startup or prompt dispatch")
            if send_errors:
                raise OSError("Local generation request channel failed") from None
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not connection.poll(remaining):
                self._stop_worker()
                raise GenerationTimeout("Local generation exceeded its deadline; worker terminated, no partial answer")
            response = connection.recv()
            if time.monotonic() > deadline:
                self._stop_worker()
                raise GenerationTimeout("Local generation finished after its request deadline")
            if _model_stamp(self.model_path) != stamp:
                self._stop_worker()
                raise ModelUnavailable("Local model identity changed during generation")
            if not isinstance(response, dict) or not response.get("ok"):
                error = response.get("error") if isinstance(response, dict) else None
                if error == "context_limit":
                    raise ContextLimitExceeded(
                        "Complete chat plus requested output exceeds the configured token context"
                    )
                self._stop_worker()
                if error == "runtime_unavailable":
                    raise ModelUnavailable("Pinned model or optional local runtime could not be loaded")
                raise GenerationError("Local generation failed; no unverified partial output returned")
            result = response.get("result")
            if not isinstance(result, dict):
                self._stop_worker()
                raise GenerationError("Local runtime returned an invalid bounded response")
            return result | {
                "model": {
                    "repository": MODEL_REPOSITORY,
                    "revision": MODEL_REVISION,
                    "sha256": MODEL_SHA256,
                    "quantization": "Q4_K_M",
                },
                "elapsed_ms": (time.monotonic() - started) * 1000,
            }
        except ModelUnavailable:
            self._stop_worker()
            raise
        except (EOFError, BrokenPipeError, OSError) as error:
            self._stop_worker()
            raise ModelUnavailable("Local generation worker exited or its local channel became unavailable") from error
        finally:
            self._lock.release()

    def close(self, timeout_seconds: float = 5) -> None:
        """Release an owned worker; do not wait indefinitely behind another request."""
        if not 0 < timeout_seconds <= 120:
            raise ValueError("close timeout must be between 0 and 120 seconds")
        if not self._lock.acquire(timeout=timeout_seconds):
            raise GenerationTimeout("Local generator is still serving a bounded request")
        try:
            self._stop_worker()
        finally:
            self._lock.release()


_DEFAULT_LOCK = threading.Lock()
_DEFAULT_GENERATOR: LocalGenerator | None = None


def generate(
    messages: Sequence[Mapping[str, str]],
    limits: GenerationLimits | None = None,
    *,
    response_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Use one lazily created local runtime; this never acquires models over a network."""
    global _DEFAULT_GENERATOR
    with _DEFAULT_LOCK:
        if _DEFAULT_GENERATOR is None:
            _DEFAULT_GENERATOR = LocalGenerator(
                context_tokens=int(os.getenv("BOOKRAG_GENERATION_CONTEXT", "4096")),
                n_threads=int(os.getenv("BOOKRAG_GENERATION_THREADS", "4")),
                gpu_layers=int(os.getenv("BOOKRAG_GENERATION_GPU_LAYERS", "-1")),
            )
        generator = _DEFAULT_GENERATOR
    return generator.generate(messages, limits, response_schema=response_schema)


def capabilities(model_path: Path | None = None) -> dict[str, Any]:
    """Cheap readiness hints; actual checksum verification happens in a bounded worker."""
    configured = model_path or Path(os.getenv("BOOKRAG_GENERATION_MODEL", DEFAULT_MODEL_DIRECTORY / MODEL_FILENAME))
    present = False
    try:
        _model_stamp(configured)
        present = True
    except ModelUnavailable:
        pass
    return {
        "runtime_installed": importlib.util.find_spec("llama_cpp") is not None,
        "model_present": present,
        "verification": "full pinned SHA-256 on worker load",
        "repository": MODEL_REPOSITORY,
        "revision": MODEL_REVISION,
        "quantization": "Q4_K_M",
        "network_inference": False,
    }


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _acquisition_lock(directory: Path) -> Generator[None]:
    # A kernel lock survives neither SIGKILL nor a crash; the harmless file may
    # remain. Never unlink it, which could create two separately locked inodes.
    import fcntl

    lock = directory / ".acquire.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ModelUnavailable("Acquisition lock must be an owned regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ModelUnavailable("Another explicit local-model acquisition is active") from error
        os.ftruncate(descriptor, 0)
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(descriptor)


def _download_partial(
    directory: Path,
    partial: Path,
    min_free_bytes: int,
    reserve_bytes: int,
    timeout_seconds: float,
) -> None:
    if partial.is_symlink() or (partial.exists() and partial.stat().st_size > MODEL_BYTES):
        raise ModelUnavailable("Unsafe or oversized owned partial; inspect it before an explicit retry")
    offset = partial.stat().st_size if partial.exists() else 0
    _disk_guard(directory, MODEL_BYTES - offset, min_free_bytes, reserve_bytes)
    if offset < MODEL_BYTES:
        request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "book-rag-local-model/1"})
        if offset:
            request.add_header("Range", f"bytes={offset}-")
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            resumed = offset > 0 and response.status == 206
            if resumed:
                expected_range = f"bytes {offset}-{MODEL_BYTES - 1}/{MODEL_BYTES}"
                if response.headers.get("Content-Range") != expected_range:
                    raise ModelUnavailable("Model server returned an unexpected resume range")
            elif offset:
                offset = 0
                _disk_guard(directory, MODEL_BYTES, min_free_bytes, reserve_bytes)
            with partial.open("ab" if resumed else "wb") as stream:
                while block := response.read(1024 * 1024):
                    if offset + len(block) > MODEL_BYTES:
                        raise ModelUnavailable("Model response exceeded the pinned byte length")
                    _disk_guard(directory, MODEL_BYTES - offset, min_free_bytes, reserve_bytes)
                    stream.write(block)
                    offset += len(block)
                stream.flush()
                os.fsync(stream.fileno())
    if offset != MODEL_BYTES or _sha256(partial) != MODEL_SHA256:
        raise ModelUnavailable("Downloaded model length or SHA-256 failed; partial retained for inspection")
    with partial.open("rb") as stream:
        if stream.read(4) != b"GGUF":
            raise ModelUnavailable("Downloaded artifact is not GGUF")


def _disk_guard(directory: Path, remaining_bytes: int, min_free_bytes: int, reserve_bytes: int) -> int:
    free = shutil.disk_usage(directory).free
    if free - remaining_bytes - reserve_bytes < min_free_bytes:
        raise ModelUnavailable("Model acquisition would breach the disk floor plus declared concurrent-work reserve")
    return free


def acquire_model(
    directory: Path = DEFAULT_MODEL_DIRECTORY,
    *,
    min_free_bytes: int = 4 * GIB,
    reserve_bytes: int = 3 * GIB,
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    """Resume a pinned official download, verifying SHA before atomic publication.

    Inference never calls this function implicitly. A partial is owned and retained
    for a later explicit retry. No global Hugging Face cache or second model copy
    is created. Concurrent acquisitions of the same directory are rejected.
    """
    if type(min_free_bytes) is not int or min_free_bytes < 4 * GIB:
        raise ValueError("min_free_bytes must preserve at least 4 GiB")
    if type(reserve_bytes) is not int or reserve_bytes < 0:
        raise ValueError("reserve_bytes must be a nonnegative integer")
    if (
        type(timeout_seconds) not in {int, float}
        or not math.isfinite(timeout_seconds)
        or not 1 <= timeout_seconds <= 300
    ):
        raise ValueError("timeout_seconds must be between 1 and 300")
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / MODEL_FILENAME
    partial = target.with_suffix(target.suffix + ".part")
    with _acquisition_lock(directory):
        _disk_guard(directory, 0, min_free_bytes, reserve_bytes)
        if target.exists():
            if target.is_symlink() or target.stat().st_size != MODEL_BYTES or _sha256(target) != MODEL_SHA256:
                raise ModelUnavailable("Existing model is not the pinned official artifact; it was not overwritten")
        else:
            _download_partial(directory, partial, min_free_bytes, reserve_bytes, timeout_seconds)
            # A second process cannot acquire our directory while the lock exists.
            # Publish without replacing an independently existing acquired model.
            os.link(partial, target)
            partial.unlink()
        license_path = directory / "Qwen2.5-1.5B-Instruct.LICENSE"
        if license_path.is_symlink():
            raise ModelUnavailable("Official license path must be an owned regular file")
        if not license_path.exists():
            with urllib.request.urlopen(LICENSE_URL, timeout=timeout_seconds) as response:
                license_bytes = response.read(64 * 1024 + 1)
            if len(license_bytes) > 64 * 1024 or b"Apache License" not in license_bytes:
                raise ModelUnavailable("Official pinned license could not be verified")
            license_path.write_bytes(license_bytes)
        if license_path.stat().st_size > 64 * 1024 or _sha256(license_path) != LICENSE_SHA256:
            raise ModelUnavailable("Pinned official license checksum failed")
        receipt = {
            "version": 1,
            "repository": MODEL_REPOSITORY,
            "revision": MODEL_REVISION,
            "filename": MODEL_FILENAME,
            "sha256": MODEL_SHA256,
            "bytes": MODEL_BYTES,
            "license": MODEL_LICENSE,
            "source_url": MODEL_URL,
            "license_url": LICENSE_URL,
            "license_sha256": _sha256(license_path),
            "acquired_utc": datetime.now(UTC).isoformat(),
            "min_free_bytes": min_free_bytes,
            "concurrent_work_reserve_bytes": reserve_bytes,
            "free_bytes_after": shutil.disk_usage(directory).free,
        }
        _write_json(directory / "receipt.json", receipt)
        return receipt
