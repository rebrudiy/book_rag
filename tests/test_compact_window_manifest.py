"""Original CC0 fixtures; exact offsets survive repeated and Unicode text."""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from book_rag.compact_window_manifest import offset_windows, repack_window_manifest, verify_compact_manifest
from book_rag.library import Library
from book_rag.semantic_batches import prepare_batch_windows
from book_rag.semantic_pilot import ReadOnlyLibrary


class Tokenizer:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    def encode(self, text: str, **kwargs: Any) -> list[int]:
        result = []
        for token in text.split():
            if token not in self.tokens:
                self.tokens.append(token)
            result.append(self.tokens.index(token))
        return result

    def decode(self, ids: list[int], **kwargs: Any) -> str:
        return " ".join(self.tokens[index] for index in ids)

    def __call__(self, text: str, **kwargs: Any) -> dict[str, list[tuple[int, int]]]:
        return {"offset_mapping": [match.span() for match in re.finditer(r"\S+", text)]}


@pytest.fixture
def original_manifest(tmp_path: Path) -> tuple[Library, Path, Tokenizer]:
    source = Library(tmp_path / "source")
    text = tmp_path / "cc0.html"
    text.write_text(
        "<h1>Оригинальный повтор CC0 🦉</h1><h2>Глава один</h2><p>"
        + "эхо 🦉 " * 140
        + "</p><h2>Глава два</h2><p>Повтор и Unicode проверены по смещениям.</p>"
    )
    source.import_book(
        text,
        {
            "id": "cc0-repeated",
            "title": "CC0 original repeat",
            "author": "Original CC0",
            "language": "ru",
            "rights": "CC0",
        },
    )
    tokenizer = Tokenizer()

    class Model:
        max_seq_length = 128

        def __init__(self) -> None:
            self.tokenizer = tokenizer

    original = tmp_path / "old-batch" / "windows.sqlite3"
    prepare_batch_windows(
        source.data_dir,
        original,
        ["cc0-repeated"],
        Model(),
        {"model": "test-only-no-model", "revision": "cc0", "model_files_sha256": ["a" * 64]},
        reserve_bytes=0,
    )
    return source, original, tokenizer


def test_all_repeated_unicode_windows_roundtrip_without_body_text_storage(
    original_manifest: tuple[Library, Path, Tokenizer], tmp_path: Path
) -> None:
    source, original, tokenizer = original_manifest
    original_bytes = original.read_bytes()
    output = tmp_path / "compact" / "windows.sqlite3"
    receipt = repack_window_manifest(source.data_dir, original, output, tokenizer, reserve_bytes=0)
    assert original.read_bytes() == original_bytes
    assert receipt["rows"] > 2 and receipt["reconstructed_rows_verified_twice"] == receipt["rows"]
    assert receipt["input_chain"] == receipt["original_receipt"]["input_chain"]
    assert receipt["body_text_stored"] is False and receipt["production_activation"] is False
    with sqlite3.connect(output) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(windows)")]
        assert "text" not in columns and "input_sha256" in columns
        assert connection.execute(
            "SELECT MIN(length(input_sha256)),MAX(length(input_sha256)) FROM windows"
        ).fetchone() == (32, 32)
        spans = connection.execute("SELECT passage_id,start,end FROM windows ORDER BY id").fetchall()
        assert any(left[0] == right[0] and left[1] != right[1] for left, right in zip(spans, spans[1:], strict=False))
    verify_compact_manifest(ReadOnlyLibrary(source.data_dir), output, original, receipt)


def test_wrong_token_offsets_fail_closed_before_publication(
    original_manifest: tuple[Library, Path, Tokenizer], tmp_path: Path
) -> None:
    source, original, tokenizer = original_manifest

    class WrongOffsets(Tokenizer):
        def __call__(self, text: str, **kwargs: Any) -> dict[str, list[tuple[int, int]]]:
            return {"offset_mapping": [(0, 1)]}

    wrong = WrongOffsets()
    wrong.tokens = tokenizer.tokens
    output = tmp_path / "compact" / "bad.sqlite3"
    with pytest.raises(ValueError, match="do not reproduce"):
        repack_window_manifest(source.data_dir, original, output, wrong, reserve_bytes=0)
    assert not output.exists() and not output.with_suffix(".sqlite3.receipt.json").exists()


def test_compact_corruption_is_detected(original_manifest: tuple[Library, Path, Tokenizer], tmp_path: Path) -> None:
    source, original, tokenizer = original_manifest
    output = tmp_path / "compact" / "windows.sqlite3"
    receipt = repack_window_manifest(source.data_dir, original, output, tokenizer, reserve_bytes=0)
    with sqlite3.connect(output) as connection:
        connection.execute("UPDATE windows SET start=start+1 WHERE id=0")
    # Exercise row reconstruction independently of the sealed-file hash guard.
    receipt.pop("database_sha256")
    with pytest.raises(ValueError, match="differs"):
        verify_compact_manifest(ReadOnlyLibrary(source.data_dir), output, original, receipt)


def test_source_mutation_invalidates_reconstruction(
    original_manifest: tuple[Library, Path, Tokenizer], tmp_path: Path
) -> None:
    source, original, tokenizer = original_manifest
    output = tmp_path / "compact" / "windows.sqlite3"
    receipt = repack_window_manifest(source.data_dir, original, output, tokenizer, reserve_bytes=0)
    changed = tmp_path / "changed.txt"
    changed.write_text("Changed original CC0 source text.")
    source.import_book(changed, {"id": "cc0-repeated", "title": "Changed", "author": "CC0", "rights": "CC0"})
    with pytest.raises(ValueError, match="changed"):
        verify_compact_manifest(ReadOnlyLibrary(source.data_dir), output, original, receipt)


def test_original_checksum_and_output_location_guards(
    original_manifest: tuple[Library, Path, Tokenizer], tmp_path: Path
) -> None:
    source, original, tokenizer = original_manifest
    for output in [original, original.parent / "new.sqlite3", source.data_dir / "new.sqlite3"]:
        with pytest.raises(ValueError, match="outside"):
            repack_window_manifest(source.data_dir, original, output, tokenizer, reserve_bytes=0)
    sidecar = original.with_suffix(".sqlite3.receipt.json")
    receipt = json.loads(sidecar.read_text())
    receipt["database_sha256"] = "0" * 64
    sidecar.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="hash changed"):
        repack_window_manifest(source.data_dir, original, tmp_path / "new.sqlite3", tokenizer, reserve_bytes=0)


def test_offset_recipe_rejects_a_different_token_budget() -> None:
    with pytest.raises(ValueError, match="128-token"):
        list(offset_windows(Tokenizer(), {}, max_seq_length=512))


def test_different_tokenizer_file_pin_cannot_publish(
    original_manifest: tuple[Library, Path, Tokenizer], tmp_path: Path
) -> None:
    source, original, tokenizer = original_manifest
    output = tmp_path / "compact" / "bad-pin.sqlite3"
    pin = {"model": "test-only-no-model", "revision": "cc0", "files_sha256": {"tokenizer.json": "b" * 64}}
    with pytest.raises(ValueError, match="Tokenizer files differ"):
        repack_window_manifest(source.data_dir, original, output, tokenizer, tokenizer_receipt=pin, reserve_bytes=0)
    assert not output.exists()


@pytest.mark.parametrize("mutation", ["extra_orphan", "missing_book", "missing_prefix"])
def test_raw_orphan_windows_cannot_disappear_behind_joins(
    original_manifest: tuple[Library, Path, Tokenizer], tmp_path: Path, mutation: str
) -> None:
    source, original, tokenizer = original_manifest
    output = tmp_path / "compact" / "windows.sqlite3"
    receipt = repack_window_manifest(source.data_dir, original, output, tokenizer, reserve_bytes=0)
    receipt.pop("database_sha256")  # Staging verification must enforce every row, too.
    with sqlite3.connect(output) as connection:
        if mutation == "extra_orphan":
            connection.execute(
                "INSERT INTO windows SELECT 999999,passage_id,999999,999999,start,end,input_sha256 "
                "FROM windows WHERE id=0"
            )
        elif mutation == "missing_book":
            connection.execute("UPDATE windows SET book_index=999999 WHERE id=0")
        else:
            connection.execute("UPDATE windows SET prefix_id=999999 WHERE id=0")
    with pytest.raises(ValueError, match="raw row count|missing edition or prefix"):
        verify_compact_manifest(ReadOnlyLibrary(source.data_dir), output, original, receipt)


def test_sealed_file_hash_covers_non_window_bytes(
    original_manifest: tuple[Library, Path, Tokenizer], tmp_path: Path
) -> None:
    source, original, tokenizer = original_manifest
    output = tmp_path / "compact" / "windows.sqlite3"
    receipt = repack_window_manifest(source.data_dir, original, output, tokenizer, reserve_bytes=0)
    with output.open("ab") as handle:
        handle.write(b"changed non-window bytes")
    with pytest.raises(ValueError, match="checksum changed"):
        verify_compact_manifest(ReadOnlyLibrary(source.data_dir), output, original, receipt)


def test_sealed_file_hash_is_rechecked_after_streaming(
    original_manifest: tuple[Library, Path, Tokenizer], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from book_rag import compact_window_manifest as module

    source, original, tokenizer = original_manifest
    output = tmp_path / "compact" / "windows.sqlite3"
    receipt = repack_window_manifest(source.data_dir, original, output, tokenizer, reserve_bytes=0)
    verify = module.verify_batch_source
    calls = 0

    def change_after_stream(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        verify(*args, **kwargs)
        calls += 1
        if calls == 2:
            with output.open("ab") as handle:
                handle.write(b"changed after reconstruction")

    monkeypatch.setattr(module, "verify_batch_source", change_after_stream)
    with pytest.raises(ValueError, match="checksum changed during"):
        verify_compact_manifest(ReadOnlyLibrary(source.data_dir), output, original, receipt)


@pytest.mark.parametrize("replace_output", [False, True])
def test_receipt_publication_failure_removes_only_the_owned_output_inode(
    original_manifest: tuple[Library, Path, Tokenizer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_output: bool,
) -> None:
    from book_rag import compact_window_manifest as module

    source, original, tokenizer = original_manifest
    original_bytes = original.read_bytes()
    output = tmp_path / "compact" / "windows.sqlite3"

    def fail_publish(*args: Any, **kwargs: Any) -> None:
        assert output.exists(), "Inject failure after os.link publishes the owned inode"
        if replace_output:
            output.unlink()
            output.write_bytes(b"replacement owned by another operation")
        raise OSError("Original CC0 simulated publication failure")

    monkeypatch.setattr(module, "_publish_json", fail_publish)
    with pytest.raises(OSError, match="publication failure"):
        repack_window_manifest(source.data_dir, original, output, tokenizer, reserve_bytes=0)
    assert original.read_bytes() == original_bytes
    assert not output.with_suffix(".sqlite3.receipt.json").exists()
    assert not list(output.parent.glob("*.owned-staging-*"))
    if replace_output:
        assert output.read_bytes() == b"replacement owned by another operation"
    else:
        assert not output.exists()


@pytest.mark.parametrize("during_repack", [False, True])
def test_cli_probe_symlink_never_overwrites_an_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during_repack: bool
) -> None:
    from book_rag import compact_window_manifest as module

    path = Path(__file__).resolve().parents[1] / "scripts/repack_window_manifest.py"
    spec = importlib.util.spec_from_file_location("owned_repack_cli_test", path)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    original = tmp_path / "original.txt"
    original.write_bytes(b"Original CC0 must never be overwritten")
    output = tmp_path / "compact.sqlite3"
    probe = output.with_suffix(".sqlite3.probe.json")
    if not during_repack:
        probe.symlink_to(original)
    calls = []

    def fake_tokenizer(*args: Any, **kwargs: Any) -> tuple[None, dict[str, Any]]:
        calls.append("tokenizer")
        return None, {}

    def fake_repack(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("repack")
        probe.symlink_to(original)  # Simulate a race after the initial absent-path check.
        return {
            "kind": "CC0 fake protocol",
            "rows": 1,
            "books": 1,
            "prefixes": 1,
            "input_chain": "a" * 64,
            "database_sha256": "b" * 64,
            "original_bytes": 2,
            "compact_bytes": 1,
            "elapsed_seconds": 0,
            "source_guards_verified_before_after": True,
            "free_bytes_after": 100,
        }

    monkeypatch.setattr(module, "load_pinned_tokenizer", fake_tokenizer)
    monkeypatch.setattr(module, "repack_window_manifest", fake_repack)
    for name in ("torch", "faiss", "llama_cpp"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(
        sys, "argv", [str(path), "--source", str(tmp_path), "--original", str(original), "--output", str(output)]
    )
    with pytest.raises(FileExistsError):
        cli.main()
    assert original.read_bytes() == b"Original CC0 must never be overwritten"
    assert probe.is_symlink()
    assert calls == (["tokenizer", "repack"] if during_repack else [])
