"""CC0 protocol tests; native FAISS always executes in a separate process."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from book_rag import incremental_ivfpq as incremental


def script_module(name: str) -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "change",
    [
        {"window_id": -1},
        {"window_id": True},
        {"passage_id": 0},
        {"passage_id": True},
        {"book_id": ""},
        {"text": ""},
        {"text": "x" * 32769},
        {"text": "\ud800"},
    ],
)
def test_input_identity_rejects_invalid_values(change: dict[str, Any]) -> None:
    value = {"window_id": 0, "passage_id": 1, "book_id": "cc0", "text": "Exact source input\n"}
    value.update(change)
    with pytest.raises(ValueError):
        incremental.Window(**value).key()


def test_input_identity_preserves_unicode_and_newlines() -> None:
    base = incremental.Window(0, 1, "cc0", "é\nline\n")
    decomposed = incremental.Window(0, 1, "cc0", "e\u0301\nline\n")
    changed_newline = incremental.Window(0, 1, "cc0", "é\r\nline\n")
    assert len({base.key(), decomposed.key(), changed_newline.key()}) == 3
    assert hashlib.sha256(base.text.encode()).hexdigest().encode() in base.key()


def test_atomic_immutable_and_checkpoint_publications(tmp_path: Path) -> None:
    immutable = tmp_path / "sealed.json"
    first = incremental._json(immutable, {"value": 1})
    assert incremental._json(immutable, {"value": 1}) == first
    with pytest.raises(ValueError, match="Immutable"):
        incremental._json(immutable, {"value": 2})
    assert json.loads(immutable.read_text()) == {"value": 1}
    checkpoint = tmp_path / "checkpoint.json"
    incremental._json(checkpoint, {"rows": 10}, replace=True)
    incremental._json(checkpoint, {"rows": 20}, replace=True)
    assert json.loads(checkpoint.read_text()) == {"rows": 20}
    assert not list(tmp_path.glob(".incremental-owned-*"))


def test_failed_writer_cleans_temporary_and_preserves_existing(tmp_path: Path) -> None:
    target = tmp_path / "sealed.bin"
    target.write_bytes(b"old sealed bytes")

    def fail(path: Path) -> None:
        path.write_bytes(b"unfinished replacement")
        raise OSError("Owned interrupted writer")

    with pytest.raises(OSError):
        incremental._atomic(target, fail, replace=True)
    assert target.read_bytes() == b"old sealed bytes"
    assert not list(tmp_path.glob(".incremental-owned-*"))


def test_symlink_output_rejected_without_touching_target(tmp_path: Path) -> None:
    source = tmp_path / "private-owned.txt"
    source.write_text("unchanged")
    link = tmp_path / "link.json"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        incremental._json(link, {"no": "overwrite"}, replace=True)
    assert source.read_text() == "unchanged"


def test_owned_source_adapter_and_epoch_tokens_are_read_only(tmp_path: Path) -> None:
    probe = script_module("probe_incremental_ivfpq")
    source = probe.create_source(tmp_path / "owned.sqlite3", 32)
    with source.connect() as conn:
        proof = incremental.capture_coverage(conn, ["cc0-proof-0"])
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE books SET title='not authorized'")
    token = incremental._build_token(source, proof)
    probe.mutate(source, "UPDATE books SET title='Outside change' WHERE id='cc0-outside'")
    assert incremental._build_token(source, proof) == token
    probe.mutate(source, "UPDATE books SET title='Covered change' WHERE id='cc0-proof-0'")
    assert incremental._build_token(source, proof) != token
    assert "faiss" not in sys.modules


def test_initial_canonical_proof_and_epochs_share_one_sql_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = script_module("probe_incremental_ivfpq")
    source = probe.create_source(tmp_path / "owned.sqlite3", 32)
    with sqlite3.connect(source.db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    original = incremental.capture_coverage
    changed = False

    def racing_capture(*args: Any, **kwargs: Any) -> Any:
        nonlocal changed
        proof = original(*args, **kwargs)
        if not changed:
            changed = True
            probe.mutate(source, "UPDATE books SET title='Changed after canonical capture' WHERE id='cc0-proof-0'")
        return proof

    monkeypatch.setattr(incremental, "capture_coverage", racing_capture)
    proof, token = incremental._initial_coverage(source, ["cc0-proof-0"])
    assert changed
    with pytest.raises(ValueError, match="Covered source changed"):
        incremental._guard(source, {"source_coverage": proof, "build_source_token": token})


def test_encoder_helpers_do_not_import_torch_or_faiss_and_reject_pending_wal(tmp_path: Path) -> None:
    encoder = script_module("benchmark_encoder_capacity")
    source = tmp_path / "owned.sqlite3"
    source.write_bytes(b"owned placeholder")
    assert encoder.frozen_database(source) == source.resolve()
    Path(str(source) + "-wal").write_bytes(b"pending owned WAL")
    with pytest.raises(ValueError, match="checkpointed"):
        encoder.frozen_database(source)
    with pytest.raises(ValueError, match="no download"):
        encoder.model_files(tmp_path / "missing-model-cache")


@pytest.mark.parametrize(
    "case",
    [
        "chunk-payload-published",
        "chunk-receipt-published",
        "checkpoint-published",
        "shard-index-published",
        "shard-receipt-published",
        "before-ready-publication",
    ],
)
def test_actual_native_crash_resume_only_in_child(case: str, tmp_path: Path) -> None:
    if importlib.util.find_spec("faiss") is None:
        pytest.skip("Optional native FAISS package absent")
    script = Path(__file__).resolve().parents[1] / "scripts/probe_incremental_ivfpq.py"
    result = subprocess.run(
        [sys.executable, str(script), "--self-test", "--case", case, "--output", str(tmp_path / "probe")],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2"},
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "probe/report.json").read_text())
    assert report["result"]["passed"] and not report["torch_imported"]
    assert report["result"]["repeated_uncommitted_rows"] == (128 if case == "chunk-payload-published" else 0)
    assert report["peak_rss_bytes"] < 1024**3 and report["owned_output_bytes"] < 300 * 1024**2
    assert "faiss" not in sys.modules


def test_actual_native_query_scope_epochs_and_code_equivalence_only_in_child(tmp_path: Path) -> None:
    if importlib.util.find_spec("faiss") is None:
        pytest.skip("Optional native FAISS package absent")
    script = Path(__file__).resolve().parents[1] / "scripts/probe_incremental_ivfpq.py"
    result = subprocess.run(
        [sys.executable, str(script), "--self-test", "--output", str(tmp_path / "proof")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "proof/report.json").read_text())
    assert report["result"]["passed"] and report["result"]["standalone_vs_original_float_add_top100_equal"]
    assert report["result"]["encoded_rows_total"] == 2048
    assert report["result"]["checkpoint_bytes"] < 1024
    assert len(report["result"]["scenarios"]) == 13
    assert len(report["result"]["atomic_failures"]) == 6
    assert "faiss" not in sys.modules


INTEGRITY_CHILD = r"""
import json, os, sys
from pathlib import Path
import numpy as np
os.environ['BOOKRAG_IVFPQ_WORKER'] = '1'
from book_rag.experimental_ivfpq import PQConfig, train_codec, faiss_backend, file_sha256
from book_rag.incremental_ivfpq import Window, build_incremental, query_incremental
from scripts.probe_incremental_ivfpq import create_source, model_space, windows, mutate
case, root = sys.argv[1], Path(sys.argv[2])
source = create_source(root/'owned.sqlite3', 512)
rng = np.random.default_rng(89)
vectors = rng.standard_normal((512,8),dtype=np.float32)
vectors /= np.linalg.norm(vectors,axis=1,keepdims=True)
codec = root/'codec.faiss'
faiss_backend().write_index(train_codec(vectors,PQConfig(dimensions=8,nlist=4,subquantizers=2,bits=4,iterations=2)),str(codec))
space = model_space(8,synthetic=True)
calls = []
def encode(records):
    calls.extend(r.window_id for r in records)
    if case == 'source-change-during-encode':
        mutate(source,"UPDATE books SET title='Changed during encode' WHERE id='cc0-proof-0'")
    return np.ascontiguousarray(vectors[[r.window_id for r in records]])
kwargs=dict(source=source,encode=encode,output=root,codec_path=codec,codec_sha256=file_sha256(codec),
    model_space=space,rows_by_book={f'cc0-proof-{j}':128 for j in range(4)},chunk_rows=64,shard_rows=128)
records=list(windows(512))
if case == 'omitted-passage':
    records[1]=Window(1,1,'cc0-proof-0',records[1].text)
elif case == 'out-of-order-passage':
    a,b=records[127],records[128]
    records[127]=Window(127,b.passage_id,b.book_id,a.text)
    records[128]=Window(128,a.passage_id,a.book_id,b.text)
if case in ('omitted-passage','out-of-order-passage','source-change-during-encode'):
    try:
        build_incremental(windows=records,**kwargs)
    except ValueError as e:
        assert any(term in str(e) for term in ('omits canonical','contiguous','Covered source changed')),str(e)
    else: raise AssertionError('Invalid source input accepted')
    assert not list(root.glob('incremental-*/ready.json'))
else:
    first=build_incremental(windows=records,stop_after_chunks=1,**kwargs)
    directory=Path(first['directory'])
    before=len(calls)
    if case == 'changed-input-prefix':
        old=records[0];records[0]=Window(0,old.passage_id,old.book_id,old.text+' changed')
    elif case == 'corrupt-checkpoint':
        p=directory/'checkpoint.json';v=json.loads(p.read_text());v['input_chain']='f'*64;p.write_text(json.dumps(v))
    elif case == 'corrupt-chunk':
        with (directory/'chunk-00000000.npz').open('ab') as f:f.write(b'owned corruption')
    elif case == 'missing-checkpoint-prefix':
        (directory/'chunk-00000000.json').unlink();(directory/'chunk-00000000.npz').unlink()
    if case in ('changed-input-prefix','corrupt-checkpoint','corrupt-chunk','missing-checkpoint-prefix'):
        try: build_incremental(windows=records,**kwargs)
        except ValueError: pass
        else: raise AssertionError('Corrupt durable prefix accepted')
        assert len(calls)==before and not (directory/'ready.json').exists()
    else:
        complete=build_incremental(windows=records,**kwargs)
        if case == 'ready-missing-shard':
            p=directory/'ready.json';v=json.loads(p.read_text());v['shards'].pop();p.write_text(json.dumps(v))
        elif case == 'model-space-mismatch':space={**space,'revision':'2'*40}
        try:query_incremental(source,directory,vectors[:1].copy(),space,nprobe=4)
        except ValueError:pass
        else:raise AssertionError('Invalid ready/model metadata accepted')
assert 'torch' not in sys.modules
print('integrity-rejected-safely')
"""


@pytest.mark.parametrize(
    "case",
    [
        "omitted-passage",
        "out-of-order-passage",
        "source-change-during-encode",
        "changed-input-prefix",
        "corrupt-checkpoint",
        "corrupt-chunk",
        "missing-checkpoint-prefix",
        "ready-missing-shard",
        "model-space-mismatch",
    ],
)
def test_native_integrity_rejections_are_fail_closed(case: str, tmp_path: Path) -> None:
    if importlib.util.find_spec("faiss") is None:
        pytest.skip("Optional native FAISS package absent")
    result = subprocess.run(
        [sys.executable, "-c", INTEGRITY_CHILD, case, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2"},
    )
    assert result.returncode == 0, result.stderr
    assert "integrity-rejected-safely" in result.stdout
    assert "faiss" not in sys.modules
