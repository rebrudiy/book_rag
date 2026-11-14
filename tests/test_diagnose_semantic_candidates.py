"""Pure/fake-encoder checks; real frozen sources and accelerator jobs are untouched."""

from __future__ import annotations

import hashlib
import json
import runpy
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from book_rag.embedding_profiles import MINILM_MODEL, MINILM_PROFILE, MINILM_REVISION
from book_rag.semantic import _profile_windows

DIAGNOSTIC = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/diagnose_semantic_candidates.py"))


class Tokenizer:
    def encode(self, text: str, **kwargs: Any) -> list[str]:
        return text.split()

    def decode(self, values: list[str], **kwargs: Any) -> str:
        return " ".join(values)

    def __call__(self, text: str, **kwargs: Any) -> dict[str, list[tuple[int, int]]]:
        import re

        return {"offset_mapping": [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]}


class Encoder:
    max_seq_length = 128
    tokenizer = Tokenizer()
    device = "fake CPU · no model loaded"

    def encode(self, texts: list[str], **kwargs: Any) -> np.ndarray:
        values = np.zeros((len(texts), 384), dtype=np.float32)
        for index, text in enumerate(texts):
            cosine = 1.0 if text == "query known probe" else 0.4 if "TARGET" in text else 0.8
            if text.startswith("TARGET"):
                cosine = 0.95
            values[index, :2] = [cosine, np.sqrt(1 - cosine**2)]
        return values


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fixture(tmp_path: Path) -> tuple[Path, Encoder]:
    source = tmp_path / "source"
    folder = source / "vectors" / "gen-diagnostic-fixture"
    folder.mkdir(parents=True)
    connection = sqlite3.connect(source / "library.sqlite3")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE books(id TEXT PRIMARY KEY,title TEXT,author TEXT,language TEXT,status TEXT);"
        "CREATE TABLE sections(id INTEGER PRIMARY KEY,title TEXT);"
        "CREATE TABLE passages(id INTEGER PRIMARY KEY,book_id TEXT,section_id INTEGER,text TEXT,sha256 TEXT);"
        "INSERT INTO books VALUES('distractor','CC0 distractor','CC0 fixture','fr','indexed');"
        "INSERT INTO books VALUES('target','CC0 target','CC0 fixture','de','indexed');"
        "INSERT INTO sections VALUES(1,'Fixture chapter');"
    )
    fingerprint = hashlib.sha256()
    for identity in range(1, 161):
        target = identity >= 159
        text = f"{'TARGET' if target else 'Distractor'} original CC0 diagnostic body {identity}. More context."
        checksum = sha(text.encode())
        connection.execute(
            "INSERT INTO passages VALUES(?,?,?,?,?)",
            (identity, "target" if target else "distractor", 1, text, checksum),
        )
        fingerprint.update(f"{identity}:{checksum}\n".encode())
    connection.commit()
    rows = connection.execute(
        "SELECT p.id,p.text,b.title,b.author,s.title section_title FROM passages p "
        "JOIN books b ON b.id=p.book_id JOIN sections s ON s.id=p.section_id ORDER BY p.id"
    ).fetchall()
    model = Encoder()
    texts = [text for row in rows for text in _profile_windows(model, row, MINILM_PROFILE)]
    vectors = model.encode(texts)
    identities = np.array([row["id"] for row in rows], dtype=np.int64)
    assert len(texts) == len(identities)
    np.save(folder / "embeddings.npy", vectors)
    np.save(folder / "ids.npy", identities)
    metadata = {
        "generation": folder.name,
        "model": MINILM_MODEL,
        "model_revision": MINILM_REVISION,
        "model_max_tokens": 128,
        "dimensions": 384,
        "strategy": "token-windows-v2",
        "context_header_tokens": 24,
        "overlap_tokens": 20,
        "embedding_profile": MINILM_PROFILE.metadata(MINILM_REVISION),
        "vectors": len(vectors),
        "passages": len(rows),
        "corpus_fingerprint": fingerprint.hexdigest(),
    }
    (folder / "metadata.json").write_text(json.dumps(metadata))
    (source / "vectors/active.json").write_text(json.dumps(metadata))
    checkpoint = {
        "completed_vectors": len(vectors),
        "vector_checksum": sha(vectors.tobytes()),
        "id_checksum": sha(identities.tobytes()),
    }
    (folder / "checkpoint.json").write_text(json.dumps(checkpoint))
    receipt = {
        "complete": True,
        "generation": folder.name,
        "vectors": len(vectors),
        "vector_checksum": checkpoint["vector_checksum"],
        "metadata_sha256": sha((folder / "metadata.json").read_bytes()),
        "checkpoint_sha256": sha((folder / "checkpoint.json").read_bytes()),
    }
    (folder / "vector-reuse.json").write_text(json.dumps(receipt))
    connection.close()
    return source, model


def test_raw_rank_exclusion_exact_reencoding_no_source_writes(tmp_path: Path) -> None:
    source, model = fixture(tmp_path)
    before = {path: sha(path.read_bytes()) for path in source.rglob("*") if path.is_file()}
    report = DIAGNOSTIC["run"](source, "query known probe", "target", model=model, fresh_windows=2)
    assert report["best_target_window"]["global_window_rank"] == 159
    assert report["best_target_window"]["target_language_window_rank"] == 1
    assert report["best_target_window"]["in_global_2000_windows"]
    assert not report["target_book_in_first_stage"]
    assert not report["best_target_window"]["in_first_stage_100_passages"]
    assert report["top_window_distribution"]["languages"] == {"fr": 100}
    assert len(report["target_reencoding"]) == 2
    variants = report["target_reencoding"][0]["input_variants"]
    assert variants[0]["fresh_stored_max_abs_difference"] == 0
    assert variants[0]["fresh_query_cosine"] == pytest.approx(0.4)
    assert variants[1]["fresh_query_cosine"] == pytest.approx(0.95)
    assert "original CC0 diagnostic body" not in json.dumps(report), "Do not expose full source text"
    assert before == {path: sha(path.read_bytes()) for path in source.rglob("*") if path.is_file()}


def test_explicit_passage_respects_sample_bound(tmp_path: Path) -> None:
    source, model = fixture(tmp_path)
    report = DIAGNOSTIC["run"](source, "query known probe", "target", target_passage=160, fresh_windows=1, model=model)
    assert len(report["target_reencoding"]) == 1
    assert report["target_reencoding"][0]["passage_id"] == 160
    with pytest.raises(ValueError, match="does not belong"):
        DIAGNOSTIC["run"](source, "query known probe", "target", target_passage=1, model=model)


def test_readonly_sql_and_pending_wal_rejection(tmp_path: Path) -> None:
    source, _ = fixture(tmp_path)
    with DIAGNOSTIC["frozen_connection"](source / "library.sqlite3") as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM passages")
    (source / "library.sqlite3-wal").write_bytes(b"not checkpointed")
    with pytest.raises(ValueError, match="pending WAL"):
        with DIAGNOSTIC["frozen_connection"](source / "library.sqlite3"):
            pytest.fail("A pending WAL must fail before immutable SQLite opens")


def test_full_payload_corruption_rejected(tmp_path: Path) -> None:
    source, model = fixture(tmp_path)
    path = source / "vectors/gen-diagnostic-fixture/embeddings.npy"
    with path.open("ab") as stream:
        stream.write(b"trailing junk")
    with pytest.raises(ValueError, match="exact file length"):
        DIAGNOSTIC["run"](source, "query known probe", "target", model=model)
    values = np.load(path, allow_pickle=False)
    values[[0, 159]] = values[[159, 0]]
    np.save(path, values)
    with pytest.raises(ValueError, match="checksums"):
        DIAGNOSTIC["run"](source, "query known probe", "target", model=model)


def test_rank_and_raw_top_k_ties_and_sentence_exactness() -> None:
    scores = np.array([0.8, 0.9, 0.9, 0.8, 0.3], dtype=np.float32)
    assert DIAGNOSTIC["rank_at"](scores, 2) == 2
    assert list(DIAGNOSTIC["top_positions"](scores, 3)) == [1, 2, 0]
    assert list(DIAGNOSTIC["top_positions"](scores, 20)) == [1, 2, 0, 3, 4]
    assert list(DIAGNOSTIC["top_positions"](np.array([], dtype=np.float32), 2)) == []
    text = "  Он проснулся? Затем наступило утро."
    assert DIAGNOSTIC["first_sentence"](text) == "  Он проснулся?"
