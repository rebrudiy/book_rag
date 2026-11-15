"""Selected official Gutenberg rsync files with independently verified cache receipts.

The original HTTPS catalog locator is preserved; it is never presented as an
HTTP request that actually occurred. No transfer starts while preparing a plan.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import stat
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from book_rag.sources import MAX_DOWNLOAD_BYTES, load_manifest

OFFICIAL_ORIGINS = {
    "rsync://gutenberg.pglaf.org/gutenberg-epub/",
    "rsync://rsync.ibiblio.org/gutenberg-epub/",
}
POLICY_URL = "https://www.gutenberg.org/help/mirroring.html"
METADATA_URL = "https://gutenberg.pglaf.org/generated-filelist.csv.bz2"
PATH_PATTERN = re.compile(r"([1-9]\d{0,7})/pg\1\.epub\Z")


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _safe_child(root: Path, relative: str) -> Path:
    if not PATH_PATTERN.fullmatch(relative):
        raise ValueError("Only exact same-ID generated no-image EPUB file paths are accepted")
    absolute = root.absolute()
    for directory in (absolute, *absolute.parents):
        if directory.is_symlink():
            raise ValueError("Bulk cache/staging parents must not be symlinks")
    child = absolute / relative
    for component in (child.parent, child):
        if component.is_symlink():
            raise ValueError("Bulk source paths must not be symlinks")
    return child


def registered_ids(database: Path, *, indexed_only: bool = False) -> set[str]:
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        return {r[0] for r in db.execute("SELECT id FROM books" + (" WHERE status='indexed'" if indexed_only else ""))}


def _verify_listing_capture(listing: dict[str, Any], metadata: Path) -> None:
    """Recompute every selected extent/mtime from retained raw metadata bytes."""
    raw = Path(listing["raw_metadata_path"])
    if not raw.is_absolute() and not raw.is_file():
        raw = metadata.parent / raw
    if (
        not raw.is_file()
        or raw.stat().st_size != listing["raw_metadata_bytes"]
        or _sha(raw) != listing["metadata_sha256"]
        or listing.get("raw_metadata_sha256") != listing["metadata_sha256"]
        or not (listing.get("timezone") == "UTC" or listing.get("mtime_timezone") == "UTC (TZ=UTC child environment)")
    ):
        raise ValueError("Raw selected-file metadata size/SHA/timezone proof does not match")
    rows: dict[str, dict[str, Any]] = {}
    with raw.open() as stream:
        for line in stream:
            fields = line.split()
            if len(fields) != 5 or not fields[0].startswith("-") or not PATH_PATTERN.fullmatch(fields[4]):
                continue
            path = fields[4]
            if path in rows:
                raise ValueError("Duplicate file in raw metadata capture")
            rows[path] = {
                "path": path,
                "bytes": int(fields[1].replace(",", "")),
                "mtime": int(
                    datetime.strptime(fields[2] + " " + fields[3], "%Y/%m/%d %H:%M:%S").replace(tzinfo=UTC).timestamp()
                ),
            }
    if any(rows.get(row["path"]) != row for row in listing["files"]):
        raise ValueError("Selected file size/mtime differs from retained raw rsync listing")


def _validate_item(entry: dict[str, Any], remote: dict[str, Any], origin: str) -> None:
    if origin not in OFFICIAL_ORIGINS:
        raise ValueError("Mirror origin is not in the official documented generated-collection allowlist")
    match = PATH_PATTERN.fullmatch(remote["path"])
    if match is None or entry["id"] != f"pg{match[1]}-full":
        raise ValueError("Mirror file and selected Gutenberg edition IDs differ")
    number = match[1]
    source = urlparse(entry["source_url"])
    download = urlparse(entry["download_url"])
    variant = entry.get("source_variant", {})
    if (
        source.scheme != "https"
        or source.hostname not in {"www.gutenberg.org", "gutenberg.org"}
        or source.username
        or source.password
        or source.port not in {None, 443}
        or source.path != f"/ebooks/{number}"
        or source.query
        or source.fragment
        or download.scheme != "https"
        or download.hostname not in {"www.gutenberg.org", "gutenberg.org"}
        or download.username
        or download.password
        or download.port not in {None, 443}
        or download.path != f"/cache/epub/{number}/pg{number}.epub"
        or download.query
        or download.fragment
        or entry.get("format") != "epub"
        or variant.get("format_family") != "epub_noimages"
        or entry.get("illustrations_included") is not False
        or entry.get("coverage_scope") != "text_only_without_illustrations"
    ):
        raise ValueError("Selected source is not an explicit same-ID no-image EPUB variant")
    if not re.match(r"^public domain in the usa\b", " ".join(entry["rights"].casefold().split())):
        raise ValueError("Per-edition public-domain rights are not explicit")
    if type(remote["bytes"]) is not int or not 0 < remote["bytes"] <= MAX_DOWNLOAD_BYTES:
        raise ValueError("Mirror file extent is missing or exceeds the existing 100 MiB cap")
    if type(remote["mtime"]) is not int or remote["mtime"] <= 0:
        raise ValueError("Expected an explicit Unix-epoch mirror file modification time")


def prepare_bulk_plan(
    queue: Path,
    metadata: Path,
    database: Path,
    output: Path,
    *,
    limit: int = 6500,
    reserve_gib: float = 4.5,
) -> dict[str, Any]:
    """Seal a bounded selected-file plan from existing queue and mirror metadata.

    All registered IDs, not only indexed IDs, are excluded at planning time.
    Current indexed/registered IDs must be checked again under the acquisition
    lock before transfer because the ordinary pipeline may still be progressing.
    """
    if limit < 1 or reserve_gib < 4.5:
        raise ValueError("Invalid bulk plan limit or reserve below 4.5 GiB")
    queue_sha, metadata_sha = _sha(queue), _sha(metadata)
    if output.exists():
        previous = json.loads(output.read_text())
        if previous.get("queue_sha256") != queue_sha or previous.get("selected_metadata_sha256") != metadata_sha:
            raise ValueError("Existing bulk plan is immutable; queue or source metadata changed")
        return previous | {"reused_existing_plan": True}
    listing = json.loads(metadata.read_text())
    if listing.get("queue_sha256", queue_sha) != queue_sha:
        raise ValueError("Mirror metadata was selected for a different source queue")
    origin = listing["origin"]
    metadata_kind = listing.get("metadata_kind", "official-compressed-filelist-csv")
    metadata_url = listing.get("metadata_url")
    if origin not in OFFICIAL_ORIGINS or not (
        (metadata_kind == "official-compressed-filelist-csv" and metadata_url == METADATA_URL)
        or (metadata_kind == "rsync-list-only-selected-files" and metadata_url == origin)
    ):
        raise ValueError("Unverified mirror or metadata origin")
    if not re.fullmatch(r"[a-f0-9]{64}", listing.get("metadata_sha256", "")):
        raise ValueError("Missing SHA-256 of the acquired official metadata stream")
    if metadata_kind == "rsync-list-only-selected-files":
        _verify_listing_capture(listing, metadata)
    files: dict[str, dict[str, Any]] = {}
    for row in listing["files"]:
        if row["path"] in files:
            raise ValueError("Duplicate mirror metadata path")
        files[row["path"]] = row
    existing = registered_ids(database)
    entries = load_manifest(queue)
    free = shutil.disk_usage(database.parent).free
    reserve = int(reserve_gib * 1024**3)
    budget = max(0, free - reserve)
    selected: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    projected = 0
    for entry in entries:
        if entry["id"] in existing:
            reasons["already_registered"] += 1
            continue
        match = re.fullmatch(r"pg([1-9]\d{0,7})-full", entry["id"])
        if match is None or entry["format"] != "epub":
            reasons["not_generated_epub"] += 1
            continue
        remote = files.get(f"{match[1]}/pg{match[1]}.epub")
        if remote is None:
            reasons["not_in_official_filelist"] += 1
            continue
        try:
            _validate_item(entry, remote, origin)
        except ValueError:
            reasons["source_identity_rights_or_extent_rejected"] += 1
            continue
        forecast = entry.get("resource_forecast", {})
        text_bytes = forecast.get("rdf_utf8_text_bytes")
        if type(text_bytes) is not int or text_bytes <= 0:
            reasons["missing_index_cost_reference"] += 1
            continue
        cost = max(int(forecast.get("estimated_added_bytes", 0)), 6 * text_bytes + remote["bytes"] + 65536)
        if len(selected) >= limit or projected + cost > budget:
            reasons["limit_or_disk_budget"] += 1
            continue
        selected.append({"entry": entry, "remote": remote, "estimated_added_bytes": cost})
        projected += cost
    plan: dict[str, Any] = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "origin": origin,
        "policy_url": POLICY_URL,
        "queue": str(queue),
        "queue_sha256": queue_sha,
        "metadata": str(metadata),
        "selected_metadata_sha256": metadata_sha,
        "metadata_url": listing["metadata_url"],
        "metadata_kind": metadata_kind,
        "metadata_sha256": listing["metadata_sha256"],
        "metadata_retrieved_at": listing.get("retrieved_at"),
        "protected_existing_ids": len(existing),
        "selected": len(selected),
        "selected_artifact_bytes": sum(i["remote"]["bytes"] for i in selected),
        "selected_by_language": dict(Counter(i["entry"]["language"] for i in selected)),
        "projected_added_bytes": projected,
        "free_bytes_before": free,
        "reserve_bytes": reserve,
        "budget_bytes": budget,
        "rejections": dict(reasons),
        "entries": selected,
        "identity_scope": (
            "Existing provisional author/title groups retained; bulk transport does not verify unique works"
        ),
        "illustrations_included": False,
    }
    _save(output, plan)
    return plan


def cache_root(data_dir: Path, origin: str) -> Path:
    if origin not in OFFICIAL_ORIGINS:
        raise ValueError("Unapproved mirror origin")
    return data_dir.absolute() / "downloads" / "bulk" / hashlib.sha256(origin.encode()).hexdigest()[:16]


def verified_bulk_source(item: dict[str, Any], root: Path, origin: str, plan_sha256: str) -> Path | None:
    """Verify actual mirror provenance/size/SHA, never an invented HTTP sidecar."""
    _validate_item(item["entry"], item["remote"], origin)
    path = _safe_child(root, item["remote"]["path"])
    receipt = path.with_suffix(path.suffix + ".bulk-source.json")
    if not path.is_file() or not receipt.is_file() or receipt.is_symlink():
        return None
    try:
        info = json.loads(receipt.read_text())
        if (
            info.get("schema_version") == 1
            and info.get("transport") == "rsync"
            and info.get("origin") == origin
            and info.get("remote_path") == item["remote"]["path"]
            and info.get("plan_sha256") == plan_sha256
            and info.get("bytes") == item["remote"]["bytes"]
            and info.get("remote_mtime") == item["remote"]["mtime"]
            and info.get("catalog_download_url") == item["entry"]["download_url"]
            and path.stat().st_size == info["bytes"]
            and _sha(path) == info.get("sha256")
        ):
            return path
    except (ValueError, OSError, AttributeError):
        return None
    return None


def publish_received(
    item: dict[str, Any],
    staging: Path,
    root: Path,
    plan: dict[str, Any],
    plan_sha256: str,
    *,
    retrieval_window_started_at: str,
    transfer_completed_at: str,
) -> Path:
    """Atomically promote a complete exact-size/mtime file; do not modify hardlinks."""
    existing = verified_bulk_source(item, root, plan["origin"], plan_sha256)
    if existing is not None:
        return existing
    source = _safe_child(staging, item["remote"]["path"])
    initial = source.stat()
    if not stat.S_ISREG(initial.st_mode) or initial.st_size != item["remote"]["bytes"]:
        raise ValueError("Received file size/type does not match exact official metadata")
    if int(initial.st_mtime) != item["remote"]["mtime"]:
        raise ValueError("Received modification time differs from pinned mirror metadata")
    digest = _sha(source)
    after = source.stat()
    if (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("Received source changed during hashing")
    target = _safe_child(root, item["remote"]["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    receipt = target.with_suffix(target.suffix + ".bulk-source.json")
    temporary = receipt.with_suffix(receipt.suffix + ".part")
    if receipt.is_symlink() or temporary.is_symlink():
        raise ValueError("Bulk provenance receipt paths must not be symlinks")
    info = {
        "schema_version": 1,
        "transport": "rsync",
        "origin": plan["origin"],
        "remote_path": item["remote"]["path"],
        "source_url": plan["origin"] + item["remote"]["path"],
        "catalog_download_url": item["entry"]["download_url"],
        "bytes": initial.st_size,
        "sha256": digest,
        "remote_mtime": item["remote"]["mtime"],
        "plan_sha256": plan_sha256,
        "metadata_url": plan["metadata_url"],
        "metadata_kind": plan["metadata_kind"],
        "metadata_sha256": plan["metadata_sha256"],
        "retrieval_window_started_at": retrieval_window_started_at,
        "retrieved_at": transfer_completed_at,
        "retrieval_time_basis": "First successful receiver batch completion; per-file socket timing unavailable",
        "checksum_scope": "Local SHA-256 plus rsync transfer verification; no authoritative upstream SHA is asserted",
    }
    temporary.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    source.replace(target)
    temporary.replace(receipt)
    return target


def manifest_entry(item: dict[str, Any], root: Path, plan: dict[str, Any], plan_sha256: str) -> dict[str, Any]:
    path = verified_bulk_source(item, root, plan["origin"], plan_sha256)
    if path is None:
        raise ValueError("Bulk file has no matching verified provenance receipt")
    receipt = json.loads(path.with_suffix(path.suffix + ".bulk-source.json").read_text())
    return item["entry"] | {
        "local_path": str(path),
        "source_artifact_sha256": receipt["sha256"],
        "retrieved_at": receipt["retrieved_at"],
        "bulk_acquisition": receipt,
        "original_filename": path.name,
    }


def transfer_command(origin: str, files_from: Path, staging: Path, *, binary: str = "/usr/bin/rsync") -> list[str]:
    if origin not in OFFICIAL_ORIGINS:
        raise ValueError("Unapproved mirror origin")
    paths = files_from.read_text().splitlines()
    if not paths or any(not PATH_PATTERN.fullmatch(path) for path in paths) or len(set(paths)) != len(paths):
        raise ValueError("Transfer list must contain only distinct exact same-ID EPUB files")
    return [
        binary,
        "-rt",
        "--relative",
        "--progress",
        "--no-motd",
        "--timeout=30",
        "--contimeout=15",
        f"--max-size={MAX_DOWNLOAD_BYTES}",
        "--partial-dir=.bookrag-rsync-partial",
        f"--files-from={files_from.absolute()}",
        origin,
        str(staging.absolute()) + "/",
    ]
