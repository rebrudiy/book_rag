"""Reuse identities and rows must remain exact across append/reorder and corruption."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from book_rag.vector_reuse import KEYMAP, RECEIPT, GenerationReuse, KeyMapWriter, ModelSpace, window_key


def descriptor() -> dict[str, Any]:
    return {
        "model": "test/model",
        "model_revision": "a" * 40,
        "dimensions": 3,
        "model_max_tokens": 128,
        "embedding_profile": {
            "name": "test",
            "model": "test/model",
            "revision": "a" * 40,
            "document_prefix": "passage: ",
            "query_prefix": "query: ",
            "max_tokens": 128,
        },
    }


def space() -> ModelSpace:
    return ModelSpace.from_metadata(descriptor(), normalized=True)


def prepare(folder: Path, texts: list[str]) -> np.ndarray:
    folder.mkdir()
    array = np.eye(3, dtype="float32")[np.arange(len(texts)) % 3]
    np.save(folder / "embeddings.npy", array)
    metadata = descriptor() | {"vectors": len(texts), "generation": "gen-owned"}
    checkpoint = {
        "version": 1,
        "completed_vectors": len(texts),
        "descriptor": metadata,
        "vector_checksum": hashlib.sha256(array.tobytes()).hexdigest(),
    }
    (folder / "metadata.json").write_text(json.dumps(metadata))
    (folder / "checkpoint.json").write_text(json.dumps(checkpoint))
    return array


def completed(tmp_path: Path, texts: list[str] | None = None) -> tuple[Path, np.ndarray]:
    texts = texts or ["passage: exact first", "passage: exact second", "passage: exact first"]
    folder = tmp_path / "staging-owned"
    array = prepare(folder, texts)
    with KeyMapWriter(folder, space(), len(texts)) as writer:
        writer.add_batch(texts[:2], start_row=0)
        writer.add_batch(texts[2:], start_row=2)
        receipt = writer.finalize()
        assert receipt["unique_keys"] == len(set(texts))
    folder.rename(tmp_path / "gen-owned")
    return tmp_path / "gen-owned", array


def test_exact_append_lookup_first_duplicate_row_and_independent_copies(tmp_path: Path) -> None:
    folder, array = completed(tmp_path)
    with GenerationReuse.open(folder, space()) as reuse:
        results = reuse.lookup(
            ["passage: exact second", "new appended window", "passage: exact first", "passage: exact first"]
        )
        assert results[1] is None
        np.testing.assert_array_equal(results[0], array[1])
        np.testing.assert_array_equal(results[2], array[0])
        first = results[2]
        assert first is not None
        first[0] = -100
        np.testing.assert_array_equal(results[3], array[0])
        np.testing.assert_array_equal(reuse.lookup(["passage: exact first"])[0], array[0])
        second = results[0]
        assert second is not None and second.dtype == np.dtype("float32")
    connection = sqlite3.connect(folder / KEYMAP)
    assert "WITHOUT ROWID" in connection.execute("select sql from sqlite_master where name='window_keys'").fetchone()[0]
    assert [r[1] for r in connection.execute("pragma table_info(window_keys)")] == ["key", "row_index"]
    connection.close()
    assert b"passage: exact first" not in (folder / KEYMAP).read_bytes()
    assert not (folder / "vectors-copy.npy").exists()


def test_input_keys_preserve_prefix_whitespace_and_unicode() -> None:
    values = ["x", " x", "x ", "passage: x", "query: x", "café", "cafe\u0301"]
    assert len({window_key(text, space()) for text in values}) == len(values)
    assert all(len(window_key(text, space())) == 32 for text in values)


@pytest.mark.parametrize(
    "change",
    [
        {"model": "different/model", "embedding_profile": {"name": "other"}},
        {"model_revision": "b" * 40, "embedding_profile": {"name": "other"}},
        {"dimensions": 4},
        {"model_max_tokens": 512},
        {"embedding_profile": {"name": "different-prefix", "document_prefix": "new: "}},
    ],
)
def test_model_space_mismatch_rejects_reuse(tmp_path: Path, change: dict[str, Any]) -> None:
    folder, _ = completed(tmp_path)
    other = ModelSpace.from_metadata(descriptor() | change, normalized=True)
    with pytest.raises(ValueError, match="model/profile"):
        GenerationReuse.open(folder, other)


def test_normalization_mismatch_rejects_reuse(tmp_path: Path) -> None:
    folder, _ = completed(tmp_path)
    with pytest.raises(ValueError, match="normalization"):
        GenerationReuse.open(folder, ModelSpace.from_metadata(descriptor(), normalized=False))


def test_resume_and_truncate_reconcile_durable_prefix(tmp_path: Path) -> None:
    folder = tmp_path / "staging-owned"
    texts = ["repeat", "second", "repeat", "fourth"]
    prepare(folder, texts)
    with KeyMapWriter(folder, space(), 4) as writer:
        writer.add_batch(texts[:3], start_row=0)
    with KeyMapWriter(folder, space(), 4, resume=True) as writer:
        assert writer.rows_written == 3
        writer.truncate_to(1)
        writer.add_batch(texts[1:], start_row=1)
        writer.finalize()
    folder.rename(tmp_path / "gen-owned")
    with GenerationReuse.open(tmp_path / "gen-owned", space()) as reuse:
        assert reuse.lookup(["repeat", "fourth"])[0] is not None


def test_staging_or_partial_generations_cannot_be_sources(tmp_path: Path) -> None:
    folder = tmp_path / "staging-owned"
    prepare(folder, ["a", "b"])
    with KeyMapWriter(folder, space(), 2) as writer:
        writer.add_batch(["a"], start_row=0)
        with pytest.raises(ValueError, match="partial"):
            writer.finalize()
    with pytest.raises(FileNotFoundError):
        GenerationReuse.open(folder, space())
    with KeyMapWriter(folder, space(), 2, resume=True) as writer:
        writer.add_batch(["b"], start_row=1)
        writer.finalize()
    with pytest.raises(ValueError, match="Staging"):
        GenerationReuse.open(folder, space())


def test_seeding_complete_generation_requires_explicit_flag(tmp_path: Path) -> None:
    folder = tmp_path / "gen-owned"
    prepare(folder, ["a"])
    with pytest.raises(ValueError, match="explicit"):
        KeyMapWriter(folder, space(), 1)
    with KeyMapWriter(folder, space(), 1, seed_completed=True) as writer:
        writer.add_batch(["a"], start_row=0)
        writer.finalize()
    with pytest.raises(ValueError, match="immutable"):
        KeyMapWriter(folder, space(), 1, seed_completed=True)


@pytest.mark.parametrize("mutation", ["truncated", "dtype", "shape", "nan", "changed_finite", "not_normalized"])
def test_corrupt_vector_sources_are_rejected(tmp_path: Path, mutation: str) -> None:
    folder, original = completed(tmp_path)
    if mutation == "truncated":
        path = folder / "embeddings.npy"
        path.write_bytes(path.read_bytes()[:-2])
    elif mutation == "dtype":
        np.save(folder / "embeddings.npy", original.astype("float64"))
    elif mutation == "shape":
        np.save(folder / "embeddings.npy", np.zeros((1, 3), dtype="float32"))
    else:
        changed = original.copy()
        if mutation == "nan":
            changed[0, 0] = np.nan
        elif mutation == "not_normalized":
            changed[0] *= 2
        else:
            changed[0] = np.array([0, 1, 0], dtype="float32")
        np.save(folder / "embeddings.npy", changed)
    with pytest.raises((ValueError, OSError)):
        GenerationReuse.open(folder, space())


def test_invalid_map_rows_detected_even_if_outer_file_digest_updated(tmp_path: Path) -> None:
    folder, _ = completed(tmp_path)
    connection = sqlite3.connect(folder / KEYMAP)
    connection.execute("update window_keys set row_index=row_index+100")
    connection.commit()
    connection.close()
    receipt = json.loads((folder / RECEIPT).read_text())
    receipt["keymap_sha256"] = hashlib.sha256((folder / KEYMAP).read_bytes()).hexdigest()
    (folder / RECEIPT).write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="row"):
        GenerationReuse.open(folder, space())


def test_incomplete_checkpoint_and_metadata_tampering_fail(tmp_path: Path) -> None:
    folder, _ = completed(tmp_path)
    checkpoint = json.loads((folder / "checkpoint.json").read_text())
    checkpoint["completed_vectors"] = 1
    (folder / "checkpoint.json").write_text(json.dumps(checkpoint))
    with pytest.raises(ValueError, match="Incomplete"):
        GenerationReuse.open(folder, space())


def test_bounded_batch_offsets_and_closed_handles(tmp_path: Path) -> None:
    folder, _ = completed(tmp_path)
    with GenerationReuse.open(folder, space()) as reuse:
        assert reuse.lookup([]) == []
        with pytest.raises(ValueError, match="500"):
            reuse.lookup(["x"] * 501)
    with pytest.raises(RuntimeError, match="closed"):
        reuse.lookup(["x"])
    pending = tmp_path / "staging-next"
    prepare(pending, ["a"])
    with KeyMapWriter(pending, space(), 1) as writer:
        with pytest.raises(ValueError, match="next row"):
            writer.add_batch(["a"], start_row=1)
        with pytest.raises(ValueError, match="count"):
            writer.add_batch(["a", "b"], start_row=0)


def test_detects_mutation_after_open_before_accessing_memmap(tmp_path: Path) -> None:
    folder, _ = completed(tmp_path)
    with GenerationReuse.open(folder, space()) as reuse:
        path = folder / "embeddings.npy"
        path.write_bytes(path.read_bytes()[:-4])
        with pytest.raises(ValueError, match="changed"):
            reuse.lookup(["passage: exact first"])


def test_receipt_loss_after_sealing_can_finish_without_changing_vector_data(tmp_path: Path) -> None:
    folder, _ = completed(tmp_path)
    before = (folder / "embeddings.npy").read_bytes()
    # Simulate a crash after the key DB was sealed and before receipt rename.
    (folder / RECEIPT).unlink()
    with KeyMapWriter(folder, space(), 3, resume=True, seed_completed=True) as writer:
        with pytest.raises(ValueError, match="Sealed"):
            writer.truncate_to(0)
        writer.finalize()
    assert (folder / "embeddings.npy").read_bytes() == before
    with GenerationReuse.open(folder, space()) as reuse:
        assert reuse.lookup(["passage: exact first"])[0] is not None


def test_partial_checkpoint_cannot_be_finalized_even_with_all_keys(tmp_path: Path) -> None:
    folder = tmp_path / "staging-owned"
    prepare(folder, ["a", "b"])
    checkpoint = json.loads((folder / "checkpoint.json").read_text())
    checkpoint["completed_vectors"] = 1
    (folder / "checkpoint.json").write_text(json.dumps(checkpoint))
    with KeyMapWriter(folder, space(), 2) as writer:
        writer.add_batch(["a", "b"], start_row=0)
        with pytest.raises(ValueError, match="Incomplete"):
            writer.finalize()


def test_corrupt_or_unpinned_model_descriptors_fail() -> None:
    with pytest.raises(ValueError, match="immutable"):
        ModelSpace.from_metadata(descriptor() | {"model_revision": "main"}, normalized=True)
    with pytest.raises(ValueError, match="Profile"):
        ModelSpace.from_metadata(descriptor() | {"embedding_profile": {}}, normalized=True)
    with pytest.raises(ValueError, match="Profile"):
        ModelSpace.from_metadata(descriptor() | {"embedding_profile": {"model": "wrong/model"}}, normalized=True)


@pytest.mark.parametrize("change", [{"complete": False}, {"schema_version": 99}, {"schema_version": True}])
def test_unfinished_or_unsupported_receipt_cannot_authorize_reuse(tmp_path: Path, change: dict[str, Any]) -> None:
    folder, _ = completed(tmp_path)
    receipt = json.loads((folder / RECEIPT).read_text()) | change
    (folder / RECEIPT).write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="finalized"):
        GenerationReuse.open(folder, space())


def test_unannounced_keymap_modification_fails_checksum(tmp_path: Path) -> None:
    folder, _ = completed(tmp_path)
    connection = sqlite3.connect(folder / KEYMAP)
    connection.execute("delete from window_keys")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="checksum"):
        GenerationReuse.open(folder, space())


def test_incremental_key_checksum_survives_duplicates_and_batch_boundaries(tmp_path: Path) -> None:
    texts = ["a", "a", "b", "a", "c", "b"]
    folder = tmp_path / "staging-owned"
    prepare(folder, texts)
    with KeyMapWriter(folder, space(), len(texts)) as writer:
        writer.add_batch(texts[:2], start_row=0)
        first = writer.checksum
        writer.add_batch(texts[2:4], start_row=2)
        assert writer.checksum != first
    with KeyMapWriter(folder, space(), len(texts), resume=True) as writer:
        prefix = writer.checksum
        writer.truncate_to(2)
        assert writer.checksum == first
        writer.add_batch(texts[2:4], start_row=2)
        assert writer.checksum == prefix
        writer.add_batch(texts[4:], start_row=4)
        expected = hashlib.sha256()
        for text, row in [("a", 0), ("b", 2), ("c", 4)]:
            expected.update(window_key(text, space()) + row.to_bytes(8, "big"))
        assert writer.checksum == expected.hexdigest()
        assert not writer.finalized
        receipt = writer.finalize()
        assert receipt["key_checksum"] == expected.hexdigest()


def test_resume_rejects_valid_shape_key_tampering(tmp_path: Path) -> None:
    folder = tmp_path / "staging-owned"
    prepare(folder, ["a", "b", "c"])
    with KeyMapWriter(folder, space(), 3) as writer:
        writer.add_batch(["a", "b"], start_row=0)
    with sqlite3.connect(folder / KEYMAP) as connection:
        connection.execute("UPDATE window_keys SET key=? WHERE row_index=0", (b"x" * 32,))
    with pytest.raises(ValueError, match="prefix checksum"):
        KeyMapWriter(folder, space(), 3, resume=True)
