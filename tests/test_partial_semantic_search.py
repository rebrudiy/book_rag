"""Real installed subset queries retain global lexical coverage and exact sources."""

import json
from pathlib import Path

import numpy as np
import pytest
from test_index_installation import seed

from book_rag import semantic
from book_rag.index_installation import install_partial_index
from book_rag.library import Library


def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, target, model = seed(tmp_path, monkeypatch)
    metadata = install_partial_index(source.data_dir, target.data_dir)
    return source, target, model, metadata


def test_partial_hybrid_keeps_global_words_and_uses_epoch_guards_without_full_scan(tmp_path: Path, monkeypatch) -> None:
    _, target, _, _ = installed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        semantic, "corpus_fingerprint", lambda _: pytest.fail("Subset queries must not scan every passage")
    )
    result = target.search("additional", mode="hybrid")
    assert result["mode"] == "hybrid" and any(hit["book_id"] == "outside" for hit in result["results"])
    assert all(hit["verified"] for hit in result["results"])
    assert result["semantic_coverage"] == {
        "indexed_books": 1,
        "searchable_books": 2,
        "active": True,
        "requested_book_covered": None,
    }
    path = tmp_path / "new-outside.txt"
    path.write_text("A newly indexed edition remains available through global words.")
    target.import_book(path, {"id": "new-outside"})
    again = target.search("newly", mode="hybrid")
    assert again["mode"] == "hybrid" and again["semantic_coverage"]["searchable_books"] == 3
    assert any(hit["book_id"] == "new-outside" for hit in again["results"])


def test_outside_book_uses_explicit_lexical_fallback_without_loading_model(tmp_path: Path, monkeypatch) -> None:
    _, target, _, _ = installed(tmp_path, monkeypatch)
    monkeypatch.setattr(semantic, "_model", lambda *args: pytest.fail("Outside scope does not need a model"))
    result = target.search("additional", mode="semantic", book_id="outside")
    assert result["mode"] == "lexical" and result["warnings"]
    assert result["semantic_coverage"]["requested_book_covered"] is False
    assert result["results"] and all(hit["book_id"] == "outside" and hit["verified"] for hit in result["results"])


def test_failed_refresh_and_annotations_do_not_invalidate_covered_text(tmp_path: Path, monkeypatch) -> None:
    _, target, _, _ = installed(tmp_path, monkeypatch)
    hit = target.search("unique0001", book_id="covered")["results"][0]
    saved = target.save({"kind": "passage", "passage_id": hit["id"]})
    target.update_saved(saved["id"], {"note": "A private annotation unrelated to index inputs"})
    broken = tmp_path / "broken.txt"
    broken.write_text("")
    with pytest.raises(ValueError):
        target.import_book(broken, {"id": "covered"})
    result = target.search("unique0001", mode="hybrid", book_id="covered")
    assert result["mode"] == "hybrid" and result["semantic_coverage"]["active"]
    assert result["semantic_coverage"]["requested_book_covered"] is True
    assert all(hit["verified"] for hit in result["results"])


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_uncovered_language_keeps_words_before_loading_model(tmp_path: Path, monkeypatch, mode: str) -> None:
    _, target, _, _ = installed(tmp_path, monkeypatch)
    source = tmp_path / "spanish.txt"
    source.write_text("Una biblioteca conserva evidencia original sobre mariposas.")
    target.import_book(source, {"id": "spanish", "language": "es"})
    monkeypatch.setattr(semantic, "_model", lambda *args: pytest.fail("Uncovered language does not need a model"))
    result = target.search("mariposas", mode=mode, language="es")
    assert result["mode"] == "lexical" and result["semantic_coverage"]["active"]
    assert any("языка" in warning for warning in result["warnings"])
    assert result["results"] and all(hit["book_id"] == "spanish" and hit["verified"] for hit in result["results"])


def test_changed_covered_input_falls_back_without_automatically_rebinding(tmp_path: Path, monkeypatch) -> None:
    _, target, _, _ = installed(tmp_path, monkeypatch)
    with target.connect() as conn:
        conn.execute("UPDATE passages SET text=text||' changed' WHERE book_id='covered'")
    monkeypatch.setattr(semantic, "_model", lambda *args: pytest.fail("Changed coverage must fail before encoding"))
    result = target.search("additional", mode="hybrid")
    assert result["mode"] == "lexical" and "semantic_coverage" not in result
    assert all(hit["verified"] for hit in result["results"])


def test_covered_change_during_inference_discards_old_candidates(tmp_path: Path, monkeypatch) -> None:
    _, target, model, _ = installed(tmp_path, monkeypatch)
    encode = model.encode

    def changed(texts, **kwargs):
        source = tmp_path / "replacement.txt"
        source.write_text("Replacement unique0001 evidence in a fresh current edition.")
        target.import_book(source, {"id": "covered", "title": "Fresh edition"})
        return encode(texts, **kwargs)

    monkeypatch.setattr(model, "encode", changed)
    result = target.search("unique0001", mode="hybrid")
    assert result["mode"] == "lexical" and "semantic_coverage" not in result
    assert any("изменился" in warning for warning in result["warnings"])
    assert result["results"] and all(hit["verified"] for hit in result["results"])
    assert result["results"][0]["title"] == "Fresh edition"


@pytest.mark.parametrize("corruption", ["receipt", "metadata", "vectors"])
def test_unavailable_semantics_remains_a_working_word_search(tmp_path: Path, monkeypatch, corruption: str) -> None:
    _, target, _, metadata = installed(tmp_path, monkeypatch)
    if corruption == "receipt":
        path = target.data_dir / "vectors" / metadata["installation_receipt"]["path"]
    elif corruption == "metadata":
        path = target.data_dir / "vectors" / "active.json"
    else:
        path = target.data_dir / "vectors" / metadata["generation"] / "embeddings.npy"
    replacement = path.with_name(path.name + ".corrupt-test")
    replacement.write_bytes(b"invalid")
    replacement.replace(path)
    result = target.search("additional", mode="hybrid")
    assert result["mode"] == "lexical" and result["warnings"] and "semantic_coverage" not in result
    assert result["results"] and all(hit["verified"] for hit in result["results"])


def test_legacy_nonfinite_matrix_falls_back_with_verified_words(tmp_path: Path, monkeypatch) -> None:
    library = Library(tmp_path / "legacy")
    source = tmp_path / "source.txt"
    source.write_text("Genuine needle evidence remains searchable.")
    library.import_book(source, {"id": "book"})
    generation = library.data_dir / "vectors" / "legacy"
    generation.mkdir(parents=True)
    np.save(generation / "embeddings.npy", np.array([[np.nan, 0]], dtype="float32"))
    np.save(generation / "ids.npy", np.array([1], dtype="int64"))
    (generation.parent / "active.json").write_text(
        json.dumps(
            {
                "generation": "legacy",
                "model": semantic.DEFAULT_MODEL,
                "corpus_fingerprint": semantic.corpus_fingerprint(library),
            }
        )
    )
    monkeypatch.setattr(
        semantic,
        "_model",
        lambda *args: type("Encoder", (), {"encode": lambda *args, **kwargs: np.array([[1, 0]], dtype="float32")})(),
    )
    result = library.search("needle", mode="hybrid")
    assert result["mode"] == "lexical" and result["results"][0]["verified"]
