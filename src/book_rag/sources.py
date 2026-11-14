"""Explicit source manifests and bounded, atomic public-source downloads."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
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


def download_source(entry: dict, destination: Path) -> Path:
    """Download into a directory, atomically replacing completed files on success.

    Completed files are reused on rerun; interrupted .part files restart safely.
    Importers record lifecycle state and hashes independently of this helper.
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
    if target.is_file() and target.stat().st_size:
        return target
    temporary = target.with_suffix(target.suffix + ".part")
    with httpx.Client(
        timeout=60.0, follow_redirects=False, headers={"User-Agent": "BookRAG/0.1 (+open-source research library)"}
    ) as client:
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
                with temporary.open("wb") as stream:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ValueError("Download exceeds the 100 MiB limit")
                        stream.write(chunk)
                if not size:
                    raise ValueError("Source returned an empty document")
                temporary.replace(target)
                return target
    raise ValueError("Too many source redirects")
