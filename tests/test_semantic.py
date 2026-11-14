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


class FakeTokenizer:
    """Whitespace offsets preserve exact substrings without optional model packages."""

    def encode(self, text: str, **kwargs: object) -> list[str]:
        return text.split()

    def decode(self, ids: list[str], **kwargs: object) -> str:
        return " ".join(ids)

    def __call__(self, text: str, **kwargs: object) -> dict[str, list[tuple[int, int]]]:
        import re

        return {"offset_mapping": [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]}


class FakeModel:
    """Deterministic vectors and an interruptible encoder for disk-resume checks."""

    max_seq_length = 64

    def __init__(self, fail_after: int | None = None) -> None:
        self.tokenizer = FakeTokenizer()
        self.fail_after = fail_after
        self.calls = 0
        self.encoded: list[str] = []

    def get_embedding_dimension(self) -> int:
        return 4

    def encode(self, texts: list[str], **kwargs: object) -> np.ndarray:
        import hashlib

        if self.fail_after is not None and self.calls >= self.fail_after:
            raise KeyboardInterrupt("Simulated embedding interruption")
        self.calls += 1
        self.encoded.extend(texts)
        vectors = np.array(
            [[value + 1 for value in hashlib.sha256(text.encode()).digest()[:4]] for text in texts], dtype="float32"
        )
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def embedding_library(tmp_path: Path) -> Library:
    library = Library(tmp_path / "library")
    path = tmp_path / "source.txt"
    path.write_text(
        "\n\n".join(" ".join(f"word{index:04d}" for index in range(start, start + 100)) for start in range(0, 700, 100))
    )
    library.import_book(path, {"id": "source", "title": "Test source", "author": "Test author", "language": "en"})
    return library


def use_model(monkeypatch: pytest.MonkeyPatch, model: FakeModel) -> None:
    from book_rag import semantic

    monkeypatch.setattr(semantic, "_model", lambda name, cache, revision=None: model)


def test_interrupted_batches_resume_without_reencoding_completed_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from book_rag import semantic

    library = embedding_library(tmp_path)
    first_model = FakeModel(fail_after=1)
    use_model(monkeypatch, first_model)
    with pytest.raises(KeyboardInterrupt):
        semantic.build_index(library, batch_size=3)
    folder = next((library.data_dir / "vectors").glob("staging-*"))
    checkpoint = json.loads((folder / "checkpoint.json").read_text())
    assert checkpoint["completed_vectors"] == len(first_model.encoded) == 3
    assert not (folder.parent / "active.json").exists()
    resumed_model = FakeModel()
    use_model(monkeypatch, resumed_model)
    report = semantic.build_index(library, batch_size=2)
    assert report["resumed_vectors"] == 3
    assert len(resumed_model.encoded) == report["vectors"] - 3
    assert len(set(first_model.encoded + resumed_model.encoded)) == report["vectors"]
    vectors = np.load(folder.parent / report["generation"] / "embeddings.npy")
    expected = FakeModel().encode(first_model.encoded + resumed_model.encoded)
    np.testing.assert_array_equal(vectors, expected)
    assert report["strategy"] == "token-windows-v2"
    assert report["model_revision"] == semantic.DEFAULT_REVISION
    assert report["corpus_fingerprint"] == corpus_fingerprint(library)


@pytest.mark.parametrize(
    "damage", ["truncated_vectors", "corrupt_vectors", "corrupt_ids", "corrupt_json", "wrong_dimension", "wrong_cursor"]
)
def test_corrupt_checkpoints_rebuild_staging_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    import json

    from book_rag import semantic

    library = embedding_library(tmp_path)
    use_model(monkeypatch, FakeModel(fail_after=1))
    with pytest.raises(KeyboardInterrupt):
        semantic.build_index(library, batch_size=3)
    folder = next((library.data_dir / "vectors").glob("staging-*"))
    checkpoint_path = folder / "checkpoint.json"
    if damage == "truncated_vectors":
        with (folder / "embeddings.npy").open("r+b") as stream:
            stream.truncate(100)
    elif damage == "corrupt_vectors":
        vectors = np.lib.format.open_memmap(folder / "embeddings.npy", mode="r+")
        vectors[0, 0] += 0.125
        vectors.flush()
        del vectors
    elif damage == "corrupt_ids":
        ids = np.lib.format.open_memmap(folder / "ids.npy", mode="r+")
        ids[0] = 999999
        ids.flush()
        del ids
    elif damage == "corrupt_json":
        checkpoint_path.write_text("{interrupted JSON")
    else:
        checkpoint = json.loads(checkpoint_path.read_text())
        if damage == "wrong_dimension":
            checkpoint["descriptor"]["dimensions"] = 12
        else:
            checkpoint["cursor"]["window_index"] += 1
        checkpoint_path.write_text(json.dumps(checkpoint))
    model = FakeModel()
    use_model(monkeypatch, model)
    report = semantic.build_index(library, batch_size=3)
    assert report["resumed_vectors"] == 0
    assert len(model.encoded) == report["vectors"]
    assert (folder.parent / report["generation"] / "metadata.json").is_file()


def test_changed_corpus_does_not_resume_old_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from book_rag import semantic

    library = embedding_library(tmp_path)
    use_model(monkeypatch, FakeModel(fail_after=1))
    with pytest.raises(KeyboardInterrupt):
        semantic.build_index(library, batch_size=3)
    old_folder = next((library.data_dir / "vectors").glob("staging-*"))
    added = tmp_path / "added.txt"
    added.write_text("New material changes the source corpus identity.")
    library.import_book(added, {"id": "added", "title": "A new source", "author": "Another author"})
    model = FakeModel()
    use_model(monkeypatch, model)
    report = semantic.build_index(library, batch_size=3)
    assert report["resumed_vectors"] == 0
    assert len(model.encoded) == report["vectors"]
    assert old_folder.exists(), "Retain unrelated interrupted work instead of silently deleting it"


def test_failed_new_model_build_preserves_searchable_active_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from book_rag import semantic

    library = embedding_library(tmp_path)
    good_model, bad_model = FakeModel(), FakeModel(fail_after=1)
    use_model(monkeypatch, good_model)
    original = semantic.build_index(library, batch_size=3)
    active = library.data_dir / "vectors" / "active.json"
    before = active.read_bytes()
    monkeypatch.setenv("BOOKRAG_EMBEDDING_REVISION", "b" * 40)
    monkeypatch.setattr(
        semantic,
        "_model",
        lambda name, cache, revision=None: good_model if name == semantic.DEFAULT_MODEL else bad_model,
    )
    with pytest.raises(KeyboardInterrupt):
        semantic.build_index(library, model_name="test/alternative", batch_size=3)
    assert active.read_bytes() == before
    result = library.search("word0000", mode="semantic")
    assert result["mode"] == "semantic" and result["results"]
    assert all(item["verified"] for item in result["results"])
    assert (active.parent / original["generation"] / "embeddings.npy").exists()


def test_ready_generation_recovers_interrupted_activation_without_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from book_rag import semantic

    library = embedding_library(tmp_path)
    model = FakeModel()
    use_model(monkeypatch, model)
    original_atomic = semantic._atomic_json

    def fail_activation(path: Path, data: dict) -> None:
        if path.name == "active.json":
            raise OSError("Simulated interruption before active pointer replacement")
        original_atomic(path, data)

    monkeypatch.setattr(semantic, "_atomic_json", fail_activation)
    with pytest.raises(OSError):
        semantic.build_index(library, batch_size=3)
    encoded = len(model.encoded)
    monkeypatch.setattr(semantic, "_atomic_json", original_atomic)
    report = semantic.build_index(library, batch_size=3)
    assert report["reused_active"]
    assert report["resumed_vectors"] == encoded == report["vectors"]
    assert len(model.encoded) == encoded


def test_shared_model_cache_used_for_build_and_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from book_rag import semantic

    library = embedding_library(tmp_path)
    model, calls = FakeModel(), []
    shared = tmp_path / "shared-model-cache"
    monkeypatch.setenv("BOOKRAG_MODEL_CACHE", str(shared))

    def load(name: str, cache: str, revision: str | None = None) -> FakeModel:
        calls.append((name, cache, revision))
        return model

    monkeypatch.setattr(semantic, "_model", load)
    semantic.build_index(library, batch_size=3)
    library.search("word0000", mode="semantic")
    assert len(calls) == 2
    assert all(cache == str(shared.resolve()) and revision == semantic.DEFAULT_REVISION for _, cache, revision in calls)


class FakeE5Model(FakeModel):
    max_seq_length = 512


def test_profile_window_budget_accounts_for_document_prefix_and_header() -> None:
    from book_rag.embedding_profiles import E5_SMALL_PROFILE, MINILM_PROFILE
    from book_rag.semantic import _profile_windows, _windows

    row = {
        "title": " ".join(f"header{index}" for index in range(80)),
        "author": "An author",
        "section_title": "A chapter",
        "text": " ".join(f"source{index}" for index in range(1200)),
    }
    model = FakeE5Model()
    windows = _profile_windows(model, row, E5_SMALL_PROFILE)
    assert len(windows) > 1
    assert all(text.startswith("passage: ") for text in windows)
    assert all(len(model.tokenizer.encode(text)) <= 512 - 6 for text in windows)
    source_words = {word for text in windows for word in text.split("\n", 1)[1].split()}
    assert source_words == set(row["text"].split())
    assert windows[-1].endswith("source1199")
    legacy_model = FakeModel()
    assert _profile_windows(legacy_model, row, MINILM_PROFILE) == _windows(legacy_model, row)


def test_e5_build_prefixes_and_query_follow_frozen_index_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from book_rag import semantic
    from book_rag.embedding_profiles import E5_SMALL_MODEL, E5_SMALL_REVISION

    library = embedding_library(tmp_path)
    model = FakeE5Model()
    calls = []
    shared = tmp_path / "shared"
    monkeypatch.setenv("BOOKRAG_MODEL_CACHE", str(shared))

    def loader(name: str, cache: str, revision: str | None = None) -> FakeE5Model:
        calls.append((name, cache, revision))
        return model

    monkeypatch.setattr(semantic, "_model", loader)
    report = semantic.build_index(library, model_name=E5_SMALL_MODEL, batch_size=3)
    assert all(text.startswith("passage: ") for text in model.encoded)
    assert report["embedding_profile"]["query_prefix"] == "query: "
    assert report["embedding_profile"]["document_prefix"] == "passage: "
    assert report["model_max_tokens"] == report["embedding_profile"]["max_tokens"] == 512
    monkeypatch.setenv("BOOKRAG_EMBEDDING_PROFILE", "minilm-v2")
    monkeypatch.setenv("BOOKRAG_EMBEDDING_MODEL", semantic.DEFAULT_MODEL)
    result = library.search("Вопрос по источнику", mode="semantic")
    assert model.encoded[-1] == "query: Вопрос по источнику"
    assert result["query"] == "Вопрос по источнику", "The public query and citation text must not gain model prefixes"
    assert all(
        name == E5_SMALL_MODEL and cache == str(shared.resolve()) and revision == E5_SMALL_REVISION
        for name, cache, revision in calls
    )


@pytest.mark.parametrize("field,value", [("document_prefix", "passage variant: "), ("query_prefix", "query variant: ")])
def test_profile_change_invalidates_interrupted_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    import json
    from dataclasses import replace

    from book_rag import semantic
    from book_rag.embedding_profiles import E5_SMALL_MODEL, E5_SMALL_PROFILE, PROFILES

    library = embedding_library(tmp_path)
    failing = FakeE5Model(fail_after=1)
    use_model(monkeypatch, failing)
    with pytest.raises(KeyboardInterrupt):
        semantic.build_index(library, model_name=E5_SMALL_MODEL, batch_size=3)
    folder = next((library.data_dir / "vectors").glob("staging-*"))
    checkpoint = json.loads((folder / "checkpoint.json").read_text())
    assert checkpoint["descriptor"]["embedding_profile"][field] == getattr(E5_SMALL_PROFILE, field)
    variant = replace(E5_SMALL_PROFILE, name="test-profile-variant", **{field: value})
    monkeypatch.setitem(PROFILES, variant.name, variant)
    monkeypatch.setenv("BOOKRAG_EMBEDDING_PROFILE", variant.name)
    use_model(monkeypatch, FakeE5Model())
    report = semantic.build_index(library, model_name=E5_SMALL_MODEL, batch_size=3)
    assert report["resumed_vectors"] == 0
    assert report["build_identity"] != checkpoint["descriptor"]["build_identity"]
    assert report["embedding_profile"][field] == value
    assert folder.exists(), "Keep an interrupted index for its original profile"


def test_legacy_active_index_uses_no_prefix_despite_current_e5_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from book_rag import semantic
    from book_rag.embedding_profiles import E5_SMALL_MODEL

    library = embedding_library(tmp_path)
    model = FakeModel()
    use_model(monkeypatch, model)
    semantic.build_index(library, batch_size=3)
    path = library.data_dir / "vectors" / "active.json"
    legacy = json.loads(path.read_text())
    legacy.pop("embedding_profile")
    path.write_text(json.dumps(legacy))
    monkeypatch.setenv("BOOKRAG_EMBEDDING_MODEL", E5_SMALL_MODEL)
    monkeypatch.setenv("BOOKRAG_EMBEDDING_PROFILE", "multilingual-e5-small")
    library.search("An unchanged legacy query", mode="semantic")
    assert model.encoded[-1] == "An unchanged legacy query"


def test_e5_resume_keeps_prefixed_committed_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from book_rag import semantic
    from book_rag.embedding_profiles import E5_SMALL_MODEL

    library = embedding_library(tmp_path)
    first = FakeE5Model(fail_after=1)
    use_model(monkeypatch, first)
    with pytest.raises(KeyboardInterrupt):
        semantic.build_index(library, model_name=E5_SMALL_MODEL, batch_size=3)
    resumed = FakeE5Model()
    use_model(monkeypatch, resumed)
    report = semantic.build_index(library, model_name=E5_SMALL_MODEL, batch_size=2)
    assert report["resumed_vectors"] == 3
    assert len(resumed.encoded) == report["vectors"] - 3
    assert all(text.startswith("passage: ") for text in first.encoded + resumed.encoded)
    actual = np.load(library.data_dir / "vectors" / report["generation"] / "embeddings.npy")
    np.testing.assert_array_equal(actual, FakeE5Model().encode(first.encoded + resumed.encoded))


class QueryVectorModel:
    def encode(self, texts: list[str], **kwargs: object) -> np.ndarray:
        return np.tile(np.array([[1.0, 0.0]], dtype="float32"), (len(texts), 1))


def retrieval_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Library, list[int]]:
    """Real source evidence with deterministic overlapping-window scores, no model download."""
    import json

    from book_rag import semantic

    library = Library(tmp_path / "retrieval")
    source = tmp_path / "many.txt"
    source.write_text(
        "\n\n".join(f"Needle exact evidence paragraph {index:03}. " + "word " * 230 for index in range(140))
    )
    library.import_book(source, {"id": "many", "title": "Many passages", "author": "Test", "language": "en"})
    other = tmp_path / "other.txt"
    other.write_text("Needle русский отдельный источник.")
    library.import_book(other, {"id": "other", "title": "Другой источник", "author": "Автор", "language": "ru"})
    with library.connect() as conn:
        passage_ids = [row[0] for row in conn.execute("SELECT id FROM passages ORDER BY id")]
    assert len(passage_ids) > 130
    ids = np.array([passage_ids[0]] * 60 + passage_ids[1:], dtype="int64")
    values = np.array([1.0] * 60 + [0.99 - index * 0.001 for index in range(len(passage_ids) - 1)], dtype="float32")
    vectors = np.column_stack((values, np.sqrt(1.0 - values**2))).astype("float32")
    directory = library.data_dir / "vectors" / "fixture"
    directory.mkdir(parents=True)
    np.save(directory / "embeddings.npy", vectors)
    np.save(directory / "ids.npy", ids)
    (directory.parent / "active.json").write_text(
        json.dumps(
            {
                "model": semantic.DEFAULT_MODEL,
                "generation": "fixture",
                "corpus_fingerprint": corpus_fingerprint(library),
            }
        )
    )
    monkeypatch.setattr(semantic, "_model", lambda *args, **kwargs: QueryVectorModel())
    return library, passage_ids


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_fixed_candidate_pool_has_limit_invariant_prefix_and_single_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from book_rag import semantic

    library, passage_ids = retrieval_library(tmp_path, monkeypatch)
    lexical = library.passages(passage_ids[79:129])
    batch = library.passages
    batches: list[list[int]] = []

    def read(ids: list[int]) -> list[dict]:
        batches.append(ids)
        return batch(ids)

    monkeypatch.setattr(library, "passages", read)
    monkeypatch.setattr(library, "passage", lambda _: pytest.fail("Retrieval must hydrate one bounded evidence batch"))
    monkeypatch.setattr(library, "search", lambda *args, **kwargs: {"results": lexical, "warnings": []})
    outputs = [semantic.semantic_search(library, "needle", mode, None, None, limit) for limit in (5, 20, 50)]
    for output, limit in zip(outputs, (5, 20, 50), strict=True):
        assert len(output["results"]) == limit
        assert output["results"] == outputs[-1]["results"][:limit]
        assert output["candidate_pool"]["passage_limit"] == 100
        assert output["retrieval_strategy"] == "fixed-passage-pool-v1"
    assert len(batches) == 3 and all(len(ids) == 100 for ids in batches)
    assert batches[0] == batches[1] == batches[2]
    assert all(len(set(ids)) == len(ids) for ids in batches)


def test_window_pool_is_bounded_and_handles_empty_small_duplicate_scores() -> None:
    from book_rag.semantic import SEMANTIC_WINDOW_CANDIDATES, _candidate_passages

    assert _candidate_passages(np.array([]), np.array([], dtype="int64"), 0.30) == ([], {}, 0)
    scores = np.array([0.30, 0.29, np.nan, -np.inf, 0.8, 0.8])
    ordered, best, windows = _candidate_passages(scores, np.array([2, 3, 4, 5, 6, 6]), 0.30)
    assert ordered == [6, 2] and windows == 6
    assert best == {6: 0.8, 2: 0.3}
    repeated = SEMANTIC_WINDOW_CANDIDATES
    scores = np.array([1.0] * repeated + [0.9] * 3000)
    ids = np.array([1] * repeated + list(range(2, 3002)))
    ordered, _, count = _candidate_passages(scores, ids, 0.30)
    assert ordered == [1] and count == repeated, "A duplicate-heavy tail must not cause unbounded window scans"


def test_batched_semantic_filters_exact_book_language_and_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library, _ = retrieval_library(tmp_path, monkeypatch)
    russian = library.search("needle", mode="semantic", language="ru", limit=50)
    assert len(russian["results"]) == 1 and russian["results"][0]["book_id"] == "other"
    assert all(item["language"] == "ru" for item in russian["results"])
    assert not library.search("needle", mode="semantic", book_id="many", language="ru")["results"]
    assert not library.search('"absent exact phrase"', mode="semantic")["results"]
    exact = library.search('"Needle exact evidence"', mode="semantic", book_id="many")
    assert exact["results"] and all("Needle exact evidence" in item["text"] for item in exact["results"])


def test_missing_or_corrupt_batch_evidence_is_suppressed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    library, passage_ids = retrieval_library(tmp_path, monkeypatch)
    original = library.passages

    def damaged(ids: list[int]) -> list[dict]:
        rows = original(ids)
        return [
            row | {"verified": False} if row["id"] == passage_ids[1] else row
            for row in rows
            if row["id"] != passage_ids[0]
        ]

    monkeypatch.setattr(library, "passages", damaged)
    result = library.search("needle", mode="semantic", limit=50)
    assert all(item["id"] not in passage_ids[:2] and item["verified"] for item in result["results"])
    assert "Corrupt semantic citation suppressed" in result["warnings"][0]


def test_corpus_change_during_query_has_explicit_current_lexical_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from book_rag import semantic

    library, _ = retrieval_library(tmp_path, monkeypatch)

    class ChangingModel(QueryVectorModel):
        def encode(self, texts: list[str], **kwargs: object) -> np.ndarray:
            added = tmp_path / "fresh.txt"
            added.write_text("Needle new evidence arriving during inference.")
            library.import_book(added, {"id": "fresh", "title": "Fresh source", "author": "New author"})
            return super().encode(texts, **kwargs)

    monkeypatch.setattr(semantic, "_model", lambda *args, **kwargs: ChangingModel())
    result = library.search("needle", mode="semantic", limit=5)
    assert result["mode"] == "lexical"
    assert any("Corpus changed during semantic search" in warning for warning in result["warnings"])
    assert all(item["verified"] for item in result["results"])
