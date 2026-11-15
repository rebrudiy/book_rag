"""Metadata-only coverage, checkpoint and stale-source guards; no real model."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from book_rag import book_discovery as bd


class Tokenizer:
    def __call__(self, text: str, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["truncation"] is False
        return {"offset_mapping": [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]}

    def encode(self, text: str, **kwargs: Any) -> list[int]:
        assert kwargs["truncation"] is False
        return [1] * (len(text.split()) + (2 if kwargs.get("add_special_tokens") else 0))

    def num_special_tokens_to_add(self, **kwargs: Any) -> int:
        return 2


class Encoder:
    tokenizer = Tokenizer()
    max_seq_length = 32

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_at: int | None = None
        self.hook: Any = None

    def encode(self, texts: list[str], **kwargs: Any) -> np.ndarray:
        self.calls.append(texts)
        assert kwargs["normalize_embeddings"] and kwargs["convert_to_numpy"]
        if self.fail_at == len(self.calls):
            raise RuntimeError("simulated interrupt")
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        vectors = np.zeros((len(texts), 384), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in text.casefold().split():
                vectors[row, int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % 384] += 1
            vectors[row] /= np.linalg.norm(vectors[row])
        return vectors


def add_book(directory: Path, bid: str, subject: str, language: str = "en") -> None:
    artifact = directory / (bid + ".txt")
    artifact.write_text("Original book text " + bid)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    with sqlite3.connect(directory / "library.sqlite3") as db:
        db.execute(
            "INSERT INTO books VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                bid,
                bid,
                "Author",
                language,
                bid,
                "Edition",
                "https://www.gutenberg.org/ebooks/11",
                digest,
                digest,
                str(artifact),
                "Public domain in the USA.",
                "indexed",
            ),
        )
        db.execute(
            "INSERT INTO book_metadata VALUES(?,?)",
            (
                bid,
                json.dumps(
                    {
                        "subjects": [subject],
                        "coverage": "full",
                        "work_type": "book_or_text",
                        "work_identity_status": "provisional",
                    }
                ),
            ),
        )
        db.execute("INSERT INTO sections VALUES(?,?,?)", (bid, 0, "Original"))
        db.execute("INSERT INTO passages VALUES(?)", (bid,))


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[bd.ReadOnlyBookLibrary, Encoder]:
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        db.execute(
            "CREATE TABLE books(id,title,author,language,work_id,edition,source_url,"
            "artifact_sha256,content_sha256,local_path,rights,status)"
        )
        db.execute("CREATE TABLE book_metadata(book_id,metadata)")
        db.execute("CREATE TABLE sections(book_id,ordinal,title)")
        db.execute("CREATE TABLE passages(book_id)")
        db.execute("CREATE TABLE private_notes(secret)")
        db.execute('INSERT INTO private_notes VALUES("never read or copied")')
    add_book(tmp_path, "philosophy", "justice philosophy")
    add_book(tmp_path, "science", "chemistry experiments", "fr")
    encoder = Encoder()
    monkeypatch.setattr(bd, "cached_model_path", lambda library: tmp_path)
    monkeypatch.setattr(bd, "model_identity", lambda path: {"model": bd.MINILM_MODEL, "revision": bd.MINILM_REVISION})
    monkeypatch.setattr(bd, "_encoder", lambda path, device: encoder)
    return bd.ReadOnlyBookLibrary(tmp_path), encoder


def test_build_and_search_metadata_cards_language_and_no_source_mutation(
    fixture: tuple[bd.ReadOnlyBookLibrary, Encoder],
) -> None:
    library, encoder = fixture
    before = hashlib.sha256(library.db_path.read_bytes()).hexdigest()
    ready = bd.build_book_discovery(library, batch_size=1)
    assert ready["kind"] == "metadata_semantic" and ready["indexed_editions"] == 2
    result = bd.discover_books(library, "justice philosophy")
    assert result["status"] == "ready" and result["books"][0]["id"] == "philosophy"
    assert result["books"][0]["metadata_evidence"]["subjects"] == ["justice philosophy"]
    assert result["books"][0]["reader_url"] == "/?book=philosophy"
    assert result["books"][0]["matched_fields"] == ["title", "author", "subjects"]
    assert all("quote" not in book and "text" not in book for book in result["books"])
    assert "never read or copied" not in json.dumps(result)
    assert bd.discover_books(library, "chemistry", language="fr")["books"][0]["id"] == "science"
    assert bd.discover_books(library, "chemistry", language="ru")["books"] == []
    assert not ready["database_modified"] and not ready["passage_semantic_coverage_claimed"]
    assert hashlib.sha256(library.db_path.read_bytes()).hexdigest() == before
    calls = len(encoder.calls)
    assert bd.build_book_discovery(library)["reused_ready"] and len(encoder.calls) == calls


def test_interrupted_batch_resumes_prefix_without_reencoding(fixture: tuple[bd.ReadOnlyBookLibrary, Encoder]) -> None:
    library, encoder = fixture
    encoder.fail_at = 2
    with pytest.raises(RuntimeError, match="simulated interrupt"):
        bd.build_book_discovery(library, batch_size=1)
    assert not (library.data_dir / "book-discovery/current.json").exists()
    checkpoint = next((library.data_dir / "book-discovery").glob(".building-*/checkpoint.json"))
    assert json.loads(checkpoint.read_text())["completed"] == 1
    encoder.fail_at = None
    ready = bd.build_book_discovery(library, batch_size=1)
    assert ready["indexed_editions"] == 2 and len(encoder.calls) == 3
    assert encoder.calls[0] != encoder.calls[-1]


@pytest.mark.parametrize("change", ["metadata", "title", "artifact", "status"])
def test_covered_changes_fail_closed_before_search(
    fixture: tuple[bd.ReadOnlyBookLibrary, Encoder], change: str
) -> None:
    library, encoder = fixture
    bd.build_book_discovery(library)
    count = len(encoder.calls)
    if change == "artifact":
        (library.data_dir / "philosophy.txt").unlink()
    else:
        with sqlite3.connect(library.db_path) as db:
            if change == "metadata":
                db.execute(
                    'UPDATE book_metadata SET metadata=? WHERE book_id="philosophy"',
                    (json.dumps({"subjects": ["changed"]}),),
                )
            elif change == "title":
                db.execute('UPDATE books SET title="Changed" WHERE id="philosophy"')
            else:
                db.execute('UPDATE books SET status="failed" WHERE id="philosophy"')
    result = bd.discover_books(library, "philosophy")
    assert result["status"] == "stale" and not result["books"] and result["coverage"]["indexed_editions"] == 0
    assert len(encoder.calls) == count


def test_changes_during_search_discard_results_but_additions_disclose_partial(
    fixture: tuple[bd.ReadOnlyBookLibrary, Encoder],
) -> None:
    library, encoder = fixture
    bd.build_book_discovery(library)
    encoder.hook = lambda: add_book(library.data_dir, "new", "history")
    result = bd.discover_books(library, "philosophy")
    assert result["status"] == "ready" and result["coverage"] == {"indexed_editions": 2, "searchable_editions": 3}
    assert "metadata_index_partial_coverage" in result["warnings"]

    def mutate() -> None:
        with sqlite3.connect(library.db_path) as db:
            db.execute('UPDATE books SET edition="New edition" WHERE id="philosophy"')

    encoder.hook = mutate
    stale = bd.discover_books(library, "philosophy")
    assert stale["status"] == "stale" and not stale["books"]


def test_generation_corruption_and_checkpoint_prefix_corruption_are_rejected(
    fixture: tuple[bd.ReadOnlyBookLibrary, Encoder],
) -> None:
    library, encoder = fixture
    encoder.fail_at = 2
    with pytest.raises(RuntimeError):
        bd.build_book_discovery(library, batch_size=1)
    vector = next((library.data_dir / "book-discovery").glob(".building-*/vectors.npy"))
    matrix = np.load(vector, mmap_mode="r+")
    matrix[0, 0] += 1
    matrix.flush()
    del matrix
    encoder.fail_at = None
    with pytest.raises(bd.DiscoveryUnavailable, match="prefix is corrupt"):
        bd.build_book_discovery(library, batch_size=1)


def test_token_caps_are_explicit_and_query_overflow_is_not_silently_truncated(
    fixture: tuple[bd.ReadOnlyBookLibrary, Encoder],
) -> None:
    library, encoder = fixture
    fields = bd.metadata_fields({"title": "Title " * 100, "author": "Author"}, {"subjects": ["subject " * 100] * 40})
    assert fields["field_truncation"]["title"] and fields["field_truncation"]["subjects"]
    windows, info = bd.metadata_windows(encoder, fields["text"])
    assert len(windows) == 3 and info["token_truncated"]
    assert all(span["tokens"] <= 32 for span in info["window_spans"])
    bd.build_book_discovery(library)
    result = bd.discover_books(library, "word " * 100)
    assert result["status"] == "query_limit" and not result["books"] and result["query_token_info"]["token_truncated"]


def test_missing_artifact_is_not_indexed_and_text_only_flags_remain_truthful(
    fixture: tuple[bd.ReadOnlyBookLibrary, Encoder],
) -> None:
    library, _ = fixture
    (library.data_dir / "philosophy.txt").unlink()
    with sqlite3.connect(library.db_path) as db:
        db.execute(
            'UPDATE book_metadata SET metadata=? WHERE book_id="science"',
            (
                json.dumps(
                    {
                        "subjects": ["chemistry"],
                        "coverage": "full",
                        "illustrations_included": False,
                        "coverage_scope": "text_only_without_illustrations",
                    }
                ),
            ),
        )
    receipt = bd.build_book_discovery(library)
    assert receipt["indexed_editions"] == 1 and receipt["unavailable_source_ids"] == ["philosophy"]
    result = bd.discover_books(library, "chemistry")
    assert result["coverage"] == {"indexed_editions": 1, "searchable_editions": 2}
    assert "metadata_index_partial_coverage" in result["warnings"]
    assert result["books"][0]["illustrations_included"] is False
    assert result["books"][0]["coverage_scope"] == "text_only_without_illustrations"


def test_corrupt_ready_vector_and_unsafe_pointer_fail_closed(fixture: tuple[bd.ReadOnlyBookLibrary, Encoder]) -> None:
    library, _ = fixture
    bd.build_book_discovery(library)
    directory = library.data_dir / "book-discovery"
    pointer = json.loads((directory / "current.json").read_text())
    vector = directory / pointer["generation"] / "vectors.npy"
    with vector.open("r+b") as stream:
        stream.seek(-1, 2)
        value = stream.read(1)
        stream.seek(-1, 2)
        stream.write(bytes([value[0] ^ 1]))
    result = bd.discover_books(library, "philosophy")
    assert result["status"] == "stale" and not result["books"] and "integrity" in result["error"]
    (directory / "current.json").write_text(
        json.dumps({"generation": "generation-../../outside", "ready_sha256": "a" * 64})
    )
    assert bd.discover_books(library, "philosophy")["status"] == "stale"


def test_build_does_not_activate_when_metadata_changes_during_encoding(
    fixture: tuple[bd.ReadOnlyBookLibrary, Encoder],
) -> None:
    library, encoder = fixture

    def mutate() -> None:
        with sqlite3.connect(library.db_path) as db:
            db.execute('UPDATE books SET author="Changed" WHERE id="philosophy"')

    encoder.hook = mutate
    with pytest.raises(bd.DiscoveryUnavailable, match="changed during discovery build"):
        bd.build_book_discovery(library)
    assert not (library.data_dir / "book-discovery/current.json").exists()
