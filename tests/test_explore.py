"""Thematic exploration must reflect indexed evidence and expose sampling limits."""

from pathlib import Path
from typing import Any

from pytest import MonkeyPatch

from book_rag.explore import build_map, tokens
from book_rag.library import Library


def add(library: Library, tmp_path: Path, book_id: str, text: str, language: str = "en") -> None:
    path = tmp_path / f"{book_id}.txt"
    path.write_text(text, encoding="utf-8")
    library.import_book(
        path,
        {
            "id": book_id,
            "title": f"Book {book_id}",
            "author": "Test author",
            "language": language,
            "edition": "Test source",
            "source_url": "https://example.org/",
            "rights": "CC0",
        },
    )


def test_same_language_related_books_have_evidence(tmp_path: Path) -> None:
    library = Library(tmp_path / "data")
    add(library, tmp_path, "ecology", "Ecology habitat forest survival nature. Ecology forest forest survival.")
    add(library, tmp_path, "forest", "Forest ecology nature habitat diversity. Forest habitat ecology nature.")
    add(library, tmp_path, "music", "Music violin orchestra harmony melody. Violin orchestra melody piano.")
    result = build_map(library)
    assert result["method"] == "lexical-tfidf"
    assert result["corpus_books"] == 3
    assert not result["truncated"]
    assert {result["edges"][0]["source"], result["edges"][0]["target"]} == {"ecology", "forest"}
    assert result["edges"][0]["shared_terms"]
    assert any(theme["term"] == "forest" and theme["count"] == 2 for theme in result["themes"])
    for edge in result["edges"]:
        assert 0 < edge["score"] <= 1.000001
        assert "music" not in (edge["source"], edge["target"])


def test_language_boundaries_and_stopwords(tmp_path: Path) -> None:
    library = Library(tmp_path / "data")
    add(library, tmp_path, "english", "Nature nature forest the and this that have.", "en")
    add(library, tmp_path, "french", "Nature nature forêt la les pour une dans.", "fr")
    result = build_map(library)
    assert not result["edges"], "Identical spelling across languages must not imply semantic relation"
    assert len({theme["language"] for theme in result["themes"]}) == 2
    assert tokens("THE and HAVE forest nature", "en") == ["forest", "nature"]
    assert tokens("как это есть свобода мысль", "ru") == ["свобода", "мысль"]
    assert tokens("die und ist Natur Wald", "de") == ["natur", "wald"]
    assert "straße" in tokens("Die Straße", "de"), "Keep query tokens compatible with SQLite FTS"


def test_only_indexed_books_included_and_limits_disclosed(tmp_path: Path) -> None:
    library = Library(tmp_path / "data")
    for index in range(3):
        add(library, tmp_path, str(index), f"Ecology forest habitat study{index} extra {index}.")
    library.register({"id": "not-downloaded", "title": "Missing book"})
    result = build_map(library, max_books=2, max_themes=3)
    assert result["corpus_books"] == 3
    assert len(result["books"]) == 2
    assert result["truncated"]
    assert len(result["themes"]) <= 3
    assert any("первые 2" in limitation for limitation in result["limitations"])
    assert all(book["id"] != "not-downloaded" for book in result["books"])


def test_empty_map_and_deterministic_results(tmp_path: Path) -> None:
    library = Library(tmp_path / "data")
    assert build_map(library)["books"] == []
    assert build_map(library)["edges"] == []
    add(library, tmp_path, "one", "Curiosity discovery learning observation science curiosity.")
    first = build_map(library)
    second = build_map(library)
    for result in (first, second):
        for key in ("elapsed_ms", "build_ms", "cache_hit"):
            result.pop(key)
    assert first == second
    assert first["books"][0]["terms"][0]["term"] == "curiosity"


def test_cached_map_is_fast_path_and_cannot_be_poisoned(tmp_path: Path) -> None:
    library = Library(tmp_path / "data")
    add(library, tmp_path, "one", "Curiosity observation discovery science.")
    first = build_map(library)
    second = build_map(library)
    assert not first["cache_hit"]
    assert second["cache_hit"]
    assert first["corpus_identity"] == second["corpus_identity"]
    second["books"][0]["title"] = "Forged book"
    second["books"][0]["terms"].clear()
    third = build_map(library)
    assert third["books"][0]["title"] == "Book one"
    assert third["books"][0]["terms"]


def test_metadata_changes_and_reindex_invalidate_cache(tmp_path: Path) -> None:
    library = Library(tmp_path / "data")
    add(library, tmp_path, "one", "Curiosity observation discovery science.")
    original = build_map(library)
    with library.connect() as conn:
        conn.execute("UPDATE books SET title='Corrected title' WHERE id='one'")
    corrected = build_map(library)
    assert not corrected["cache_hit"]
    assert original["corpus_identity"] != corrected["corpus_identity"]
    assert corrected["books"][0]["title"] == "Corrected title"
    add(library, tmp_path, "one", "Ecology habitat forest survival nature.")
    reindexed = build_map(library)
    assert not reindexed["cache_hit"]
    assert reindexed["corpus_identity"] != corrected["corpus_identity"]
    assert any(term["term"] == "ecology" for term in reindexed["books"][0]["terms"])


def test_ingestion_during_build_is_disclosed_and_not_cached(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from book_rag import explore

    library = Library(tmp_path / "data")
    add(library, tmp_path, "one", "Curiosity observation discovery science.")
    original_build = explore._build_map
    builds = 0

    def build_then_ingest(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal builds
        builds += 1
        result = original_build(*args, **kwargs)
        if builds == 1:
            add(library, tmp_path, "two", "Ecology habitat forest survival nature.")
        return result

    monkeypatch.setattr(explore, "_build_map", build_then_ingest)
    changed = build_map(library)
    assert changed["snapshot_changed"]
    assert changed["corpus_books"] == 1
    assert any("корпус изменился" in limitation for limitation in changed["limitations"])
    stable = build_map(library)
    assert not stable["snapshot_changed"]
    assert not stable["cache_hit"]
    assert stable["corpus_books"] == 2
    assert build_map(library)["cache_hit"]
    assert builds == 2


def test_cache_is_bounded_to_four_recent_maps(tmp_path: Path) -> None:
    libraries = [Library(tmp_path / str(index)) for index in range(5)]
    for library in libraries:
        build_map(library)
    assert build_map(libraries[-1])["cache_hit"]
    assert not build_map(libraries[0])["cache_hit"], "Old snapshots must be evicted"


def test_concurrent_requests_build_once(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from book_rag import explore

    library = Library(tmp_path / "data")
    add(library, tmp_path, "one", "Curiosity observation discovery science.")
    original_build = explore._build_map
    builds = 0

    def counted_build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal builds
        builds += 1
        return original_build(*args, **kwargs)

    monkeypatch.setattr(explore, "_build_map", counted_build)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: build_map(library), range(4)))
    assert builds == 1
    assert sum(result["cache_hit"] for result in results) == 3
    assert len({result["corpus_identity"] for result in results}) == 1
