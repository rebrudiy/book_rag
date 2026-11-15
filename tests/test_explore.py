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
    assert any("выборк" in limitation.lower() for limitation in result["limitations"])
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


def skewed_library(tmp_path: Path) -> Library:
    library = Library(tmp_path / "skewed")
    for language, count, prefix in (("en", 8, "Alpha"), ("fr", 3, "Zed"), ("ru", 2, "Язык")):
        for ordinal in range(count):
            identity = f"{language}-{ordinal}"
            add(library, tmp_path, identity, f"Unique original CC0 {identity}. Ecology habitat forest.", language)
            with library.connect() as conn:
                conn.execute("UPDATE books SET title=? WHERE id=?", (f"{prefix} {ordinal}", identity))
    return library


def test_language_rounds_include_cyrillic_beyond_title_prefix(tmp_path: Path) -> None:
    library = skewed_library(tmp_path)
    with library.connect() as conn:
        prefix = list(conn.execute("SELECT language FROM books ORDER BY title,id LIMIT 6"))
    assert all(row["language"] == "en" for row in prefix)
    result = build_map(library, max_books=6)
    assert [book["id"] for book in result["books"]] == ["en-0", "fr-0", "ru-0", "en-1", "fr-1", "ru-1"]
    assert result["sampling"]["available_by_language"] == {"en": 8, "fr": 3, "ru": 2}
    assert result["sampling"]["selected_by_language"] == {"en": 2, "fr": 2, "ru": 2}
    assert result["sampling"]["strategy"] == "language-round-robin-v1"
    assert result["sampling"]["proportional"] is False
    assert all(book["id"].split("-")[0] == book["language"] for book in result["books"])
    assert any("ru: 2/2" in limitation for limitation in result["limitations"])


def test_exhausted_language_does_not_waste_later_slots_and_prefix_is_stable(tmp_path: Path) -> None:
    from book_rag.explore import _select_books

    library = skewed_library(tmp_path)
    with library.connect() as conn:
        small, _ = _select_books(conn, 3)
        large, _ = _select_books(conn, 10)
    assert [book["id"] for book in large[:3]] == [book["id"] for book in small]
    assert len(large) == 10
    assert {language: sum(book["language"] == language for book in large) for language in ("en", "fr", "ru")} == {
        "en": 5,
        "fr": 3,
        "ru": 2,
    }


def test_selection_reads_only_metadata_and_retains_hard_500_cap(tmp_path: Path) -> None:
    import sqlite3

    from book_rag.explore import _select_books

    library = skewed_library(tmp_path)
    with library.connect() as conn:
        for ordinal in range(600):
            conn.execute(
                "INSERT INTO books(id,title,author,language,work_id,edition,source_url,rights,status) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"metadata-{ordinal}",
                    f"Alpha {ordinal}",
                    "CC0 fixture",
                    "en",
                    str(ordinal),
                    "Test",
                    "",
                    "CC0",
                    "indexed",
                ),
            )

        def metadata_only(action: int, table: str | None, *args: Any) -> int:
            if action == sqlite3.SQLITE_READ and table in {"passages", "sections"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(metadata_only)
        chosen, available = _select_books(conn, 9999)
        conn.set_authorizer(None)
    assert len(chosen) == 500 and available == {"en": 608, "fr": 3, "ru": 2}
    assert sum(book["language"] == "ru" for book in chosen) == 2


def test_missing_languages_are_explicit_when_cap_cannot_fit_them_all(tmp_path: Path) -> None:
    library = skewed_library(tmp_path)
    result = build_map(library, max_books=1)
    assert result["sampling"]["omitted_languages"] == ["fr", "ru"]
    assert result["sampling"]["selected_by_language"] == {"en": 1}
    assert any("не поместились" in limitation for limitation in result["limitations"])


def test_title_ties_are_resolved_by_id_not_insertion_order(tmp_path: Path) -> None:
    from book_rag.explore import _select_books

    library = Library(tmp_path / "ties")
    for identity, language in (("b", "en"), ("a", "en"), ("я", "ru"), ("ю", "ru")):
        add(library, tmp_path, identity, f"Exact original source {identity}.", language)
        with library.connect() as conn:
            conn.execute("UPDATE books SET title='Same title' WHERE id=?", (identity,))
    with library.connect() as conn:
        chosen, _ = _select_books(conn, 4)
    assert [book["id"] for book in chosen] == ["a", "ю", "b", "я"]


def test_new_sampling_metadata_is_deep_copied_and_strategy_versions_cache(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from book_rag import explore

    library = skewed_library(tmp_path)
    result = build_map(library, max_books=6)
    result["sampling"]["available_by_language"]["ru"] = 999
    cached = build_map(library, max_books=6)
    assert cached["cache_hit"] and cached["sampling"]["available_by_language"]["ru"] == 2
    monkeypatch.setattr(explore, "_MAP_SELECTION_STRATEGY", "test-next-strategy")
    newer = build_map(library, max_books=6)
    assert not newer["cache_hit"] and newer["sampling"]["strategy"] == "test-next-strategy"
