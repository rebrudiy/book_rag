"""Provider deferrals persist across job restarts and apply to the whole host."""

import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from book_rag.cli import ingest_manifest
from book_rag.library import Library
from book_rag.sources import DownloadDeferred


def test_retry_after_defers_host_across_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOKRAG_DATA_DIR", str(tmp_path / "data"))
    manifest = tmp_path / "queue.json"
    entries = [
        {
            "id": str(n),
            "title": "Title",
            "author": "A",
            "language": "en",
            "work_id": str(n),
            "edition": "E",
            "source_url": "https://provider.test/book",
            "format": "epub",
            "rights": "CC0",
            "rights_url": "https://provider.test/rights",
            "download_url": f"https://provider.test/{n}.epub",
        }
        for n in range(2)
    ]
    manifest.write_text(json.dumps(entries))
    request = httpx.Request("GET", entries[0]["download_url"])
    response = httpx.Response(429, request=request, headers={"Retry-After": "3600"})
    downloader = Mock(side_effect=DownloadDeferred(response, 3600))
    monkeypatch.setattr("book_rag.sources.download_source", downloader)
    report = ingest_manifest(manifest, download_delay=0, min_free_gib=0)
    assert report["entries"][0]["status"] == "deferred" and downloader.call_count == 1
    assert Library(tmp_path / "data").stats()["status_counts"] == {"deferred": 1, "discovered": 1}
    # A new run must consult persisted host policy before sending any request.
    again = ingest_manifest(manifest, download_delay=0, min_free_gib=0)
    assert again["stopped_reason"] and downloader.call_count == 1
    assert again["entries"][0]["status"] == "deferred"
