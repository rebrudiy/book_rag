from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from book_rag.experimental_ivfpq import file_sha256
from book_rag.library import Library
from book_rag.semantic_batches import batch_source_state, prepare_batch_windows, verify_batch_source
from book_rag.semantic_pilot import ReadOnlyLibrary, manifest_windows


class Tokenizer:
    def encode(self, text: str, **kwargs: Any) -> list[int]:
        return [ord(value) for value in text]

    def decode(self, values: list[int], **kwargs: Any) -> str:
        return "".join(chr(value) for value in values)

    def __call__(self, text: str, **kwargs: Any) -> dict[str, Any]:
        return {"offset_mapping": [(index, index + 1) for index in range(len(text))]}


@pytest.fixture
def owned(tmp_path: Path) -> Library:
    source = Library(tmp_path / "original")
    for identity, language, text in [
        ("cc0-fr", "fr", "La clef bleue est dans la tasse verte."),
        ("cc0-de", "de", "Der silberne Schlüssel liegt unter der Brücke."),
        ("cc0-outside", "en", "An unrelated complete CC0 story."),
    ]:
        path = tmp_path / (identity + ".txt")
        path.write_text(text + "\n\n" + text * 12)
        source.import_book(
            path,
            {
                "id": identity,
                "title": identity,
                "author": "Original CC0 test author",
                "language": language,
                "edition": "Complete original CC0 edition",
                "rights": "CC0-1.0",
                "coverage": "full",
                "source_url": "https://example.invalid/" + identity,
                "rights_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            },
        )
    return source


def prepare(source: Library, target: Path, **kwargs: Any) -> dict[str, Any]:
    return prepare_batch_windows(
        source.data_dir,
        target,
        ["cc0-fr", "cc0-de"],
        SimpleNamespace(max_seq_length=128, tokenizer=Tokenizer()),
        {"model": "owned-test-tokenizer", "revision": "a" * 40},
        reserve_bytes=0,
        **kwargs,
    )


def test_explicit_multilingual_complete_stream_has_no_original_constructor_or_vector_copy(
    owned: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = file_sha256(owned.db_path)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Never initialize or migrate any original database")

    monkeypatch.setattr(Library, "__init__", forbidden)
    target = tmp_path / "batch" / "windows.sqlite3"
    receipt = prepare(owned, target)
    records = list(manifest_windows(target))
    with owned.connect() as conn:
        expected = {row[0] for row in conn.execute("SELECT id FROM passages WHERE book_id IN ('cc0-fr','cc0-de')")}
    assert expected == {row.passage_id for row in records}
    assert {row.book_id for row in records} == {"cc0-fr", "cc0-de"}
    assert [row.passage_id for row in records] == sorted(row.passage_id for row in records)
    assert len(records) == receipt["rows"] == sum(receipt["rows_by_book"].values())
    assert not receipt["source_database_copied"] and not receipt["embedding_vectors_stored"]
    assert file_sha256(owned.db_path) == before
    assert prepare(owned, target) == receipt


@pytest.mark.parametrize("selection", [[], ["cc0-fr", "cc0-fr"], ["missing"], [True], [" "]])
def test_invalid_selection_fails_before_output(owned: Library, tmp_path: Path, selection: list[Any]) -> None:
    with pytest.raises(ValueError):
        prepare_batch_windows(
            owned.data_dir,
            tmp_path / "windows.sqlite3",
            selection,
            SimpleNamespace(max_seq_length=128, tokenizer=Tokenizer()),
            {},
            reserve_bytes=0,
        )
    assert not (tmp_path / "windows.sqlite3").exists()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE books SET edition='Changed edition' WHERE id='cc0-fr'",
        "UPDATE books SET rights='Changed rights' WHERE id='cc0-fr'",
        "UPDATE book_metadata SET metadata='{}' WHERE book_id='cc0-fr'",
        "UPDATE section_roles SET section_translator='Changed translator' "
        "WHERE section_id=(SELECT min(id) FROM sections)",
        "UPDATE index_versions SET fingerprint='different' WHERE book_id='cc0-fr'",
        "UPDATE passages SET text='Changed canonical text' WHERE book_id='cc0-fr'",
        "UPDATE books SET status='failed' WHERE id='cc0-fr'",
    ],
)
def test_covered_text_origin_attribution_or_status_change_rejects_reuse(
    owned: Library, tmp_path: Path, statement: str
) -> None:
    target = tmp_path / "windows.sqlite3"
    receipt = prepare(owned, target)
    with owned.connect() as conn:
        conn.execute(statement)
    with pytest.raises(ValueError):
        verify_batch_source(ReadOnlyLibrary(owned.data_dir), receipt)
    with pytest.raises(ValueError):
        prepare(owned, target)


def test_unrelated_changes_and_failed_refresh_preserve_batch(owned: Library, tmp_path: Path) -> None:
    target = tmp_path / "windows.sqlite3"
    receipt = prepare(owned, target)
    with owned.connect() as conn:
        conn.execute("UPDATE books SET title='Unrelated update' WHERE id='cc0-outside'")
    owned.stage("cc0-fr", "failed", "Controlled failed refresh")
    verify_batch_source(ReadOnlyLibrary(owned.data_dir), receipt)
    assert prepare(owned, target) == receipt


def test_manifest_payload_or_sidecar_corruption_fails_closed(owned: Library, tmp_path: Path) -> None:
    target = tmp_path / "windows.sqlite3"
    prepare(owned, target)
    with sqlite3.connect(target) as conn:
        conn.execute("UPDATE windows SET text='corrupt' WHERE id=0")
    with pytest.raises(ValueError, match="checksum"):
        prepare(owned, target)


def test_crash_after_sqlite_publish_recovers_internal_complete_receipt(owned: Library, tmp_path: Path) -> None:
    target = tmp_path / "windows.sqlite3"
    receipt = prepare(owned, target)
    target.with_suffix(".sqlite3.receipt.json").unlink()
    assert prepare(owned, target) == receipt
    assert json.loads(target.with_suffix(".sqlite3.receipt.json").read_text()) == receipt


def test_bounded_windows_abort_without_publishing_incomplete_editions(owned: Library, tmp_path: Path) -> None:
    target = tmp_path / "windows.sqlite3"
    with pytest.raises(ValueError, match="window limit"):
        prepare(owned, target, max_windows=1)
    assert not target.exists() and not target.with_suffix(".sqlite3.receipt.json").exists()
    assert not list(tmp_path.glob("*.owned-staging-*"))


def test_canonical_drift_during_tokenization_never_publishes(owned: Library, tmp_path: Path) -> None:
    class ChangingTokenizer(Tokenizer):
        changed = False

        def __call__(self, text: str, **kwargs: Any) -> dict[str, Any]:
            if not self.changed:
                with owned.connect() as conn:
                    conn.execute("UPDATE books SET rights='Changed during tokenization' WHERE id='cc0-fr'")
                self.changed = True
            return super().__call__(text, **kwargs)

    target = tmp_path / "windows.sqlite3"
    with pytest.raises(ValueError, match="changed"):
        prepare_batch_windows(
            owned.data_dir,
            target,
            ["cc0-fr"],
            SimpleNamespace(max_seq_length=128, tokenizer=ChangingTokenizer()),
            {},
            reserve_bytes=0,
        )
    assert not target.exists()


def test_original_directory_or_symlink_output_is_rejected(owned: Library, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        prepare(owned, owned.data_dir / "windows.sqlite3")
    target = tmp_path / "windows.sqlite3"
    target.symlink_to(owned.db_path)
    with pytest.raises(ValueError, match="outside"):
        prepare(owned, target)


def test_database_inode_replacement_is_not_an_automatic_rebind(owned: Library, tmp_path: Path) -> None:
    import shutil

    before = batch_source_state(ReadOnlyLibrary(owned.data_dir), ["cc0-fr"])
    copied = tmp_path / "replacement.sqlite3"
    shutil.copyfile(owned.db_path, copied)
    copied.replace(owned.db_path)
    after = batch_source_state(ReadOnlyLibrary(owned.data_dir), ["cc0-fr"])
    assert before != after and before["database_inode"] != after["database_inode"]


def load_cli() -> Any:
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "build_semantic_batch.py"
    spec = importlib.util.spec_from_file_location("bookrag_batch_cli_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hung_native_phase_has_a_deadline_and_closes_its_process_and_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = load_cli()

    class Pipe:
        closed = False

        def poll(self, timeout: float) -> bool:
            return False

        def close(self) -> None:
            self.closed = True

    class Process:
        pid = 123
        alive = True
        terminated = False

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated, self.alive = True, False

        def join(self, timeout: float) -> None:
            pass

    parent, child, process = Pipe(), Pipe(), Process()
    context = SimpleNamespace(Pipe=lambda: (parent, child), Process=lambda **kwargs: process)
    monkeypatch.setattr(cli.multiprocessing, "get_context", lambda method: context)
    times = iter([0.0, 2.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
    service = SimpleNamespace(connection=None, process=SimpleNamespace(pid=456))
    with pytest.raises(TimeoutError, match="wall-clock"):
        cli.run_phase(service, {}, tmp_path, 1024**2, phase_timeout=1)
    assert process.terminated and parent.closed and child.closed


@pytest.mark.parametrize("value", [True, 0, float("inf"), float("nan"), 86401])
def test_invalid_native_deadline_does_not_start_workers(value: float, tmp_path: Path) -> None:
    cli = load_cli()
    with pytest.raises(ValueError, match="timeout"):
        cli.run_phase(None, {}, tmp_path, 1000, phase_timeout=value)


@pytest.mark.parametrize("resource_failure", [False, True])
def test_interrupted_encoder_protocol_is_aborted_and_failure_receipt_is_saved(
    owned: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_failure: bool
) -> None:
    import hashlib
    import sys

    cli = load_cli()
    codec_dir = tmp_path / "codec"
    codec_dir.mkdir()
    codec = codec_dir / "codec.faiss"
    codec.write_bytes(b"Owned fake codec; native backend is never invoked in this failure test")
    space = {"model": "test", "revision": "a" * 40, "profile": {}, "strategy": "test"}
    report_path = tmp_path / "codec-report.json"
    report_path.write_text(
        json.dumps(
            {
                "source": {"model": "test", "model_revision": "a" * 40, "embedding_profile": {}, "strategy": "test"},
                "result": {
                    "build": {
                        "directory": str(codec_dir),
                        "shards": [{"codec_sha256": hashlib.sha256(codec.read_bytes()).hexdigest()}],
                    }
                },
            }
        )
    )
    ids = tmp_path / "ids.json"
    ids.write_text('["cc0-fr"]')
    output = tmp_path / "batch"
    state = {"aborted": False, "close_called": False, "resource_calls": 0}

    class Encoder:
        connection = None
        metadata = {"model_space": space}

        def __init__(self, *args: Any) -> None:
            pass

        def abort(self) -> None:
            state["aborted"] = True

        def close(self) -> Any:
            state["close_called"] = True
            raise AssertionError("A partial encode reply must not be read as a shutdown receipt")

    def rpc(connection: Any, request: dict, timeout: float = 45) -> dict:
        if request["operation"] == "prepare_batch":
            return {"rows": 1, "rows_by_book": {"cc0-fr": 1}, "input_chain": "0" * 64, "database_sha256": "0" * 64}
        return {"rows_encoded": 0}

    def phase(*args: Any, **kwargs: Any) -> None:
        raise TimeoutError("Owned controlled native timeout")

    def resources(*args: Any) -> dict:
        state["resource_calls"] += 1
        if resource_failure and state["resource_calls"] > 1:
            raise RuntimeError("Owned controlled resource failure during final recording")
        return {"owned_output_bytes": 0, "free_disk_bytes": 10 * 1024**3}

    monkeypatch.setattr(cli, "EncoderService", Encoder)
    monkeypatch.setattr(cli, "rpc", rpc)
    monkeypatch.setattr(cli, "run_phase", phase)
    monkeypatch.setattr(cli, "_resources", resources)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_semantic_batch",
            "--source",
            str(owned.data_dir),
            "--book-ids-from",
            str(ids),
            "--output",
            str(output),
            "--codec-report",
            str(report_path),
            "--device",
            "cpu",
        ],
    )
    with pytest.raises(TimeoutError, match="controlled native"):
        cli.main()
    receipts = list(output.glob("run-*.json"))
    assert len(receipts) == 1
    recorded = json.loads(receipts[0].read_text())
    assert recorded["status"] == "failed" and recorded["encoder_aborted_after_failure"]
    assert "controlled native" in recorded["error"]
    assert ("resource_error" in recorded) == resource_failure
    assert state["aborted"] and not state["close_called"]
