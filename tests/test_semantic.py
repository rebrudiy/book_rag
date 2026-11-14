"""Window coverage regressions without downloading a model in CI."""

from pathlib import Path

import numpy as np
import pytest

from book_rag.library import Library
from book_rag.semantic import corpus_fingerprint, token_windows


def test_windows_cover_every_token_and_last_evidence() -> None:
    text = " ".join(f"w{i}" for i in range(300))
    offsets = []
    position = 0
    for word in text.split():
        offsets.append((position, position + len(word)))
        position += len(word) + 1
    windows = list(token_windows(text, offsets, budget=100, overlap=20))
    assert len(windows) == 4
    assert windows[-1].endswith("w299")
    assert all(len(w.split()) <= 100 for w in windows)
    assert set(text.split()) == {word for window in windows for word in window.split()}
    assert windows[0].split()[-20:] == windows[1].split()[:20]


def test_windows_empty_unicode_and_invalid_budget() -> None:
    assert list(token_windows("", [], budget=100)) == []
    assert list(token_windows("Я 🌏", [(0, 1), (2, 3)], budget=30)) == ["Я 🌏"]
    with pytest.raises(ValueError):
        list(token_windows("x", [(0, 1)], budget=10, overlap=10))


def test_fingerprint_invalidates_on_edition_change(tmp_path: Path) -> None:
    library = Library(tmp_path / "lib")
    path = tmp_path / "book.txt"
    path.write_text("A text.")
    library.import_book(path, {"id": "b", "title": "Title", "author": "A"})
    before = corpus_fingerprint(library)
    library.import_book(path, {"id": "b", "title": "A new title", "author": "B"})
    assert corpus_fingerprint(library) != before


def test_stale_index_falls_back_without_model(tmp_path: Path) -> None:
    import json

    library = Library(tmp_path / "lib")
    (library.data_dir / "vectors").mkdir()
    (library.data_dir / "vectors" / "active.json").write_text(json.dumps({"corpus_fingerprint": "stale"}))
    result = library.search("anything", mode="semantic")
    assert result["mode"] == "lexical" and "stale" in result["warnings"][0]
    assert np.isfinite(result["elapsed_ms"])
