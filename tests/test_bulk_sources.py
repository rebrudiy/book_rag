"""Synthetic CC0 bytes and mocked rsync; no actual book/provider requests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import runpy
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zipfile import ZipFile

import pytest

from book_rag import bulk_sources as bulk
from book_rag.library import Library

RUNNER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/run_bulk_sources.py"))
ORIGIN = "rsync://gutenberg.pglaf.org/gutenberg-epub/"
MTIME = 1760000000


def epub(number: int) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w") as z:
        z.writestr(
            "META-INF/container.xml", '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>'
        )
        z.writestr(
            "book.opf",
            '<package><manifest><item id="c" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="c"/></spine></package>',
        )
        z.writestr(
            "chapter.xhtml",
            f"<html><head><title>CC0 example {number}</title></head><body><h1>Chapter</h1>"
            f"<p>The amber bird numbered {number} remembers the copper bookmark. "
            "This synthetic passage is CC0.</p></body></html>",
        )
    return output.getvalue()


def entry(number: int, size: int) -> dict[str, Any]:
    return {
        "id": f"pg{number}-full",
        "title": f"Synthetic CC0 source {number}",
        "author": "CC0 fixture creator",
        "language": "en",
        "work_id": f"candidate-{number}",
        "edition": "Synthetic transport test, not a real PG record",
        "source_url": f"https://www.gutenberg.org/ebooks/{number}",
        "download_url": f"https://www.gutenberg.org/cache/epub/{number}/pg{number}.epub",
        "format": "epub",
        "rights": "Public domain in the USA.",
        "rights_url": "https://www.gutenberg.org/policy/license.html",
        "illustrations_included": False,
        "coverage_scope": "text_only_without_illustrations",
        "source_variant": {"format_family": "epub_noimages"},
        "work_identity_status": "provisional",
        "resource_forecast": {
            "rdf_utf8_text_bytes": 40000,
            "rdf_artifact_bytes": size,
            "estimated_added_bytes": 400000,
        },
    }


@pytest.fixture
def example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(100 * 1024**3, 0, 100 * 1024**3))
    library = Library(tmp_path / "library")
    control = tmp_path / "control.txt"
    control.write_text("An old protected public-domain control remains unchanged.")
    meta = entry(1, 100)
    library.import_book(control, meta)
    library.save({"kind": "query", "query": "Preserve my fixture query", "note": "A private-note control"})
    payloads = {n: epub(n) for n in [1, 101, 102]}
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps([entry(n, len(payloads[n])) for n in payloads]))
    raw = tmp_path / "listing.txt"
    stamp = datetime.fromtimestamp(MTIME, UTC).strftime("%Y/%m/%d %H:%M:%S")
    raw.write_text("".join(f"-rw-r--r-- {len(data)} {stamp} {n}/pg{n}.epub\n" for n, data in payloads.items()))
    listing = {
        "origin": ORIGIN,
        "metadata_kind": "rsync-list-only-selected-files",
        "metadata_url": ORIGIN,
        "metadata_sha256": bulk._sha(raw),
        "raw_metadata_path": str(raw),
        "raw_metadata_sha256": bulk._sha(raw),
        "raw_metadata_bytes": raw.stat().st_size,
        "timezone": "UTC",
        "retrieved_at": "2026-01-01T00:00:00+00:00",
        "queue_sha256": bulk._sha(queue),
        "files": [{"path": f"{n}/pg{n}.epub", "bytes": len(data), "mtime": MTIME} for n, data in payloads.items()],
    }
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps(listing))
    plan_path = tmp_path / "bulk" / "plan.json"
    plan = bulk.prepare_bulk_plan(queue, metadata, library.db_path, plan_path)
    bulk._save(
        plan_path.with_suffix(".reachability.json"),
        {
            "reachable_exact_file": True,
            "origin": ORIGIN,
            "plan_sha256": bulk._sha(plan_path),
        },
    )
    return {
        "library": library,
        "payloads": payloads,
        "queue": queue,
        "metadata": metadata,
        "listing": listing,
        "plan_path": plan_path,
        "plan": plan,
    }


def install_transfer(
    monkeypatch: pytest.MonkeyPatch, payloads: dict[int, bytes], *, returncode: int = 0
) -> list[list[str]]:
    calls: list[list[str]] = []

    def transfer(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(command)
        files = (
            Path(next(a.split("=", 1)[1] for a in command if a.startswith("--files-from="))).read_text().splitlines()
        )
        staging = Path(command[-1])
        for name in files:
            number = int(name.split("/", 1)[0])
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payloads[number])
            os.utime(target, (MTIME, MTIME))
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(RUNNER["subprocess"], "run", transfer)
    return calls


def test_plan_is_readonly_excludes_all_old_ids_and_is_immutable(example: dict[str, Any]) -> None:
    library, plan = example["library"], example["plan"]
    assert plan["selected"] == 2 and plan["protected_existing_ids"] == 1
    assert [i["entry"]["id"] for i in plan["entries"]] == ["pg101-full", "pg102-full"]
    assert plan["metadata_kind"] == "rsync-list-only-selected-files" and plan["metadata_url"] == ORIGIN
    before = library.get_book("pg1-full")
    again = bulk.prepare_bulk_plan(example["queue"], example["metadata"], library.db_path, example["plan_path"])
    assert again["reused_existing_plan"] and library.get_book("pg1-full") == before
    example["metadata"].write_text("{}")
    with pytest.raises(ValueError, match="immutable"):
        bulk.prepare_bulk_plan(example["queue"], example["metadata"], library.db_path, example["plan_path"])


@pytest.mark.parametrize(
    "mutation", ["foreign_host", "other_id", "image_variant", "negative_rights", "credentials", "oversize"]
)
def test_rights_source_identity_and_noimage_extent_fail_closed(example: dict[str, Any], mutation: str) -> None:
    item = example["plan"]["entries"][0]
    e, r = item["entry"], item["remote"]
    if mutation == "foreign_host":
        e["download_url"] = "https://example.org/101/pg101.epub"
    if mutation == "other_id":
        r["path"] = "102/pg102.epub"
    if mutation == "image_variant":
        e["source_variant"]["format_family"] = "epub_images"
    if mutation == "negative_rights":
        e["rights"] = "Not public domain in the USA."
    if mutation == "credentials":
        e["source_url"] = "https://secret@www.gutenberg.org/ebooks/101"
    if mutation == "oversize":
        r["bytes"] = 100 * 1024**2 + 1
    with pytest.raises(ValueError):
        bulk._validate_item(e, r, ORIGIN)


@pytest.mark.parametrize(
    "path", ["../101/pg101.epub", "/101/pg101.epub", "101/pg102.epub", "101/pg101-images.epub", "101/pg101.epub\nother"]
)
def test_transfer_files_from_cannot_escape_or_recursively_select_directories(tmp_path: Path, path: str) -> None:
    files = tmp_path / "files.txt"
    files.write_text(path + "\n")
    with pytest.raises(ValueError):
        bulk.transfer_command(ORIGIN, files, tmp_path / "staging")


def test_atomic_cache_preserves_real_origin_sha_and_cached_retrieval_time(
    example: dict[str, Any], tmp_path: Path
) -> None:
    item, plan = example["plan"]["entries"][0], example["plan"]
    staging, root = tmp_path / "staging", bulk.cache_root(example["library"].data_dir, ORIGIN)
    source = staging / item["remote"]["path"]
    source.parent.mkdir(parents=True)
    source.write_bytes(example["payloads"][101])
    os.utime(source, (MTIME, MTIME))
    sha = bulk._sha(example["plan_path"])
    target = bulk.publish_received(
        item, staging, root, plan, sha, retrieval_window_started_at="first", transfer_completed_at="completed"
    )
    result = bulk.manifest_entry(item, root, plan, sha)
    assert result["bulk_acquisition"]["source_url"] == ORIGIN + item["remote"]["path"]
    assert result["download_url"].startswith("https://www.gutenberg.org/")
    assert result["source_artifact_sha256"] == hashlib.sha256(example["payloads"][101]).hexdigest()
    assert not target.with_suffix(".epub.source.json").exists()
    bulk.publish_received(
        item, staging, root, plan, sha, retrieval_window_started_at="later", transfer_completed_at="later"
    )
    assert bulk.manifest_entry(item, root, plan, sha)["retrieved_at"] == "completed"
    target.write_bytes(b"corrupt")
    assert bulk.verified_bulk_source(item, root, ORIGIN, sha) is None


def test_wrong_size_mtime_or_symlink_is_never_published(example: dict[str, Any], tmp_path: Path) -> None:
    item, plan = example["plan"]["entries"][0], example["plan"]
    staging, root = tmp_path / "staging", tmp_path / "cache"
    source = staging / item["remote"]["path"]
    source.parent.mkdir(parents=True)
    source.write_bytes(b"bad")
    os.utime(source, (MTIME, MTIME))
    args = (item, staging, root, plan, bulk._sha(example["plan_path"]))
    with pytest.raises(ValueError, match="size"):
        bulk.publish_received(*args, retrieval_window_started_at="first", transfer_completed_at="done")
    source.write_bytes(example["payloads"][101])
    os.utime(source, (MTIME + 1, MTIME + 1))
    with pytest.raises(ValueError, match="time"):
        bulk.publish_received(*args, retrieval_window_started_at="first", transfer_completed_at="done")
    source.unlink()
    source.symlink_to(example["queue"])
    with pytest.raises(ValueError, match="symlink"):
        bulk.publish_received(*args, retrieval_window_started_at="first", transfer_completed_at="done")


def test_payload_refuses_existing_serial_runner_lock_before_library_or_network(
    example: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    library = example["library"]
    monkeypatch.setattr(RUNNER["subprocess"], "run", lambda *a, **k: pytest.fail("No competing transport"))
    monkeypatch.setattr("book_rag.library.Library", lambda *a, **k: pytest.fail("No schema/DB constructor before lock"))
    with (library.data_dir / ".gutenberg-acquisition.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="shared lock"):
            RUNNER["run"](example["plan_path"], library.data_dir, allow_transfer=True)


def test_actual_temp_import_and_resume_preserve_old_book_notes_receipts_and_reader_quotes(
    example: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = example["library"]
    before, saved = library.get_book("pg1-full"), library.saved()
    calls = install_transfer(monkeypatch, example["payloads"])
    state = RUNNER["run"](example["plan_path"], library.data_dir, allow_transfer=True, max_books=1, batch_size=1)
    assert state["processed_this_run"] == 1 and len(calls) == 1
    state = RUNNER["run"](example["plan_path"], library.data_dir, allow_transfer=True, batch_size=1)
    assert state["stage"] == "queue_complete" and len(calls) == 2
    assert state["completed"]["indexed_untouched"] == 1
    manifest = json.loads(example["plan_path"].with_suffix(".local-manifest.json").read_text())
    assert len(manifest) == 2 and library.get_book("pg1-full") == before and library.saved() == saved
    for e in manifest:
        result = library.search("amber", "baseline", book_id=e["id"])["results"][0]
        assert result["verified"] and result["reader_url"] and result["artifact_sha256"] == e["source_artifact_sha256"]
        archived = Path(library.get_book(e["id"])["local_path"])
        assert archived.stat().st_ino == Path(e["local_path"]).stat().st_ino
    again = RUNNER["run"](example["plan_path"], library.data_dir, allow_transfer=True)
    assert again["completed"]["indexed_untouched"] == 2 and len(calls) == 2
    assert len(json.loads(example["plan_path"].with_suffix(".local-manifest.json").read_text())) == 2


def test_failed_transfer_retains_partial_stage_and_defers_all_provider_hosts_without_registering_sources(
    example: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_transfer(monkeypatch, example["payloads"], returncode=23)
    library = example["library"]
    state = RUNNER["run"](example["plan_path"], library.data_dir, allow_transfer=True)
    assert state["stage"] == "transfer_failed_deferred" and len(calls) == 1
    assert bulk.registered_ids(library.db_path) == {"pg1-full"}
    assert RUNNER["provider_retry_at"](library.db_path) > RUNNER["time"].time()
    assert list(bulk.cache_root(library.data_dir, ORIGIN).glob(".staging/*/*.epub"))
    assert not list(bulk.cache_root(library.data_dir, ORIGIN).glob("*/*.bulk-source.json"))
    with pytest.raises(ValueError, match="deferred"):
        RUNNER["run"](example["plan_path"], library.data_dir, allow_transfer=True)
    assert len(calls) == 1


def test_disk_reserve_prevents_payload_and_import(example: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    library = example["library"]
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(5 * 1024**3, 0, 4 * 1024**3))
    calls = install_transfer(monkeypatch, example["payloads"])
    state = RUNNER["run"](example["plan_path"], library.data_dir, allow_transfer=True)
    assert state["stage"] == "stopped_disk_reserve" and not calls
    assert bulk.registered_ids(library.db_path) == {"pg1-full"}


def test_raw_metadata_digest_and_selected_extent_are_independently_checked(example: dict[str, Any]) -> None:
    listing = json.loads(example["metadata"].read_text())
    listing["files"][1]["bytes"] += 1
    example["metadata"].write_text(json.dumps(listing))
    with pytest.raises(ValueError, match="retained raw"):
        bulk.prepare_bulk_plan(
            example["queue"],
            example["metadata"],
            example["library"].db_path,
            example["plan_path"].with_name("changed.json"),
        )
    listing["files"][1]["bytes"] -= 1
    example["metadata"].write_text(json.dumps(listing))
    Path(listing["raw_metadata_path"]).write_text("corrupt metadata")
    with pytest.raises(ValueError, match="Raw selected-file"):
        bulk.prepare_bulk_plan(
            example["queue"],
            example["metadata"],
            example["library"].db_path,
            example["plan_path"].with_name("changed.json"),
        )


def test_replacing_invalid_cache_uses_rename_preserving_archived_hardlink(
    example: dict[str, Any], tmp_path: Path
) -> None:
    item, plan = example["plan"]["entries"][0], example["plan"]
    staging, root = tmp_path / "stage", tmp_path / "cache"
    source = staging / item["remote"]["path"]
    source.parent.mkdir(parents=True)
    original = example["payloads"][101]
    source.write_bytes(original)
    os.utime(source, (MTIME, MTIME))
    args = (item, staging, root, plan, bulk._sha(example["plan_path"]))
    target = bulk.publish_received(*args, retrieval_window_started_at="first", transfer_completed_at="done")
    archived = tmp_path / "archived-original.epub"
    os.link(target, archived)
    target.with_suffix(
        ".epub.bulk-source.json"
    ).unlink()  # Receipt missing; existing hardlinked payload remains untouched.
    source.write_bytes(original[::-1])
    os.utime(source, (MTIME, MTIME))
    replacement = bulk.publish_received(*args, retrieval_window_started_at="second", transfer_completed_at="later")
    assert archived.read_bytes() == original and replacement.read_bytes() == original[::-1]
    assert archived.stat().st_ino != replacement.stat().st_ino
    files = tmp_path / "selected.txt"
    files.write_text(item["remote"]["path"] + "\n")
    command = bulk.transfer_command(ORIGIN, files, staging)
    assert not {"--delete", "--inplace", "--append", "--remove-source-files"} & set(command)


def test_sigint_waits_for_actual_local_transport_child_before_releasing_provider_lock(
    example: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A local sleeping executable stands in for rsync; no network is used."""
    import fcntl
    import signal
    import subprocess
    import sys
    import time

    child_pid = tmp_path / "transport-child.pid"
    binary = tmp_path / "fake-rsync"
    binary.write_text(
        f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\n"
        f"Path({str(child_pid)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n"
    )
    binary.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "scripts/run_bulk_sources.py"
    code = (
        "import runpy,sys; from pathlib import Path; r=runpy.run_path(sys.argv[1]); "
        "r['run'](Path(sys.argv[2]),Path(sys.argv[3]),allow_transfer=True,binary=sys.argv[4])"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(script),
            str(example["plan_path"]),
            str(example["library"].data_dir),
            str(binary),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid.exists(), "The actual local transport child must have started"
        transport_pid = int(child_pid.read_text())
        process.send_signal(signal.SIGINT)
        process.wait(timeout=5)
        with pytest.raises(ProcessLookupError):
            os.kill(transport_pid, 0)
        state = json.loads(example["plan_path"].with_suffix(".run-state.json").read_text())
        assert state["stage"] == "interrupted_resumable"
        assert state["transfer_seconds"] > 0
        with (example["library"].data_dir / ".gutenberg-acquisition.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("zone,valid", [("UTC (TZ=UTC child environment)", True), ("local timezone unknown", False)])
def test_capture_requires_explicit_utc_even_when_research_receipt_uses_another_field(
    example: dict[str, Any],
    zone: str,
    valid: bool,
) -> None:
    metadata = json.loads(example["metadata"].read_text())
    metadata.pop("timezone")
    metadata["mtime_timezone"] = zone
    if valid:
        bulk._verify_listing_capture(metadata, example["metadata"])
    else:
        with pytest.raises(ValueError, match="timezone"):
            bulk._verify_listing_capture(metadata, example["metadata"])
