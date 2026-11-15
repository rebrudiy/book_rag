"""No real weights/network/torch needed for NLI pin, quota and boundary tests."""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from book_rag import nli_validation as nli


@pytest.fixture
def tiny_pins(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    config = json.dumps(
        {"id2label": {str(k): v for k, v in nli.LABELS.items()}, "max_position_embeddings": 512}
    ).encode()
    header = json.dumps({"weight": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]}}).encode()
    contents = {"config.json": config, "model.safetensors": struct.pack("<Q", len(header)) + header + b"\x00\x00"}
    monkeypatch.setattr(
        nli,
        "PINS",
        tuple(nli.FilePin(name, len(body), hashlib.sha256(body).hexdigest()) for name, body in contents.items()),
    )
    monkeypatch.setattr(nli.shutil, "disk_usage", lambda directory: SimpleNamespace(free=10 * nli.GIB))
    return contents


def mock_http(monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]) -> list[httpx.Request]:
    calls = []
    original = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.scheme == "https" and nli.REVISION in str(request.url)
        return responses.pop(0)

    monkeypatch.setattr(
        nli.httpx, "Client", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler))
    )
    return calls


def test_pinned_acquisition_resume_and_zero_http_cached_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tiny_pins: dict[str, bytes]
) -> None:
    directory = tmp_path / "model"
    directory.mkdir()
    partial = tiny_pins["model.safetensors"][:10]
    weight_size = len(tiny_pins["model.safetensors"])
    (directory / "model.safetensors.partial").write_bytes(partial)
    calls = mock_http(
        monkeypatch,
        [
            httpx.Response(200, content=tiny_pins["config.json"]),
            httpx.Response(
                206,
                content=tiny_pins["model.safetensors"][10:],
                headers={"content-range": f"bytes 10-{weight_size - 1}/{weight_size}"},
            ),
        ],
    )
    state = tmp_path / "provider.json"
    first = nli.acquire_nli(directory, access_state=state)
    assert first["verified"] and first["new_files"] == 2 and calls[1].headers["range"] == "bytes=10-"
    receipt = (directory / "model.safetensors.source.json").read_bytes()
    state.write_text(json.dumps({"retry_at_epoch": time.time() + 3600}))
    again = nli.acquire_nli(directory, access_state=state)
    assert again["new_files"] == 0 and len(calls) == 2
    assert (directory / "model.safetensors.source.json").read_bytes() == receipt
    assert not (directory / "pytorch_model.bin").exists()


def test_weight_pin_mismatch_stops_and_preserves_shared_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tiny_pins: dict[str, bytes]
) -> None:
    calls = mock_http(monkeypatch, [httpx.Response(200, content=b"x" * len(tiny_pins["config.json"]))])
    state = tmp_path / "provider.json"
    with pytest.raises(nli.NLIUnavailable, match="hash mismatch"):
        nli.acquire_nli(tmp_path / "first", access_state=state)
    assert len(calls) == 1 and state.is_file()
    with pytest.raises(nli.NLIUnavailable, match="provider deferred"):
        nli.acquire_nli(tmp_path / "second", access_state=state)
    assert len(calls) == 1


def test_provider_retry_after_is_not_shortened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tiny_pins: dict[str, bytes]
) -> None:
    calls = mock_http(monkeypatch, [httpx.Response(429, headers={"retry-after": "7200"})])
    state = tmp_path / "provider.json"
    with pytest.raises(nli.NLIUnavailable):
        nli.acquire_nli(tmp_path / "model", access_state=state)
    assert len(calls) == 1 and json.loads(state.read_text())["retry_at_epoch"] > time.time() + 7190


def test_disk_floor_and_git_blob_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tiny_pins: dict[str, bytes]
) -> None:
    monkeypatch.setattr(nli.shutil, "disk_usage", lambda directory: SimpleNamespace(free=nli.DISK_FLOOR + 1))
    calls = mock_http(monkeypatch, [])
    with pytest.raises(nli.NLIUnavailable, match="disk reserve"):
        nli.acquire_nli(tmp_path / "model", access_state=tmp_path / "provider.json")
    assert not calls
    content = b"Original MIT license metadata\n"
    path = tmp_path / "card"
    path.write_bytes(content)
    pin = nli.FilePin(
        "card", len(content), hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest(), "git-sha1"
    )
    assert nli.file_identity(path, pin)["sha256"] == hashlib.sha256(content).hexdigest()
    path.unlink()
    path.symlink_to(tmp_path / "other")
    with pytest.raises(nli.NLIUnavailable):
        nli.file_identity(path, pin)


def fake_predictor(monkeypatch: pytest.MonkeyPatch, tokens: int) -> tuple[nli.NLIPredictor, list[Any]]:
    calls = []

    class Logits:
        def float(self):
            return self

    class Model:
        def __call__(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(logits=[Logits()])

    torch = SimpleNamespace(
        tensor=lambda value, **kwargs: value,
        long="long",
        inference_mode=nullcontext,
        softmax=lambda *args, **kwargs: SimpleNamespace(tolist=lambda: [0.1, 0.2, 0.7]),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(nli, "memory_guard", lambda: None)
    predictor = nli.NLIPredictor(Path("/nonexistent/offline"))

    def tokenize(premise, hypothesis, **kwargs):
        assert kwargs["truncation"] is False and kwargs["add_special_tokens"] is True
        return {"input_ids": [1] * tokens, "attention_mask": [1] * tokens}

    predictor.model = Model()
    predictor.tokenizer = tokenize
    return predictor, calls


def test_explicit_label_mapping_and_no_silent_context_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    predictor, calls = fake_predictor(monkeypatch, 512)
    result = predictor.score("premise", "hypothesis")
    assert result["predicted_label"] == "contradiction" and result["scores"]["contradiction"] == 0.7 and len(calls) == 1
    assert not result["semantic_truth_verified"] and not result["question_axis_coverage_verified"]
    too_long, calls = fake_predictor(monkeypatch, 513)
    rejected = too_long.score("premise", "hypothesis")
    assert rejected["status"] == "context_limit" and rejected["tokens"] == 513 and rejected["scores"] is None
    assert rejected["truncated"] is False and not calls


def test_empty_invalid_pairs_do_not_load_weights_and_memory_guard_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    predictor = nli.NLIPredictor(Path("/nonexistent/offline"))
    assert predictor.score("", "x")["status"] == "invalid_empty_pair" and predictor.model is None
    assert predictor.score("x" * 200001, "y")["status"] == "input_character_limit" and predictor.model is None
    monkeypatch.setattr(nli, "peak_resident_bytes", lambda: nli.MAX_MEMORY + 1)
    with pytest.raises(nli.NLIUnavailable, match="peak RSS"):
        nli.memory_guard()


def test_open_probe_uses_absolute_canonical_quote_offsets_and_fails_closed() -> None:
    import runpy

    probe = runpy.run_path(str(Path(__file__).parents[1] / "scripts/probe_nli_validation.py"))
    source = {"source_id": "S1", "book_id": "public", "artifact_sha256": "a" * 64, "text": "abcdefghij", "start": 100}
    quote = {"source_id": "S1", "start": 103, "end": 108, "quote": "defgh"}
    capture = {
        "responses": {
            "case": {
                "query": "Question",
                "sources": [source],
                "claims": [{"text": "Claim", "supporting_quotes": [quote]}],
            }
        }
    }
    pairs = probe["make_pairs"](capture, {"cases": []})
    assert pairs[0]["premise"] == "defgh" and pairs[1]["premise"] == "abcdefghij"
    assert pairs[0]["support_provenance"][0]["start"] == 103
    quote["start"] = 3
    quote["end"] = 8
    with pytest.raises(ValueError, match="support offsets"):
        probe["make_pairs"](capture, {"cases": []})


def test_probe_supervisor_records_memory_failure_and_reaps_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import runpy
    import subprocess

    probe = runpy.run_path(str(Path(__file__).parents[1] / "scripts/probe_nli_validation.py"))
    output = tmp_path / "result.json"

    class Child:
        pid = 123456789
        returncode = None
        waited = False

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            self.waited = True
            self.returncode = -15
            return self.returncode

    child = Child()
    signals = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=str(nli.MAX_MEMORY // 1024 + 1))
    )
    monkeypatch.setattr(probe["os"], "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(sys, "argv", ["probe", "--output", str(output)])
    with pytest.raises(MemoryError):
        probe["main"]()
    assert child.waited and signals and signals[0][0] == child.pid
    report = json.loads(output.read_text())
    assert report["status"] == "failed_supervisor" and "2.5GiB" in report["supervisor_error"]
