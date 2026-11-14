"""Explicit manifests and bounded atomic downloads with verifiable cache identity."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY = 10.0
RETRY_STATUS_CODES = {408, 429, 500, 502, 503, 504}
REQUIRED_FIELDS = {
    "id",
    "title",
    "author",
    "language",
    "work_id",
    "edition",
    "source_url",
    "format",
    "rights",
    "rights_url",
}


class DownloadDeferred(httpx.HTTPStatusError):
    """The provider requested a wait longer than this worker can honor inline."""

    def __init__(self, response: httpx.Response, retry_after: float) -> None:
        self.retry_after = retry_after
        try:
            self.retry_at: datetime | None = datetime.now(UTC) + timedelta(seconds=retry_after)
        except OverflowError:
            self.retry_at = None
        schedule = (
            f"retry at or after {self.retry_at.isoformat()}"
            if self.retry_at is not None
            else "requested delay exceeds timestamp range; reschedule manually"
        )
        message = (
            f"HTTP {response.status_code}: provider requested Retry-After {retry_after:g}s; "
            f"download deferred, {schedule}. No early retry was sent."
        )
        super().__init__(message, request=response.request, response=response)


def load_manifest(path: Path) -> list[dict]:
    """Load an auditable JSON manifest; local paths resolve against its directory."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError("Manifest must be a JSON list")
    seen: set[str] = set()
    result: list[dict] = []
    for raw in entries:
        if not isinstance(raw, dict) or not REQUIRED_FIELDS.issubset(raw):
            raise ValueError("Manifest entry is missing required provenance metadata")
        entry = raw.copy()
        identity = entry["id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", identity):
            raise ValueError("Source IDs must use safe ASCII letters, digits, dots, dashes or underscores")
        if identity in seen:
            raise ValueError(f"Duplicate source ID: {identity}")
        seen.add(identity)
        for key in REQUIRED_FIELDS:
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise ValueError(f"Source metadata {key} must be a non-empty string")
        if entry.get("local_path"):
            local = Path(entry["local_path"])
            if not local.is_absolute():
                local = path.parent / local
            entry["local_path"] = str(local.resolve())
        elif not entry.get("download_url"):
            raise ValueError("Each source needs local_path or download_url")
        result.append(entry)
    return result


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verified_cache(target: Path, metadata: Path, url: str) -> bool:
    """A legacy file without sidecar cannot establish its source URL."""
    if not target.is_file() or not metadata.is_file():
        return False
    try:
        info = json.loads(metadata.read_text(encoding="utf-8"))
        size = target.stat().st_size
        return (
            info.get("schema_version") == 1
            and info.get("download_url") == url
            and 0 < size <= MAX_DOWNLOAD_BYTES
            and info.get("bytes") == size
            and info.get("sha256") == _digest(target)
        )
    except (ValueError, OSError, AttributeError):
        return False


def cached_source(entry: dict, destination: Path) -> Path | None:
    """Return a proven local/remote cache hit without creating files or networking.

    Remote hits require a matching requested URL, sidecar version, size and
    current file SHA-256. A legacy file without a sidecar is a cache miss.
    """
    if entry.get("local_path"):
        local = Path(entry["local_path"])
        return local if local.is_file() else None
    identity = entry["id"]
    extension = entry["format"].lower().lstrip(".")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", identity) or extension not in {"txt", "html", "htm", "epub", "fb2", "pdf"}:
        raise ValueError("Unsafe source identifier or unsupported source format")
    url = entry["download_url"]
    if urlparse(url).scheme != "https":
        raise ValueError("Remote sources must use HTTPS")
    target = destination / f"{identity}.{extension}"
    metadata = target.with_suffix(target.suffix + ".source.json")
    return target if _verified_cache(target, metadata, url) else None


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    delay = 0.5 * (2**attempt)
    value = response.headers.get("retry-after", "") if response is not None else ""
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                moment = parsedate_to_datetime(value)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=UTC)
                delay = (moment - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
    if not math.isfinite(delay):
        delay = 0.5 * (2**attempt)
    return max(0.0, delay)


def _transfer(client: httpx.Client, url: str, temporary: Path) -> tuple[str, str, int]:
    """Stream one attempt; return final URL, SHA-256 and canonical file size."""
    for _ in range(6):
        with client.stream("GET", url) as response:
            if response.is_redirect:
                next_request = response.next_request
                if next_request is None:
                    raise ValueError("Source redirect is missing a Location header")
                url = str(next_request.url)
                if urlparse(url).scheme != "https":
                    raise ValueError("Source redirected to insecure HTTP")
                continue
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length and int(length) > MAX_DOWNLOAD_BYTES:
                raise ValueError("Download exceeds the 100 MiB limit")
            size = 0
            digest = hashlib.sha256()
            with temporary.open("wb") as stream:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise ValueError("Download exceeds the 100 MiB limit")
                    digest.update(chunk)
                    stream.write(chunk)
            if not size:
                raise ValueError("Source returned an empty document")
            if length and not response.headers.get("content-encoding") and size != int(length):
                raise httpx.RemoteProtocolError(
                    "Source response ended before its declared size", request=response.request
                )
            return str(response.url), digest.hexdigest(), size
    raise ValueError("Too many source redirects")


def download_source(entry: dict, destination: Path) -> Path:
    """Obtain one source in a directory with provenance-checked cache reuse.

    A completed remote file is reused only if its sidecar matches the requested
    URL, file size and SHA-256. Legacy or corrupt caches are downloaded again.
    Transient transport/429/5xx failures retry at most three attempts, honoring
    Retry-After up to ten seconds. Longer provider waits raise DownloadDeferred
    with an actionable retry timestamp, never an early retry. Interrupted
    attempts restart safely; byte-range resume is intentionally not claimed.
    """
    if entry.get("local_path"):
        local = Path(entry["local_path"])
        if not local.is_file():
            raise FileNotFoundError(local)
        return local
    identity, extension = entry["id"], entry["format"].lower().lstrip(".")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", identity) or extension not in {"txt", "html", "htm", "epub", "fb2", "pdf"}:
        raise ValueError("Unsafe source identifier or unsupported source format")
    url = entry["download_url"]
    if urlparse(url).scheme != "https":
        raise ValueError("Remote sources must use HTTPS")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"{identity}.{extension}"
    metadata = target.with_suffix(target.suffix + ".source.json")
    cached = cached_source(entry, destination)
    if cached is not None:
        return cached
    temporary = target.with_suffix(target.suffix + ".part")
    metadata_temp = metadata.with_suffix(metadata.suffix + ".part")
    try:
        with httpx.Client(
            timeout=60.0,
            follow_redirects=False,
            headers={"User-Agent": "BookRAG/0.1 (+open-source research library)", "Accept-Encoding": "identity"},
        ) as client:
            for attempt in range(MAX_ATTEMPTS):
                try:
                    final_url, digest, size = _transfer(client, url, temporary)
                except httpx.HTTPStatusError as error:
                    if error.response.status_code not in RETRY_STATUS_CODES:
                        raise
                    delay = _retry_delay(error.response, attempt)
                    if delay > MAX_RETRY_DELAY:
                        raise DownloadDeferred(error.response, delay) from error
                    if attempt + 1 >= MAX_ATTEMPTS:
                        raise
                    time.sleep(delay)
                    continue
                except httpx.TransportError:
                    if attempt + 1 >= MAX_ATTEMPTS:
                        raise
                    time.sleep(_retry_delay(None, attempt))
                    continue
                metadata_temp.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "download_url": url,
                            "final_url": final_url,
                            "sha256": digest,
                            "bytes": size,
                            "retrieved_at": datetime.now(UTC).isoformat(),
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                # A crash between these replaces leaves a cache miss on rerun,
                # never a falsely verified file with a mismatched source URL.
                temporary.replace(target)
                metadata_temp.replace(metadata)
                return target
    finally:
        temporary.unlink(missing_ok=True)
        metadata_temp.unlink(missing_ok=True)
    raise RuntimeError("Download attempts exhausted")
