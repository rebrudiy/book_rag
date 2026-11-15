from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from book_rag.experimental_ivfpq import file_sha256
from book_rag.library import Library
from book_rag.semantic_pilot import (
    ReadOnlyLibrary,
    create_owned_subset,
    manifest_windows,
    prepare_windows,
    verify_owned_subset,
)


class CharacterTokenizer:
    def encode(self, text: str, **kwargs: Any) -> list[int]:
        return [ord(char) for char in text]

    def decode(self, values: list[int], **kwargs: Any) -> str:
        return "".join(chr(value) for value in values)

    def __call__(self, text: str, **kwargs: Any) -> dict[str, Any]:
        return {"offset_mapping": [(j, j + 1) for j in range(len(text))]}


def seed(root: Path) -> Library:
    library = Library(root / "original-owned")
    for j in range(2):
        path = root / f"cc0-{j}.txt"
        path.write_text(("Ключ хранится в зелёной чашке. Точный исходный фрагмент.\n\n" * 8) + f"Конец {j}.")
        library.import_book(
            path,
            {
                "id": f"cc0-{j}",
                "title": f"CC0 {j}",
                "author": "CC0 author",
                "language": "ru",
                "edition": "Owned test edition",
                "rights": "CC0",
                "rights_url": "https://example.invalid/CC0",
            },
        )
    return library


def test_selective_copy_preserves_ids_attribution_provenance_and_does_not_init_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = seed(tmp_path)
    with original.connect() as conn:
        conn.execute(
            "UPDATE section_roles SET section_translator='CC0 translator' "
            "WHERE section_id=(SELECT min(id) FROM sections)"
        )
        passage = conn.execute("SELECT id FROM passages ORDER BY id LIMIT 1").fetchone()[0]
    original.save({"kind": "passage", "passage_id": passage, "title": "Private owned test note", "note": "not copied"})
    before = file_sha256(original.db_path)
    initializer = Library.__init__

    def guarded(self: Library, directory: Path) -> None:
        assert directory.resolve() != original.data_dir.resolve(), "Never initialize/migrate original"
        initializer(self, directory)

    monkeypatch.setattr(Library, "__init__", guarded)
    destination = tmp_path / "subset"
    receipt = create_owned_subset(original.data_dir, destination, ["cc0-0", "cc0-1"])
    assert file_sha256(original.db_path) == before
    assert receipt["source_signature"]["table_counts"]["books"] == 2
    owned = ReadOnlyLibrary(destination)
    with owned.connect() as conn, original.connect() as source:
        assert list(map(tuple, conn.execute("SELECT * FROM passages ORDER BY id"))) == list(
            map(tuple, source.execute("SELECT * FROM passages ORDER BY id"))
        )
        assert list(map(tuple, conn.execute("SELECT * FROM section_roles ORDER BY section_id"))) == list(
            map(tuple, source.execute("SELECT * FROM section_roles ORDER BY section_id"))
        )
        assert conn.execute("SELECT count(*) FROM saved").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM saved_provenance").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE books SET title='forbidden'")
    assert owned.search("ключ", mode="lexical")["results"]
    assert verify_owned_subset(destination)["source_signature"] == receipt["source_signature"]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE books SET rights='Changed terms' WHERE id='cc0-0'",
        "UPDATE section_roles SET section_translator='Changed translator' "
        "WHERE section_id=(SELECT min(id) FROM sections)",
        "UPDATE index_versions SET fingerprint='changed' WHERE book_id='cc0-0'",
    ],
)
def test_live_provenance_or_attribution_change_rejects_reuse(statement: str, tmp_path: Path) -> None:
    source = seed(tmp_path)
    destination = tmp_path / "subset"
    create_owned_subset(source.data_dir, destination, ["cc0-0"])
    with source.connect() as conn:
        conn.execute(statement)
    with pytest.raises(ValueError, match="Selected original"):
        verify_owned_subset(destination)


def test_default_selection_is_russian_only_and_explicit_wrong_language_fails(tmp_path: Path) -> None:
    source = seed(tmp_path)
    with source.connect() as conn:
        conn.execute("UPDATE books SET language='en' WHERE id='cc0-1'")
    receipt = create_owned_subset(source.data_dir, tmp_path / "russian")
    assert receipt["book_ids"] == ["cc0-0"]
    with pytest.raises(ValueError, match="indexed Russian"):
        create_owned_subset(source.data_dir, tmp_path / "wrong-language", ["cc0-1"])
    assert not list(tmp_path.glob("wrong-language*"))


@pytest.mark.parametrize("selection", [["missing"], ["cc0-0", "cc0-0"], [str(i) for i in range(26)]])
def test_invalid_or_unbounded_selection_cleans_owned_staging(selection: list[str], tmp_path: Path) -> None:
    source = seed(tmp_path)
    with pytest.raises(ValueError):
        create_owned_subset(source.data_dir, tmp_path / "invalid-selection", selection)
    assert not list(tmp_path.glob("invalid-selection*"))


def test_exact_production_window_manifest_replays_checksums_and_rejects_source_drift(tmp_path: Path) -> None:
    source = seed(tmp_path)
    destination = tmp_path / "subset"
    create_owned_subset(source.data_dir, destination, ["cc0-0", "cc0-1"])
    model = SimpleNamespace(max_seq_length=128, tokenizer=CharacterTokenizer())
    path = tmp_path / "windows.sqlite3"
    space = {"model": "owned-fake-tokenizer", "revision": "f" * 40}
    first = prepare_windows(destination, path, model, space)
    records = list(manifest_windows(path))
    assert len(records) == first["rows"] and records[0].window_id == 0
    assert [record.passage_id for record in records] == sorted(record.passage_id for record in records)
    chain = "0" * 64
    for record in records:
        chain = hashlib.sha256(bytes.fromhex(chain) + record.key()).hexdigest()
    assert chain == first["input_chain"]
    assert prepare_windows(destination, path, model, space) == first
    with sqlite3.connect(destination / "library.sqlite3") as conn:
        conn.execute("UPDATE books SET title='Changed owned header' WHERE id='cc0-0'")
    with pytest.raises(ValueError, match="another canonical/model"):
        prepare_windows(destination, path, model, space)


def test_sealed_manifest_corruption_is_rejected_before_encoding(tmp_path: Path) -> None:
    source = seed(tmp_path)
    destination = tmp_path / "subset"
    create_owned_subset(source.data_dir, destination, ["cc0-0"])
    model = SimpleNamespace(max_seq_length=128, tokenizer=CharacterTokenizer())
    path = tmp_path / "windows.sqlite3"
    space = {"model": "fake"}
    prepare_windows(destination, path, model, space)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE windows SET text=text||' changed' WHERE id=0")
    with pytest.raises(ValueError, match="another canonical/model"):
        prepare_windows(destination, path, model, space)
    with pytest.raises(ValueError, match="manifest checksum"):
        list(manifest_windows(path))
    # Even a self-consistent outer hash cannot conceal corrupted row checksums.
    receipt_path = path.with_suffix(path.suffix + ".receipt.json")
    receipt = json.loads(receipt_path.read_text())
    receipt["database_sha256"] = file_sha256(path)
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="input checksum"):
        list(manifest_windows(path))


def test_incomplete_window_manifest_is_rebuilt_and_owned_staging_is_removed(tmp_path: Path) -> None:
    source = seed(tmp_path)
    destination = tmp_path / "subset"
    create_owned_subset(source.data_dir, destination, ["cc0-0"])
    model = SimpleNamespace(max_seq_length=128, tokenizer=CharacterTokenizer())
    path = tmp_path / "windows.sqlite3"
    path.write_bytes(b"Owned interrupted unsealed output")
    receipt = prepare_windows(destination, path, model, {"model": "fake"})
    assert len(list(manifest_windows(path))) == receipt["rows"] > 0
    assert not path.with_name(path.name + ".owned-staging").exists()


def test_manifest_changed_during_replay_fails_before_builder_can_seal(tmp_path: Path) -> None:
    source = seed(tmp_path)
    destination = tmp_path / "subset"
    create_owned_subset(source.data_dir, destination, ["cc0-0"])
    path = tmp_path / "windows.sqlite3"
    prepare_windows(destination, path, SimpleNamespace(max_seq_length=128, tokenizer=CharacterTokenizer()), {})
    replay = manifest_windows(path)
    next(replay)
    # Append after the open cursor starts: SQLite still reads its old coherent
    # snapshot, but the complete-file guard must reject changed durable bytes.
    with path.open("ab") as handle:
        handle.write(b"changed-owned-file")
    with pytest.raises(ValueError, match="changed during replay"):
        list(replay)


def test_corrupt_owned_canonical_and_failed_owned_constructor_are_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = seed(tmp_path)
    destination = tmp_path / "subset"
    create_owned_subset(source.data_dir, destination, ["cc0-0"])
    with sqlite3.connect(destination / "library.sqlite3") as conn:
        conn.execute("UPDATE passages SET text='tampered' WHERE id=(SELECT min(id) FROM passages)")
    with pytest.raises(ValueError, match="database bytes changed"):
        verify_owned_subset(destination)

    def fail(self: Library, directory: Path) -> None:
        directory.mkdir()
        (directory / "owned-partial-file").write_text("owned")
        raise OSError("Owned schema initialization failure")

    monkeypatch.setattr(Library, "__init__", fail)
    failed_destination = tmp_path / "failed-subset"
    with pytest.raises(OSError, match="schema initialization"):
        create_owned_subset(source.data_dir, failed_destination, ["cc0-0"])
    assert not list(tmp_path.glob("failed-subset*"))
