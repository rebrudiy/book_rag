"""Owned compact-v2 registration; deterministic vectors and no native inference."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from test_semantic_batch_overlay import Tokenizer, fake_worker, seed_batch

from book_rag import semantic_batch_overlay as overlay
from book_rag.compact_window_manifest import repack_window_manifest
from book_rag.experimental_ivfpq import file_sha256


def test_v2_retains_all_proofs_but_can_use_compact_without_old_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = seed_batch(tmp_path)
    compact = tmp_path / "compact" / "windows.sqlite3"
    repack_window_manifest(state.live.data_dir, state.batch / "windows.sqlite3", compact, Tokenizer(), reserve_bytes=0)
    preserved = {p: file_sha256(p) for p in state.generation.iterdir() if p.is_file()}
    producer_sha = file_sha256(state.batch / "windows.sqlite3.receipt.json")
    (state.batch / "windows.sqlite3").unlink()  # Owned fixture ONLY, never real source payloads.
    monkeypatch.setattr(overlay, "_run_worker", lambda request: fake_worker(state, request))
    monkeypatch.setattr(overlay, "_encode_query", lambda *args: np.eye(1, 384, dtype=np.float32))
    registry = overlay.install_batch_registry(
        state.live,
        [state.batch],
        state.destination,
        compact_manifests={state.batch: compact},
    )
    assert registry["schema_version"] == 2 and registry["kind"] == overlay.KIND_V2
    entry = registry["entries"][0]
    assert entry["manifest_receipt_sha256"] == producer_sha
    assert entry["window_manifest"]["sha256"] == file_sha256(compact)
    assert preserved == {p: file_sha256(p) for p in preserved}
    assert producer_sha == file_sha256(state.batch / "windows.sqlite3.receipt.json")
    monkeypatch.setenv("BOOKRAG_SEMANTIC_BATCH_REGISTRY", str(state.destination / "registry.json"))
    response = overlay.batch_overlay_search(state.live, "clef", "semantic", "cc0-fr", "fr", 5)
    assert response and response["results"]
    assert all(row["verified"] and row["book_id"] == "cc0-fr" for row in response["results"])
    code = """
import sys
from pathlib import Path
from book_rag.semantic_batch_overlay import _registry,_entry
r=_registry(Path(sys.argv[1]));_entry(r,0)
assert all(x not in sys.modules for x in ('torch','faiss','transformers','book_rag.semantic'))
"""
    subprocess.run(
        [sys.executable, "-c", code, str(state.destination / "registry.json")],
        check=True,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=15,
    )
    compact.write_bytes(compact.read_bytes() + b"corrupt")
    fallback = overlay.batch_overlay_search(state.live, "clef", "semantic", "cc0-fr", "fr", 5)
    assert fallback and fallback["mode"] == "lexical" and fallback["warnings"]


def test_v1_stays_strict_and_unknown_v2_formats_are_rejected(tmp_path: Path) -> None:
    state = seed_batch(tmp_path)
    (state.batch / "windows.sqlite3").unlink()
    with pytest.raises((ValueError, OSError)):
        overlay._inspect_batch(state.batch)
    unknown = {"format": "unknown", "path": "/x", "receipt_path": "/y", "sha256": "0" * 64, "receipt_sha256": "0" * 64}
    value = {
        "kind": overlay.KIND_V2,
        "schema_version": 2,
        "entries": [{"covered_book_ids": ["cc0-fr"], "window_manifest": unknown}],
        "book_to_entry": {"cc0-fr": 0},
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Unknown registered"):
        overlay._registry(path)
    value["kind"], value["schema_version"] = overlay.KIND, 1
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="v2"):
        overlay._registry(path)


def test_compact_size_cap_rejects_before_hashing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from book_rag import compact_window_reader as reader

    payload, sidecar = tmp_path / "compact.sqlite3", tmp_path / "compact.sqlite3.receipt.json"
    payload.write_bytes(b"owned oversized fixture")
    sidecar.write_text("{}")
    monkeypatch.setattr(reader, "MAX_COMPACT_BYTES", 1)
    monkeypatch.setattr(reader, "file_sha256", lambda *args: pytest.fail("Hash must not run above cap"))
    with pytest.raises(ValueError, match="bounded regular"):
        reader.compact_receipt(
            payload, sidecar, expected_sha256="0" * 64, expected_receipt_sha256="0" * 64, producer_receipt={}
        )
    (tmp_path / "source-fixture").mkdir()
    state = seed_batch(tmp_path / "source-fixture")
    monkeypatch.setattr(overlay, "file_sha256", lambda *args: pytest.fail("Installer must check size before hash"))
    with pytest.raises(ValueError, match="bounded regular"):
        overlay.install_batch_registry(
            state.live, [state.batch], state.destination, compact_manifests={state.batch: payload}
        )
