"""Saved quotations retain their original chapter through index replacement."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from book_rag.api import create_app
from book_rag.canonical_versions import read_canonical_manifest
from book_rag.library import Library


def seed(tmp_path: Path) -> tuple[Library, Path, dict]:
    library = Library(tmp_path / "library")
    source = tmp_path / "first.html"
    source.write_text(
        "<h1>First chapter</h1><p>A remembered 🦉 appears beside the silver birch.</p>"
        "<h2>Second chapter</h2><p>Historical context follows in a separate chapter.</p>"
    )
    library.import_book(
        source,
        {
            "id": "book",
            "title": "Historical fixture",
            "author": "Original author",
            "edition": "First edition",
            "language": "en",
            "source_url": "https://example.org/original",
            "rights": "CC0",
        },
    )
    return library, source, library.search("birch")["results"][0]


def replace(library: Library, tmp_path: Path) -> None:
    source = tmp_path / "replacement.html"
    source.write_text("<h1>New chapter</h1><p>This edition has entirely different text.</p>")
    library.import_book(source, {"id": "book", "edition": "Second edition", "source_url": "https://example.org/new"})


def test_history_is_lazy_exact_and_survives_reindex_without_reparsing(tmp_path: Path, monkeypatch) -> None:
    library, _, hit = seed(tmp_path)
    assert not (library.data_dir / "canonical").exists()
    first = library.save({"kind": "passage", "passage_id": hit["id"], "note": "Private note"})
    second = library.save({"kind": "passage", "passage_id": hit["id"]})
    assert first["provenance"]["canonical_version_id"] == second["provenance"]["canonical_version_id"]
    before = library.saved_context(first["id"])
    assert before is not None
    assert before["book"]["edition"] == "First edition"
    assert before["passage"]["verified"] and before["passage"]["text"] == hit["text"]
    assert "Private note" not in json.dumps(
        read_canonical_manifest(library.data_dir, first["provenance"]["canonical_version_id"])
    )
    replace(library, tmp_path)

    def parser_forbidden(*args, **kwargs):
        raise AssertionError("Historical reader must never reparse the raw source")

    monkeypatch.setattr("book_rag.parsers.parse_book", parser_forbidden)
    saved = library.saved()[1]
    assert saved["passage_id"] is None and "reader_url" not in saved
    assert saved["historical_reader_url"] == f"/?saved={first['id']}"
    after = library.saved_context(first["id"])
    assert after == before
    following = library.saved_context(first["id"], 1)
    assert following is not None and following["section"]["text"] == "Historical context follows in a separate chapter."
    assert following["passage"] is None
    current = library.get_book("book")
    assert current is not None and current["edition"] == "Second edition"


def test_storage_failure_still_saves_verified_quote_and_note(tmp_path: Path, monkeypatch) -> None:
    library, _, hit = seed(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("No space available for historical chapter")

    monkeypatch.setattr("book_rag.canonical_versions.preserve_canonical", fail)
    saved = library.save({"kind": "passage", "passage_id": hit["id"], "note": "Retain this annotation"})
    assert saved["text"] == hit["text"] and saved["note"] == "Retain this annotation"
    assert "historical_reader_url" not in saved and saved["provenance"]["historical_context_error"]
    assert saved["reader_url"] == hit["reader_url"]


def test_low_disk_preserves_quote_without_allocating_historical_files(tmp_path: Path, monkeypatch) -> None:
    import shutil

    library, _, hit = seed(tmp_path)
    usage = shutil.disk_usage(library.data_dir)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: usage._replace(free=4 * 1024**3))
    saved = library.save({"kind": "passage", "passage_id": hit["id"], "note": "Small annotation survives"})
    assert saved["text"] == hit["text"] and saved["note"] == "Small annotation survives"
    assert not (library.data_dir / "canonical").exists()
    assert "historical_context_error" in saved["provenance"]


def test_api_rejects_corrupt_context_without_returning_a_false_quote(tmp_path: Path) -> None:
    library, _, hit = seed(tmp_path)
    saved = library.save({"kind": "passage", "passage_id": hit["id"]})
    app = create_app(library.data_dir)
    with TestClient(app) as client:
        route = f"/api/saved/{saved['id']}/context"
        assert client.get(route).json()["passage"]["verified"]
        assert client.get(route, params={"ordinal": 1}).json()["passage"] is None
        assert client.get(route, params={"ordinal": -1}).status_code == 422
        assert client.get(route, params={"ordinal": 99}).status_code == 404
        assert client.get("/api/saved/999/context").status_code == 404
        manifest = read_canonical_manifest(library.data_dir, saved["provenance"]["canonical_version_id"])
        digest = manifest["sections"][0]["sha256"]
        (library.data_dir / "canonical" / "sections" / digest[:2] / f"{digest}.json.gz").write_bytes(b"corrupt")
        result = client.get(route)
        assert result.status_code == 409 and "passage" not in result.json()
        assert client.get("/api/saved").json()["items"][0]["text"] == hit["text"]


def test_save_and_concurrent_reindex_freeze_one_coherent_edition(tmp_path: Path, monkeypatch) -> None:
    library, _, hit = seed(tmp_path)
    started, finished = threading.Event(), threading.Event()
    preserve = library._preserve_saved_book

    def capture(conn, passage):
        started.set()
        assert not finished.wait(0.05), "Replacement must wait for the quotation snapshot transaction"
        return preserve(conn, passage)

    monkeypatch.setattr(library, "_preserve_saved_book", capture)

    def concurrent_import():
        assert started.wait(5)
        replace(library, tmp_path)
        finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(concurrent_import)
        saved = library.save({"kind": "passage", "passage_id": hit["id"]})
        pending.result(timeout=10)
    context = library.saved_context(saved["id"])
    assert context is not None
    assert context["book"]["edition"] == "First edition"
    assert context["book"]["source_url"] == "https://example.org/original"
    assert context["passage"]["text"] == hit["text"]
