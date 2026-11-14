"""Offline user journeys crossing import, retrieval, reader, notes, export and restart."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from book_rag import parsers
from book_rag.api import create_app
from book_rag.cli import app as cli_app


def metadata(book_id: str = "integration-book") -> dict[str, Any]:
    """Explicit CC0 provenance for a tiny text authored for these tests."""
    return {
        "id": book_id,
        "title": "Navigation and Observation",
        "author": "Тестовый Автор",
        "language": "en",
        "work_id": "integration-work",
        "edition": "Fixture edition A",
        "source_url": "https://example.org/fixture/edition-a",
        "rights": "CC0-1.0",
        "rights_url": "https://creativecommons.org/publicdomain/zero/1.0/",
    }


def write_epub(path: Path) -> None:
    """A permitted local EPUB with ordered chapters and non-BMP code points."""
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "OPS/book.opf",
            '<package xmlns="http://www.idpf.org/2007/opf">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Navigation and Observation</dc:title><dc:creator>Тестовый Автор</dc:creator>"
            "<dc:language>en</dc:language></metadata><manifest>"
            '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>',
        )
        archive.writestr(
            "OPS/one.xhtml",
            '<html lang="en"><head><title>Observation</title></head><body>'
            "<h1>Observation 🔭</h1><p>🧭 A navigator observes the night sky.</p>"
            "<p>Crystal telescopes reveal distant nebulae. Café observers take notes.</p>"
            "<p>A footnote explains the observation.</p></body></html>",
        )
        archive.writestr(
            "OPS/two.xhtml",
            '<html lang="en"><head><title>Journeys</title></head><body>'
            "<h1>Journeys</h1><p>The expedition crosses a silent glacier.</p></body></html>",
        )


def test_ebook_to_reader_collections_export_and_process_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "library"
    ebook = tmp_path / "permitted.epub"
    write_epub(ebook)
    app = create_app(directory)
    result = app.state.library.import_book(ebook, metadata())
    assert result["status"] == "indexed"
    with TestClient(app) as client:
        results = client.get(
            "/api/search", params={"q": '"Crystal telescopes"', "book_id": "integration-book", "language": "en"}
        ).json()["results"]
        assert len(results) == 1
        hit = results[0]
        assert hit["verified"]
        location = parse_qs(urlsplit(hit["reader_url"]).query)
        assert location["book"] == ["integration-book"]
        book = client.get("/api/books/integration-book").json()
        section = next(section for section in book["sections"] if str(section["id"]) == location["section"][0])
        assert 0 <= hit["start"] < hit["end"] <= len(section["text"])
        assert hit["end"] - hit["start"] == len(hit["text"])
        assert section["text"][hit["start"] : hit["end"]] == hit["text"]
        assert len(hit["text"].encode("utf-16-le")) // 2 > len(hit["text"])
        assert "🧭" in hit["text"] and "Café" in hit["text"]
        assert [section["title"] for section in book["sections"]] == ["Observation 🔭", "Journeys"]
        assert client.get(hit["reader_url"]).status_code == 200
        assert client.get(f"/api/passages/{hit['id']}").json()["text"] == hit["text"]
        assert not client.get("/api/search", params={"q": "crystal telescopes", "language": "ru"}).json()["results"]
        evidence = client.post(
            "/api/saved",
            json={
                "kind": "passage",
                "title": "Observation",
                "passage_id": hit["id"],
                "collection": "Астрономия",
                "note": "Check the source <script>alert(1)</script>",
            },
        )
        assert evidence.status_code == 201
        saved = evidence.json()
        assert saved["provenance"]["artifact_sha256"] == book["artifact_sha256"]
        assert saved["provenance"]["source_url"] == metadata()["source_url"]
        assert saved["provenance"]["edition"] == "Fixture edition A"
        assert (
            client.post(
                "/api/saved",
                json={
                    "kind": "query",
                    "title": "Second collection",
                    "query": "silent glacier",
                    "collection": "Путешествия",
                },
            ).status_code
            == 201
        )
        exported = client.get("/api/saved/export", params={"collection": "Астрономия"})
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("text/markdown")
        assert "attachment" in exported.headers["content-disposition"]
        assert "Second collection" not in exported.text
        assert metadata()["source_url"] in exported.text
        assert "artifact_sha256" in exported.text and "Открыть в локальном читателе" in exported.text
        assert "<script>" not in exported.text
    # A new app and new SQLite connections emulate a process restart.
    restarted = create_app(directory)
    with TestClient(restarted) as client:
        items = client.get("/api/saved").json()["items"]
        assert len(items) == 2
        assert {item["collection"] for item in items} == {"Астрономия", "Путешествия"}
        assert client.get("/api/stats").json()["books"] == 1
        assert client.get("/api/search", params={"q": "crystal telescopes"}).json()["results"][0]["verified"]
    monkeypatch.setenv("BOOKRAG_DATA_DIR", str(directory))
    output = tmp_path / "exports" / "notes.md"
    invocation = CliRunner().invoke(cli_app, ["export", "--output", str(output), "--collection", "Астрономия"])
    assert invocation.exit_code == 0, invocation.output
    assert output.read_text(encoding="utf-8") == exported.text


def test_metadata_correction_parser_refresh_and_snapshot_survival(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "library"
    source = tmp_path / "book.epub"
    write_epub(source)
    app = create_app(directory)
    app.state.library.import_book(source, metadata())
    with TestClient(app) as client:
        old = client.get("/api/search", params={"q": '"Crystal telescopes"'}).json()["results"][0]
        saved = client.post(
            "/api/saved",
            json={"kind": "passage", "passage_id": old["id"], "title": "Original edition", "collection": "A"},
        ).json()
        corrected = metadata() | {
            "title": "Corrected Navigation",
            "author": "Другой Автор",
            "edition": "Corrected edition B",
            "source_url": "https://example.org/fixture/edition-b",
            "rights": "CC-BY-4.0",
            "rights_url": "https://creativecommons.org/licenses/by/4.0/",
        }
        # The bytes did not change; the changed attribution must defeat the skip.
        imported = app.state.library.import_book(source, corrected)
        assert imported["skipped"] is False
        assert app.state.library.import_book(source, corrected)["skipped"] is True
        current = client.get("/api/search", params={"q": '"Crystal telescopes"'}).json()["results"][0]
        assert current["id"] != old["id"]
        assert current["edition"] == "Corrected edition B"
        assert current["source_url"] == corrected["source_url"]
        assert "Corrected edition B" in current["citation"]
        snapshot = client.get("/api/saved").json()["items"][0]
        assert snapshot["id"] == saved["id"] and snapshot["text"] == old["text"]
        assert snapshot["passage_id"] is None and "reader_url" not in snapshot
        assert snapshot["provenance"]["source_url"] == metadata()["source_url"]
        assert snapshot["provenance"]["edition"] == "Fixture edition A"
        assert client.get(f"/api/passages/{old['id']}").status_code == 404
        exported = client.get("/api/saved/export", params={"collection": "A"}).text
        assert metadata()["source_url"] in exported and corrected["source_url"] not in exported
        assert "Открыть в локальном читателе" not in exported
        # Parser changes must defeat skip even with unchanged artifact/metadata.
        monkeypatch.setattr(parsers, "PARSER_FINGERPRINT", parsers.PARSER_FINGERPRINT + "-integration-test-version")
        assert app.state.library.import_book(source, corrected)["skipped"] is False
        refreshed = client.get("/api/search", params={"q": '"Crystal telescopes"'}).json()["results"][0]
        assert refreshed["id"] != current["id"] and refreshed["verified"]
    with TestClient(create_app(directory)) as client:
        persisted = client.get("/api/saved").json()["items"][0]
        assert persisted["text"] == old["text"] and persisted["provenance"]["edition"] == "Fixture edition A"


def test_failed_source_survives_restart_then_resumes(tmp_path: Path) -> None:
    directory = tmp_path / "library"
    missing = tmp_path / "missing.html"
    app = create_app(directory)
    with pytest.raises(FileNotFoundError):
        app.state.library.import_book(missing, metadata("recoverable"))
    with TestClient(create_app(directory)) as client:
        assert client.get("/api/stats").json()["status_counts"] == {"failed": 1}
        book = client.get("/api/books/recoverable").json()
        assert book["error"] and book["status"] == "failed"
        assert not client.get("/api/search", params={"q": "crystal telescopes"}).json()["results"]
    missing.write_text("<h1>Recovery</h1><p>Crystal telescopes reveal distant nebulae.</p>", encoding="utf-8")
    restarted = create_app(directory)
    restarted.state.library.import_book(missing, metadata("recoverable"))
    with TestClient(restarted) as client:
        stats = client.get("/api/stats").json()
        assert stats["books"] == 1 and stats["status_counts"] == {"indexed": 1}
        assert client.get("/api/search", params={"q": "crystal telescopes"}).json()["results"]
        assert client.get("/api/books/recoverable").json()["error"] is None


def test_book_discovery_is_distinct_from_evidence_and_no_answer(tmp_path: Path) -> None:
    app = create_app(tmp_path / "library")
    source = tmp_path / "preface.html"
    source.write_text("<h1>Preface</h1><p>A preface thanking helpful colleagues.</p>", encoding="utf-8")
    app.state.library.import_book(source, metadata("navigation") | {"title": "Photonic Navigation"})
    with TestClient(app) as client:
        discovery = client.get("/api/search", params={"q": "photonic navigation"}).json()["results"]
        assert len(discovery) == 1 and discovery[0]["title"] == "Photonic Navigation"
        assert discovery[0]["research_supported"] is False
        research = client.post(
            "/api/research", json={"query": "photonic navigation", "book_ids": ["navigation"]}
        ).json()
        assert research["abstained"] and research["results"] == []
        absent = client.get(
            "/api/search", params={"q": "How do superconducting magnets stabilize fusion plasma?"}
        ).json()
        assert absent["results"] == []
        assert client.post(
            "/api/research", json={"query": "How do superconducting magnets stabilize fusion plasma?"}
        ).json()["abstained"]
        assert not client.get("/api/search", params={"q": "photonic navigation", "book_id": "missing"}).json()[
            "results"
        ]
        assert client.get("/api/books", params={"q": "Photonic", "language": "en"}).json()["books"]
        # Author discovery must be Unicode case-insensitive, like full-text search.
        assert client.get("/api/books", params={"q": "тестовый автор"}).json()["books"]
