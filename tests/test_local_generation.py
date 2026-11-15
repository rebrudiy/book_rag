"""Bounded runtime and acquisition checks use invented data, never real books."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from book_rag import local_generation as generation


class FakeModel:
    def __init__(self, token_count: int = 8, text: str = "Invented response", completion_tokens: int = 2) -> None:
        self.token_count = token_count
        self.text = text
        self.completion_tokens = completion_tokens
        self.reset_calls = 0
        self.calls: list[dict[str, Any]] = []
        self.prompt: bytes = b""

    def tokenize(self, text: bytes, **kwargs: Any) -> list[int]:
        self.prompt = text
        assert kwargs == {"add_bos": False, "special": True}
        return list(range(self.token_count))

    def reset(self) -> None:
        self.reset_calls += 1

    def create_completion(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "choices": [{"text": self.text, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": self.token_count, "completion_tokens": self.completion_tokens},
        }


def fake_worker(connection: Any, settings: dict[str, Any]) -> None:
    """A real spawned process with no native/model imports or network calls."""
    try:
        while request := connection.recv():
            content = request["messages"][-1]["content"]
            if content == "wait":
                time.sleep(5)
            if content == "exit":
                return
            if content == "context":
                connection.send({"ok": False, "error": "context_limit"})
                continue
            if content == "slow":
                time.sleep(0.15)
            connection.send(
                {
                    "ok": True,
                    "result": {
                        "text": content,
                        "finish_reason": "stop",
                        "usage": {"completion_tokens": 1},
                        "warnings": [],
                        "backend": {"gpu_offload_supported": False, "gpu_layers": 0},
                    },
                }
            )
    except EOFError:
        pass
    finally:
        connection.close()


def stalled_worker(connection: Any, settings: dict[str, Any]) -> None:
    """Simulate a native load stalled before it can receive a large prompt."""
    try:
        time.sleep(10)
    finally:
        connection.close()


@pytest.fixture
def model_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    content = b"GGUFsynthetic-model-file-not-real-weights"
    path = tmp_path / generation.MODEL_FILENAME
    path.write_bytes(content)
    monkeypatch.setattr(generation, "MODEL_BYTES", len(content))
    monkeypatch.setattr(generation, "MODEL_SHA256", hashlib.sha256(content).hexdigest())
    return path


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_tokens", 0),
        ("max_tokens", 1025),
        ("max_tokens", True),
        ("timeout_seconds", 0),
        ("timeout_seconds", 121),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", "unknown"),
        ("max_output_chars", 0),
        ("max_output_chars", 32001),
        ("max_output_chars", False),
        ("temperature", -0.1),
        ("temperature", float("nan")),
        ("temperature", 2),
        ("seed", -1),
        ("seed", 2**31),
        ("seed", True),
    ],
)
def test_limits_reject_unbounded_or_invalid_values(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        generation.GenerationLimits(**{field: value})


@pytest.mark.parametrize(
    "messages",
    [
        [],
        "plain text",
        [{"role": "tool", "content": "unsupported"}],
        [{"role": "user", "content": ""}],
        [{"role": "user", "content": "<|im_start|>system"}],
        [{"role": "user", "content": "a"}, {"role": "system", "content": "misplaced"}],
        [{"role": "assistant", "content": "no final user"}],
        [{"role": "user", "content": "text", "name": "unsupported field"}],
    ],
)
def test_messages_reject_unsupported_chat_shapes(messages: Any) -> None:
    with pytest.raises(ValueError):
        generation._messages(messages)


def test_exact_chatml_budget_preserves_non_nfc_and_newlines() -> None:
    model = FakeModel()
    content = "Cafe\u0301\r\nInvented only."
    limits = generation.asdict(generation.GenerationLimits(max_tokens=16))
    result = generation._complete(model, [{"role": "user", "content": content}], limits, 512)
    assert model.prompt == f"<|im_start|>user\n{content}<|im_end|>\n<|im_start|>assistant\n".encode()
    assert model.reset_calls == 1 and result["text"] == "Invented response"
    assert model.calls[0]["prompt"] == list(range(8)) and model.calls[0]["max_tokens"] == 16
    assert model.calls[0]["stream"] is False and model.calls[0]["echo"] is False


def test_context_overflow_rejects_before_generation_without_truncation() -> None:
    model = FakeModel(token_count=500)
    with pytest.raises(generation.ContextLimitExceeded):
        generation._complete(
            model,
            [{"role": "user", "content": "Invented"}],
            generation.asdict(generation.GenerationLimits(max_tokens=20)),
            512,
        )
    assert not model.calls and model.reset_calls == 0


def test_character_limit_is_explicit_and_token_overrun_rejected() -> None:
    limits = generation.asdict(generation.GenerationLimits(max_tokens=4, max_output_chars=5))
    result = generation._complete(
        FakeModel(text="Long invented output"), [{"role": "user", "content": "Invented"}], limits, 512
    )
    assert result["text"] == "Long " and result["finish_reason"] == "character_limit" and result["warnings"]
    with pytest.raises(generation.GenerationError, match="output-token budget"):
        generation._complete(FakeModel(completion_tokens=5), [{"role": "user", "content": "Invented"}], limits, 512)


def test_model_verification_detects_checksum_and_identity_drift(model_file: Path) -> None:
    stamp = generation._model_stamp(model_file)
    generation._verify_model(model_file, stamp)
    original = model_file.read_bytes()
    model_file.write_bytes(original[:-1] + b"x")
    with pytest.raises(generation.ModelUnavailable):
        generation._verify_model(model_file, stamp)
    with pytest.raises(generation.ModelUnavailable, match="checksum"):
        generation._verify_model(model_file, generation._model_stamp(model_file))


def test_fake_spawned_worker_reused_and_thread_responses_never_cross(
    model_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(generation, "_worker_main", fake_worker)
    generator = generation.LocalGenerator(model_file, gpu_layers=0)
    try:

        def request(content: str) -> dict[str, Any]:
            return generator.generate(
                [{"role": "user", "content": content}], generation.GenerationLimits(timeout_seconds=5)
            )

        assert request("first")["text"] == "first"
        pid = generator.worker_pid
        with ThreadPoolExecutor(max_workers=2) as executor:
            values = list(executor.map(request, ["slow", "second"]))
        assert [value["text"] for value in values] == ["slow", "second"]
        assert generator.worker_pid == pid
        with pytest.raises(generation.ContextLimitExceeded):
            request("context")
        assert request("after-context")["text"] == "after-context" and generator.worker_pid == pid
    finally:
        generator.close()
    assert generator.worker_pid is None


def test_timeout_kills_owned_worker_and_next_request_recovers(
    model_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(generation, "_worker_main", fake_worker)
    generator = generation.LocalGenerator(model_file, gpu_layers=0)
    try:
        generator.generate([{"role": "user", "content": "ready"}])
        old_pid = generator.worker_pid
        started = time.monotonic()
        with pytest.raises(generation.GenerationTimeout):
            generator.generate([{"role": "user", "content": "wait"}], generation.GenerationLimits(timeout_seconds=1))
        assert time.monotonic() - started < 4 and generator.worker_pid is None
        assert old_pid is not None
        with pytest.raises(ProcessLookupError):
            os.kill(old_pid, 0)
        result = generator.generate([{"role": "user", "content": "recovered"}])
        assert result["text"] == "recovered" and generator.worker_pid != old_pid
    finally:
        generator.close()


def test_cold_start_large_prompt_dispatch_is_also_deadline_bounded(
    model_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(generation, "_worker_main", stalled_worker)
    generator = generation.LocalGenerator(model_file, gpu_layers=0)
    started = time.monotonic()
    with pytest.raises(generation.GenerationTimeout):
        generator.generate([{"role": "user", "content": "Ж" * 32000}], generation.GenerationLimits(timeout_seconds=1))
    assert time.monotonic() - started < 4 and generator.worker_pid is None
    generator.close()


def test_lock_wait_timeout_does_not_kill_another_owned_request_worker(
    model_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(generation, "_worker_main", fake_worker)
    generator = generation.LocalGenerator(model_file, gpu_layers=0)
    held, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        with generator._lock:
            held.set()
            release.wait(4)

    try:
        generator.generate([{"role": "user", "content": "ready"}])
        pid = generator.worker_pid
        thread = threading.Thread(target=hold_lock)
        thread.start()
        assert held.wait(1)
        with pytest.raises(generation.GenerationTimeout, match="waiting"):
            generator.generate(
                [{"role": "user", "content": "never sent"}], generation.GenerationLimits(timeout_seconds=1)
            )
        assert generator.worker_pid == pid
        release.set()
        thread.join(timeout=2)
        assert generator.generate([{"role": "user", "content": "still healthy"}])["text"] == "still healthy"
    finally:
        release.set()
        generator.close()


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200, headers: dict[str, str] | None = None) -> None:
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


@pytest.fixture
def acquisition(monkeypatch: pytest.MonkeyPatch) -> tuple[bytes, bytes]:
    data, license_bytes = b"GGUF" + b"invented-model-bytes" * 200, b"Apache License; synthetic test license only."
    monkeypatch.setattr(generation, "MODEL_BYTES", len(data))
    monkeypatch.setattr(generation, "MODEL_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(generation, "LICENSE_SHA256", hashlib.sha256(license_bytes).hexdigest())
    monkeypatch.setattr(generation.shutil, "disk_usage", lambda _: SimpleNamespace(free=20 * generation.GIB))
    return data, license_bytes


def test_acquisition_resumes_exact_range_and_publishes_verified_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acquisition: tuple[bytes, bytes]
) -> None:
    data, license_bytes = acquisition
    partial = tmp_path / (generation.MODEL_FILENAME + ".part")
    partial.write_bytes(data[:37])
    requests: list[Any] = []

    def urlopen(request: Any, **kwargs: Any) -> FakeResponse:
        requests.append(request)
        if isinstance(request, str):
            return FakeResponse(license_bytes)
        assert request.get_header("Range") == "bytes=37-"
        return FakeResponse(data[37:], 206, {"Content-Range": f"bytes 37-{len(data) - 1}/{len(data)}"})

    monkeypatch.setattr(generation.urllib.request, "urlopen", urlopen)
    receipt = generation.acquire_model(tmp_path)
    target = tmp_path / generation.MODEL_FILENAME
    assert target.read_bytes() == data and not partial.exists()
    assert receipt["sha256"] == hashlib.sha256(data).hexdigest()
    assert json.loads((tmp_path / "receipt.json").read_text())["bytes"] == len(data)
    assert len(requests) == 2
    monkeypatch.setattr(
        generation.urllib.request, "urlopen", lambda *a, **k: pytest.fail("No repeat network acquisition")
    )
    assert generation.acquire_model(tmp_path)["sha256"] == receipt["sha256"]


def test_complete_partial_after_interruption_needs_no_model_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acquisition: tuple[bytes, bytes]
) -> None:
    data, license_bytes = acquisition
    (tmp_path / (generation.MODEL_FILENAME + ".part")).write_bytes(data)

    def license_only(url: Any, **kwargs: Any) -> FakeResponse:
        assert isinstance(url, str) and url == generation.LICENSE_URL
        return FakeResponse(license_bytes)

    monkeypatch.setattr(generation.urllib.request, "urlopen", license_only)
    assert generation.acquire_model(tmp_path)["bytes"] == len(data)


def test_disk_floor_and_concurrent_lock_fail_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acquisition: tuple[bytes, bytes]
) -> None:
    monkeypatch.setattr(generation.urllib.request, "urlopen", lambda *a, **k: pytest.fail("No network allowed"))
    monkeypatch.setattr(generation.shutil, "disk_usage", lambda _: SimpleNamespace(free=6 * generation.GIB))
    with pytest.raises(generation.ModelUnavailable, match="disk floor"):
        generation.acquire_model(tmp_path)
    monkeypatch.setattr(generation.shutil, "disk_usage", lambda _: SimpleNamespace(free=20 * generation.GIB))
    with generation._acquisition_lock(tmp_path), pytest.raises(generation.ModelUnavailable, match="active"):
        generation.acquire_model(tmp_path)


def test_existing_corrupt_artifact_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acquisition: tuple[bytes, bytes]
) -> None:
    data, _ = acquisition
    path = tmp_path / generation.MODEL_FILENAME
    path.write_bytes(b"wrong" + data[5:])
    before = path.read_bytes()
    monkeypatch.setattr(generation.urllib.request, "urlopen", lambda *a, **k: pytest.fail("No replacement allowed"))
    with pytest.raises(generation.ModelUnavailable, match="not overwritten"):
        generation.acquire_model(tmp_path)
    assert path.read_bytes() == before


def test_checksum_failure_retains_partial_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acquisition: tuple[bytes, bytes]
) -> None:
    data, _ = acquisition
    monkeypatch.setattr(generation.urllib.request, "urlopen", lambda *a, **k: FakeResponse(data[:-1] + b"x"))
    with pytest.raises(generation.ModelUnavailable, match="SHA-256"):
        generation.acquire_model(tmp_path)
    assert not (tmp_path / generation.MODEL_FILENAME).exists()
    assert (tmp_path / (generation.MODEL_FILENAME + ".part")).exists()


def test_receipt_failed_publication_cleans_only_owned_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text("old receipt")
    unrelated = tmp_path / "unrelated.tmp"
    unrelated.write_text("owned sentinel")

    def fail_replace(*args: Any) -> None:
        raise OSError("Synthetic publish failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError):
        generation._write_json(receipt, {"new": True})
    assert receipt.read_text() == "old receipt" and unrelated.read_text() == "owned sentinel"
    assert set(tmp_path.iterdir()) == {receipt, unrelated}


@pytest.mark.parametrize(
    "schema",
    [
        [],
        {"$ref": "https://example.invalid/schema.json"},
        {"$dynamicRef": "file:///private/secret"},
        {"description": "x" * 17000},
        {"const": float("nan")},
    ],
)
def test_response_schema_is_bounded_and_cannot_fetch_remote_content(schema: Any) -> None:
    with pytest.raises(ValueError):
        generation._response_schema(schema)


def test_schema_grammar_is_passed_without_importing_native_runtime() -> None:
    model = FakeModel(text='{"synthetic":true}')
    marker = object()
    result = generation._complete(
        model,
        [{"role": "user", "content": "Invented data"}],
        generation.asdict(generation.GenerationLimits(max_tokens=16)),
        512,
        marker,
    )
    assert model.calls[0]["grammar"] is marker and json.loads(result["text"]) == {"synthetic": True}
    schema = {"type": "object", "$defs": {"sample": {"type": "string"}}, "$ref": "#/$defs/sample"}
    assert generation._response_schema(schema) == schema


def test_missing_optional_native_package_fails_lazily_without_network(
    model_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Channel:
        def __init__(self) -> None:
            self.responses: list[Any] = []
            self.closed = False

        def send(self, value: Any) -> None:
            self.responses.append(value)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setitem(sys.modules, "llama_cpp", None)
    monkeypatch.setattr(
        generation.urllib.request, "urlopen", lambda *a, **k: pytest.fail("Inference must stay offline")
    )
    channel = Channel()
    generation._worker_main(
        cast(generation.Connection, channel),
        {"model_path": str(model_file), "model_stamp": generation._model_stamp(model_file)},
    )
    assert channel.responses == [{"ok": False, "error": "runtime_unavailable"}] and channel.closed


def test_missing_model_does_not_implicitly_acquire_weights(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generation, "acquire_model", lambda *a, **k: pytest.fail("No implicit download"))
    generator = generation.LocalGenerator(tmp_path / "absent.gguf")
    with pytest.raises(generation.ModelUnavailable, match="missing"):
        generator.generate([{"role": "user", "content": "Invented question"}])
    assert generator.worker_pid is None


def test_linux_extra_uses_portable_pypi_source_not_a_macos_wheel() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    extra = project["project"]["optional-dependencies"]["generation"]
    assert extra == [f"llama-cpp-python=={generation.RUNTIME_VERSION}"]
    lock = tomllib.loads((root / "uv.lock").read_text())
    package = next(item for item in lock["package"] if item["name"] == "llama-cpp-python")
    assert package["source"] == {"registry": "https://pypi.org/simple"}
    assert package["sdist"]["url"].endswith(".tar.gz")
    assert not any("macosx" in item["url"] for item in package.get("wheels", []))


def test_interrupted_download_releases_lock_and_resumes_owned_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acquisition: tuple[bytes, bytes]
) -> None:
    data, license_bytes = acquisition

    class Interrupted(FakeResponse):
        def read(self, size: int | None = -1, /) -> bytes:
            if self.tell():
                raise ConnectionResetError("Synthetic download interruption")
            return super().read(47)

    monkeypatch.setattr(generation.urllib.request, "urlopen", lambda *a, **k: Interrupted(data))
    with pytest.raises(ConnectionResetError):
        generation.acquire_model(tmp_path)
    partial = tmp_path / (generation.MODEL_FILENAME + ".part")
    assert partial.read_bytes() == data[:47] and not (tmp_path / generation.MODEL_FILENAME).exists()

    def resumed(url: Any, **kwargs: Any) -> FakeResponse:
        if isinstance(url, str):
            return FakeResponse(license_bytes)
        assert url.get_header("Range") == "bytes=47-"
        return FakeResponse(data[47:], 206, {"Content-Range": f"bytes 47-{len(data) - 1}/{len(data)}"})

    monkeypatch.setattr(generation.urllib.request, "urlopen", resumed)
    generation.acquire_model(tmp_path)
    assert (tmp_path / generation.MODEL_FILENAME).read_bytes() == data and not partial.exists()


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "maxLength": 2000},
        {"type": "string", "minLength": 2000},
        {"type": "array", "items": {"type": "integer"}, "maxItems": 2000},
        {"type": "array", "items": {"type": "integer"}, "minItems": 2000},
        {"type": "string", "maxLength": 65},
        {"type": "string", "maxLength": True},
        {"type": "array", "items": {"type": "integer"}, "minItems": -1},
        {"type": "string", "minLength": 5, "maxLength": 3},
        {"type": "string", "pattern": "^[a-z]{0,2000}$"},
        {"type": "string", "pattern": "^([a-z]{32}){16}$"},
        {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 32}},
        {
            "type": "array",
            "maxItems": 16,
            "items": {"$ref": "#/$defs/value"},
            "$defs": {"value": {"type": "string", "maxLength": 32}},
        },
        {"$defs": {"value": {"$ref": "#/$defs/value"}}, "$ref": "#/$defs/value"},
    ],
)
def test_pathological_schema_rejected_before_model_or_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: dict[str, Any]
) -> None:
    generator = generation.LocalGenerator(tmp_path / "model-must-not-be-opened.gguf")
    monkeypatch.setattr(generator, "_start_worker", lambda *args: pytest.fail("No native worker for unsafe grammar"))
    with pytest.raises(ValueError):
        generator.generate([{"role": "user", "content": "Synthetic request"}], response_schema=schema)
    assert generator.worker_pid is None


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "minLength": 1, "maxLength": 64},
        {"type": "array", "minItems": 0, "maxItems": 4, "items": {"type": "string"}},
        {"type": "string", "pattern": "^S[1-9][0-9]{0,2}$"},
        {"type": "string", "pattern": "^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"},
        {"type": "object", "properties": {"maxLength": {"type": "integer"}}, "additionalProperties": False},
        {
            "type": "object",
            "properties": {"items": {"$ref": "#/$defs/list"}},
            "additionalProperties": False,
            "$defs": {"list": {"type": "array", "maxItems": 4, "items": {"type": "string"}}},
        },
    ],
)
def test_routine_small_schema_constraints_remain_supported(schema: dict[str, Any]) -> None:
    assert generation._response_schema(schema) == schema


def test_many_individually_small_repetitions_share_a_total_budget() -> None:
    schema = {
        "type": "object",
        "properties": {f"field{index}": {"type": "string", "maxLength": 64} for index in range(20)},
    }
    with pytest.raises(ValueError, match="total grammar budget"):
        generation._response_schema(schema)


def test_two_records_with_two_bounded_quotes_fit_the_supported_path_budget() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "records": {
                "type": "array",
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "text": {"type": "string", "minLength": 1},
                        "quotes": {
                            "type": "array",
                            "maxItems": 2,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "quote": {"type": "string", "minLength": 1, "maxLength": 64},
                                    "source": {"type": "string", "enum": ["A", "B"]},
                                },
                                "required": ["quote", "source"],
                            },
                        },
                    },
                    "required": ["text", "quotes"],
                },
            },
        },
        "required": ["records"],
    }
    assert generation._response_schema(schema) == schema


@pytest.mark.parametrize(
    "grammar",
    [
        "root ::= " + "(" * 65 + '"x"' + ")?" * 65,
        'root ::= "' + "x" * 66_000 + '"',
        "\n".join(f'line{index} ::= "x"' for index in range(257)),
        'root ::= "unterminated',
    ],
)
def test_generated_grammar_limits_precede_native_parser(grammar: str) -> None:
    calls: list[Any] = []
    native = SimpleNamespace(
        llama_grammar=SimpleNamespace(json_schema_to_gbnf=lambda schema: grammar),
        LlamaGrammar=SimpleNamespace(from_string=lambda *a, **k: calls.append(a)),
    )
    with pytest.raises(ValueError):
        generation._schema_grammar(native, {"type": "string"})
    assert not calls, "Unsafe generated grammar reached the native parser"


def test_grammar_budget_ignores_parentheses_in_literals_and_character_classes() -> None:
    grammar = r'root ::= "(" [()] "\"" ")"'
    calls: list[Any] = []
    marker = object()

    def native_parser(value: str, *, verbose: bool) -> object:
        calls.append((value, verbose))
        return marker

    native = SimpleNamespace(
        llama_grammar=SimpleNamespace(json_schema_to_gbnf=lambda schema: grammar),
        LlamaGrammar=SimpleNamespace(from_string=native_parser),
    )
    assert generation._schema_grammar(native, {"type": "string"}) is marker
    assert calls == [(grammar, False)]
