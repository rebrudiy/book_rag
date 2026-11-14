"""Download identity, interrupted transfers and bounded provider backoff."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from book_rag.sources import DownloadDeferred, download_source


def entry(url: str = "https://example.com/book.txt") -> dict:
    return {"id": "sample", "format": "txt", "download_url": url}


def mock_client(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> list[float]:
    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    sleeps: list[float] = []
    monkeypatch.setattr("book_rag.sources.time.sleep", sleeps.append)
    return sleeps


def test_source_sidecar_preserves_verifiable_origin_and_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/book.txt":
            return httpx.Response(302, headers={"location": "https://example.com/final.txt"})
        return httpx.Response(200, content=b"A complete text.")

    mock_client(monkeypatch, handler)
    path = download_source(entry(), tmp_path)
    metadata = json.loads(path.with_suffix(".txt.source.json").read_text())
    assert metadata["download_url"] == entry()["download_url"]
    assert metadata["final_url"] == "https://example.com/final.txt"
    assert metadata["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert metadata["bytes"] == len(path.read_bytes())
    assert datetime.fromisoformat(metadata["retrieved_at"]).tzinfo is not None
    assert download_source(entry(), tmp_path) == path
    assert len(calls) == 2  # second call reuses independently verified cache


def test_changed_url_cannot_relabel_cached_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=request.url.path.encode())

    mock_client(monkeypatch, handler)
    path = download_source(entry(), tmp_path)
    download_source(entry("https://example.com/new-edition.txt"), tmp_path)
    assert path.read_text() == "/new-edition.txt"
    metadata = json.loads(path.with_suffix(".txt.source.json").read_text())
    assert metadata["download_url"] == "https://example.com/new-edition.txt"
    assert len(calls) == 2


@pytest.mark.parametrize("damage", ["same-size", "truncate", "metadata", "legacy"])
def test_unverified_or_damaged_cache_is_refetched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"Original.")

    mock_client(monkeypatch, handler)
    path = download_source(entry(), tmp_path)
    if damage == "same-size":
        path.write_bytes(b"Modified.")
    elif damage == "truncate":
        path.write_bytes(b"Ori")
    elif damage == "metadata":
        path.with_suffix(".txt.source.json").write_text("not json")
    else:
        path.with_suffix(".txt.source.json").unlink()
    download_source(entry(), tmp_path)
    assert path.read_bytes() == b"Original."
    assert len(calls) == 2


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_status_retries_honor_bounded_retry_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) < 3:
            return httpx.Response(status, headers={"retry-after": "2"})
        return httpx.Response(200, content=b"Recovered.")

    sleeps = mock_client(monkeypatch, handler)
    assert download_source(entry(), tmp_path).read_text() == "Recovered."
    assert sleeps == [2.0, 2.0]
    assert len(calls) == 3


def test_retry_after_http_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            future = format_datetime(datetime.now(UTC) + timedelta(seconds=8), usegmt=True)
            return httpx.Response(429, headers={"retry-after": future})
        return httpx.Response(200, content=b"Recovered.")

    sleeps = mock_client(monkeypatch, handler)
    download_source(entry(), tmp_path)
    assert len(sleeps) == 1 and 6 <= sleeps[0] <= 8


def test_permanent_http_error_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(404)

    sleeps = mock_client(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        download_source(entry(), tmp_path)
    assert len(calls) == 1 and not sleeps
    assert not (tmp_path / "sample.txt").exists()


class InterruptedStream(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b"Partial bytes."
        raise httpx.ReadError("Connection interrupted")


def test_interrupted_stream_restarts_without_publishing_partial_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(200, stream=InterruptedStream())
        assert not (tmp_path / "sample.txt").exists()
        return httpx.Response(200, content=b"Complete after restart.")

    sleeps = mock_client(monkeypatch, handler)
    path = download_source(entry(), tmp_path)
    assert path.read_bytes() == b"Complete after restart."
    assert len(sleeps) == 1 and len(calls) == 2
    assert not path.with_suffix(".txt.part").exists()


def test_exhausted_retries_preserve_old_cache_and_clean_part_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("Old cache remains available for inspection.")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=InterruptedStream())

    sleeps = mock_client(monkeypatch, handler)
    with pytest.raises(httpx.ReadError):
        download_source(entry(), tmp_path)
    assert path.read_text() == "Old cache remains available for inspection."
    assert len(sleeps) == 2
    assert not path.with_suffix(".txt.part").exists()
    assert not path.with_suffix(".txt.source.json").exists()


def test_false_content_length_is_retried_and_never_committed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "999"}, content=b"Short.")

    sleeps = mock_client(monkeypatch, handler)
    with pytest.raises(httpx.RemoteProtocolError):
        download_source(entry(), tmp_path)
    assert len(sleeps) == 2
    assert not (tmp_path / "sample.txt").exists()


def test_cached_source_proves_identity_without_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from book_rag.sources import cached_source

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"Verified bytes.")

    mock_client(monkeypatch, handler)
    assert cached_source(entry(), tmp_path) is None
    path = download_source(entry(), tmp_path)
    assert cached_source(entry(), tmp_path) == path
    assert cached_source(entry("https://example.com/other.txt"), tmp_path) is None
    path.write_text("Damaged.")
    assert cached_source(entry(), tmp_path) is None
    assert len(calls) == 1


@pytest.mark.parametrize("retry_after", ["99999", "11"])
def test_long_retry_after_defers_without_early_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_after: str,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(429, headers={"retry-after": retry_after})

    sleeps = mock_client(monkeypatch, handler)
    earliest = datetime.now(UTC) + timedelta(seconds=float(retry_after))
    with pytest.raises(DownloadDeferred, match="retry at or after") as caught:
        download_source(entry(), tmp_path)
    assert caught.value.retry_after == float(retry_after)
    assert caught.value.retry_at is not None and caught.value.retry_at >= earliest
    assert len(calls) == 1 and sleeps == []
    assert not (tmp_path / "sample.txt").exists()


def test_long_http_date_retry_after_is_not_clipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(503, headers={"retry-after": format_datetime(future, usegmt=True)})

    sleeps = mock_client(monkeypatch, handler)
    with pytest.raises(DownloadDeferred) as caught:
        download_source(entry(), tmp_path)
    assert caught.value.retry_at is not None
    assert abs((caught.value.retry_at - future).total_seconds()) < 2
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize("header", ["not-a-date", "nan", "inf"])
def test_invalid_retry_after_uses_bounded_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    header: str,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return (
            httpx.Response(503, headers={"retry-after": header})
            if len(calls) == 1
            else httpx.Response(200, content=b"OK")
        )

    sleeps = mock_client(monkeypatch, handler)
    assert download_source(entry(), tmp_path).read_bytes() == b"OK"
    assert sleeps == [0.5] and len(calls) == 2
