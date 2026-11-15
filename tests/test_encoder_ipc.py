from __future__ import annotations

import multiprocessing
import pickle
import sys
import threading
import time
from typing import Any

import pytest

from book_rag.encoder_ipc import plain_metadata, rpc


def forbidden_unpickle() -> Any:
    raise AssertionError("A framework-specific reducer crossed the IPC boundary")


class FrameworkVersion(str):
    def __reduce__(self) -> Any:
        return forbidden_unpickle, ()


def test_metadata_is_plain_json_before_pickle_and_cannot_import_frameworks() -> None:
    before = "torch" in sys.modules
    value = plain_metadata({"nested": [{"version": FrameworkVersion("2.15.0")}], "count": 3})
    assert type(value["nested"][0]["version"]) is str
    assert pickle.loads(pickle.dumps(value))["nested"][0]["version"] == "2.15.0"
    assert ("torch" in sys.modules) == before
    assert "faiss" not in sys.modules


@pytest.mark.parametrize("value", [{"bad": float("nan")}, {"large": "x" * (1024 * 1024 + 1)}, {"object": object()}])
def test_metadata_rejects_unbounded_or_non_json_values(value: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        plain_metadata(value)


def test_rpc_reports_worker_failure_without_hanging() -> None:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()

    def responder() -> None:
        child.recv()
        child.send({"ok": False, "error": "owned test error"})

    thread = threading.Thread(target=responder)
    thread.start()
    try:
        with pytest.raises(RuntimeError, match="owned test error"):
            rpc(parent, {"operation": "test"}, 1)
    finally:
        thread.join(timeout=1)
        parent.close()
        child.close()


def test_rpc_has_finite_receive_deadline() -> None:
    parent, child = multiprocessing.get_context("spawn").Pipe()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            rpc(parent, {"operation": "unanswered"}, 0.05)
        assert time.monotonic() - started < 1
    finally:
        parent.close()
        child.close()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 121, True])
def test_rpc_rejects_nonfinite_or_unbounded_deadlines(timeout: float) -> None:
    parent, child = multiprocessing.get_context("spawn").Pipe()
    try:
        with pytest.raises(ValueError, match="finite"):
            rpc(parent, {}, timeout)
    finally:
        parent.close()
        child.close()


@pytest.mark.parametrize("response", [[], {"ok": True}, {"ok": False, "error": "controlled failure"}])
def test_rpc_rejects_malformed_worker_response(response: Any) -> None:
    parent, child = multiprocessing.get_context("spawn").Pipe()

    def responder() -> None:
        child.recv()
        child.send(response)

    thread = threading.Thread(target=responder)
    thread.start()
    try:
        with pytest.raises(RuntimeError):
            rpc(parent, {}, 1)
    finally:
        thread.join(timeout=1)
        parent.close()
        child.close()
