"""Acquisition snapshots must survive mutable provider caches and local edits."""

from __future__ import annotations

import errno
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO

import pytest

from book_rag import artifacts
from book_rag.artifacts import ArtifactIntegrityError, SourceChangedError, archive_artifact, locate_artifact


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def assert_no_temporaries(data_dir: Path) -> None:
    assert not list((data_dir / "artifacts").rglob(".acquire-*"))


def test_arbitrary_local_copy_survives_inplace_source_edit(tmp_path: Path) -> None:
    source, data = tmp_path / "source.HTML", tmp_path / "data"
    original = b"<p>Original edition.</p>"
    source.write_bytes(original)
    result = archive_artifact(source, data, digest(original).upper())
    assert result["method"] == "copy"
    assert result["path"] == data / "artifacts" / digest(original)[:2] / (digest(original) + ".html")
    assert result["bytes"] == len(original) and result["sha256"] == digest(original)
    assert source.stat().st_ino != result["path"].stat().st_ino
    source.write_bytes(b"<p>Revised local edition.</p>")
    revised = archive_artifact(source, data)
    assert revised["path"] != result["path"]
    assert result["path"].read_bytes() == original
    assert locate_artifact(data, digest(original)) == result["path"]
    assert_no_temporaries(data)


@pytest.mark.skipif(os.name == "nt", reason="Windows conservatively copies open download sources")
def test_controlled_download_hardlink_survives_atomic_provider_replacement(tmp_path: Path) -> None:
    data = tmp_path / "data"
    downloads = data / "downloads"
    downloads.mkdir(parents=True)
    source = downloads / "book.epub"
    original = b"Original EPUB bytes"
    source.write_bytes(original)
    permissions = source.stat().st_mode
    result = archive_artifact(source, data)
    assert result["method"] == "hardlink"
    assert result["path"].stat().st_ino == source.stat().st_ino
    assert source.stat().st_mode == permissions
    replacement = downloads / "new.tmp"
    replacement.write_bytes(b"Provider changed this edition")
    os.replace(replacement, source)
    assert result["path"].read_bytes() == original
    newer = archive_artifact(source, data)
    assert newer["path"] != result["path"] and newer["method"] == "hardlink"
    assert locate_artifact(data, result["sha256"]) == result["path"]
    assert_no_temporaries(data)


def test_identical_bytes_and_suffix_deduplicate_and_formats_stay_distinct(tmp_path: Path) -> None:
    data = tmp_path / "data"
    a, b, html = (tmp_path / name for name in ("a.txt", "b.TXT", "other.html"))
    for source in (a, b, html):
        source.write_bytes(b"Same bytes with distinct declared formats.")
    first = archive_artifact(a, data)
    repeated = archive_artifact(b, data)
    other_format = archive_artifact(html, data)
    assert repeated["path"] == first["path"] and repeated["method"] == "existing"
    assert other_format["sha256"] == first["sha256"]
    assert other_format["path"] != first["path"] and other_format["path"].suffix == ".html"
    assert locate_artifact(data, first["sha256"]) == other_format["path"], "Suffix selection is deterministic"
    assert_no_temporaries(data)


def test_local_file_inside_data_but_outside_downloads_is_copied(tmp_path: Path) -> None:
    data = tmp_path / "data"
    raw = data / "russian" / "raw.html"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"A locally generated or imported source.")
    archived = archive_artifact(raw, data)
    assert archived["method"] == "copy"
    raw.write_bytes(b"Modified in place")
    assert archived["path"].read_bytes() == b"A locally generated or imported source."


def test_external_symlink_or_symlinked_download_directory_never_hardlinks(tmp_path: Path) -> None:
    external = tmp_path / "external.html"
    external.write_bytes(b"External editable source")
    data = tmp_path / "data"
    downloads = data / "downloads"
    downloads.mkdir(parents=True)
    alias = downloads / "alias.html"
    alias.symlink_to(external)
    result = archive_artifact(alias, data)
    assert result["method"] == "copy"
    external.write_bytes(b"Changed external bytes")
    assert result["path"].read_bytes() == b"External editable source"
    other_data = tmp_path / "other-data"
    other_data.mkdir()
    other = tmp_path / "external-directory"
    other.mkdir()
    (other / "book.txt").write_bytes(b"Another external source")
    (other_data / "downloads").symlink_to(other, target_is_directory=True)
    assert archive_artifact(other_data / "downloads" / "book.txt", other_data)["method"] == "copy"


@pytest.mark.parametrize("failure", [errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP])
def test_source_link_failures_fall_back_to_atomic_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: int
) -> None:
    data = tmp_path / "data"
    source = data / "downloads" / "source.fb2"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"FB2 source snapshot")

    def denied(*args: object, **kwargs: object) -> None:
        raise OSError(failure, "Simulated hardlink restriction")

    monkeypatch.setattr(artifacts.os, "link", denied)
    result = archive_artifact(source, data)
    assert result["method"] == "copy" and result["path"].read_bytes() == source.read_bytes()
    assert result["path"].stat().st_ino != source.stat().st_ino
    assert_no_temporaries(data)


def test_changed_source_during_copy_is_rejected_and_owned_temporary_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, data = tmp_path / "local.pdf", tmp_path / "data"
    source.write_bytes(b"Original document bytes")
    original_copy = artifacts._copy_payload

    def changed(stream: BinaryIO, target: Path) -> None:
        original_copy(stream, target)
        source.write_bytes(b"New incompatible bytes!")

    monkeypatch.setattr(artifacts, "_copy_payload", changed)
    with pytest.raises(SourceChangedError):
        archive_artifact(source, data)
    assert_no_temporaries(data)
    assert not list((data / "artifacts").rglob("*.pdf"))


@pytest.mark.skipif(os.name == "nt", reason="Windows copies controlled download sources")
def test_changed_source_between_hash_and_hardlink_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    source = data / "downloads" / "source.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"Original source")
    link = os.link

    def changed(src: Path, dst: Path, *, follow_symlinks: bool = True) -> None:
        source.write_bytes(b"Changed while linking")
        link(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(artifacts.os, "link", changed)
    with pytest.raises(SourceChangedError):
        archive_artifact(source, data)
    assert_no_temporaries(data)
    assert not list((data / "artifacts").rglob("*.txt"))


def test_source_atomic_replacement_during_hash_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, data = tmp_path / "source.txt", tmp_path / "data"
    source.write_bytes(b"Original edition")
    original_digest = artifacts._digest

    def replaced(stream: BinaryIO) -> tuple[str, int]:
        value = original_digest(stream)
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"Different edition")
        os.replace(replacement, source)
        return value

    monkeypatch.setattr(artifacts, "_digest", replaced)
    with pytest.raises(SourceChangedError):
        archive_artifact(source, data)
    assert not (data / "artifacts").exists()


def test_corrupt_existing_archive_is_rejected_without_overwriting_history(tmp_path: Path) -> None:
    source, data = tmp_path / "source.txt", tmp_path / "data"
    source.write_bytes(b"Correct original bytes")
    record = archive_artifact(source, data)
    record["path"].write_bytes(b"Corrupt history must stay visible for diagnosis")
    with pytest.raises(ArtifactIntegrityError, match="mismatch"):
        archive_artifact(source, data)
    with pytest.raises(ArtifactIntegrityError):
        locate_artifact(data, record["sha256"])
    assert record["path"].read_bytes() == b"Corrupt history must stay visible for diagnosis"
    assert_no_temporaries(data)


def test_publish_failure_cleans_only_owned_temporary_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, data = tmp_path / "source.txt", tmp_path / "data"
    source.write_bytes(b"Original source")
    root = data / "artifacts"
    root.mkdir(parents=True)
    unrelated = root / "leave-me-alone.tmp"
    unrelated.write_bytes(b"Unrelated operator data")

    def failure(src: Path, dst: Path) -> None:
        raise OSError(errno.EIO, "Simulated publication failure")

    monkeypatch.setattr(artifacts, "_publish_no_replace", failure)
    with pytest.raises(OSError, match="publication"):
        archive_artifact(source, data)
    assert source.read_bytes() == b"Original source" and unrelated.read_bytes() == b"Unrelated operator data"
    assert_no_temporaries(data)
    assert not list(root.rglob("*.txt"))


def test_concurrent_identical_acquisitions_never_clobber(tmp_path: Path) -> None:
    source, data = tmp_path / "source.html", tmp_path / "data"
    source.write_bytes(b"Concurrent immutable edition" * 1024)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: archive_artifact(source, data), range(8)))
    assert len({result["path"] for result in results}) == 1
    assert results[0]["path"].read_bytes() == source.read_bytes()
    assert sum(result["method"] == "copy" for result in results) == 1
    assert_no_temporaries(data)


@pytest.mark.parametrize("bad", ["", "../escape", "a" * 63, "g" * 64, "a" * 65, "a/" + "b" * 62])
def test_invalid_hashes_cannot_select_arbitrary_paths(tmp_path: Path, bad: str) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"Source")
    with pytest.raises(ValueError, match="64 hexadecimal"):
        archive_artifact(source, tmp_path / "data", bad)
    with pytest.raises(ValueError, match="64 hexadecimal"):
        locate_artifact(tmp_path / "data", bad)


def test_expected_hash_mismatch_and_invalid_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    source = tmp_path / "source.txt"
    source.write_bytes(b"Source")
    with pytest.raises(ArtifactIntegrityError, match="expected"):
        archive_artifact(source, data, "0" * 64)
    source.write_bytes(b"")
    with pytest.raises(ValueError, match="1 byte"):
        archive_artifact(source, data)
    source.write_bytes(b"This source exceeds the test limit")
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 10)
    with pytest.raises(ValueError, match="100 MiB"):
        archive_artifact(source, data)
    unsupported = tmp_path / "source.exe"
    unsupported.write_bytes(b"No")
    with pytest.raises(ValueError, match="Unsupported"):
        archive_artifact(unsupported, data)
    directory = tmp_path / "directory.epub"
    directory.mkdir()
    with pytest.raises(ValueError, match="regular"):
        archive_artifact(directory, data)
    assert not (data / "artifacts").exists()
    assert locate_artifact(data, "f" * 64) is None


def test_archive_symlinks_are_rejected_without_reading_or_overwriting_target(tmp_path: Path) -> None:
    source, data = tmp_path / "source.txt", tmp_path / "data"
    content = b"Source"
    source.write_bytes(content)
    sha = digest(content)
    shard = data / "artifacts" / sha[:2]
    shard.mkdir(parents=True)
    target = shard / (sha + ".txt")
    target.symlink_to(source)
    with pytest.raises(ArtifactIntegrityError, match="nonsymlink"):
        archive_artifact(source, data)
    with pytest.raises(ArtifactIntegrityError):
        locate_artifact(data, sha)
    assert source.read_bytes() == content and target.is_symlink()
    target.unlink()
    shard.rmdir()
    shard.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ArtifactIntegrityError, match="shard"):
        locate_artifact(data, sha)


@pytest.mark.parametrize("suffix", sorted(artifacts.SUPPORTED_SUFFIXES))
def test_supported_formats_keep_safe_original_suffix(tmp_path: Path, suffix: str) -> None:
    source = tmp_path / ("Source" + suffix.upper())
    source.write_bytes(b"Acquired bytes are preserved before parsing their declared format.")
    record = archive_artifact(source, tmp_path / "data")
    assert record["path"].suffix == suffix
    assert record["path"].read_bytes() == source.read_bytes()


def test_publish_primitive_never_replaces_existing_destination(tmp_path: Path) -> None:
    staging, destination = tmp_path / "staging", tmp_path / "destination"
    staging.write_bytes(b"New payload")
    destination.write_bytes(b"Existing history")
    with pytest.raises(FileExistsError):
        artifacts._publish_no_replace(staging, destination)
    assert destination.read_bytes() == b"Existing history" and staging.read_bytes() == b"New payload"


def test_concurrent_corrupt_destination_is_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, data = tmp_path / "source.txt", tmp_path / "data"
    source.write_bytes(b"Correct source")
    publish = artifacts._publish_no_replace

    def other_publisher(src: Path, dst: Path) -> None:
        dst.write_bytes(b"An inconsistent preexisting history entry")
        publish(src, dst)

    monkeypatch.setattr(artifacts, "_publish_no_replace", other_publisher)
    with pytest.raises(ArtifactIntegrityError):
        archive_artifact(source, data)
    target = data / "artifacts" / digest(b"Correct source")[:2] / (digest(b"Correct source") + ".txt")
    assert target.read_bytes() == b"An inconsistent preexisting history entry"
    assert_no_temporaries(data)


@pytest.mark.skipif(os.name == "nt", reason="This race concerns a hardlinked POSIX payload")
def test_hash_race_after_hardlink_publication_removes_only_new_corrupt_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    source = data / "downloads" / "source.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"Before publication")
    publish = artifacts._publish_no_replace

    def mutate_before_publication(src: Path, dst: Path) -> None:
        source.write_bytes(b"Mutation violates the owned downloader atomic-replace contract")
        publish(src, dst)

    monkeypatch.setattr(artifacts, "_publish_no_replace", mutate_before_publication)
    with pytest.raises(ArtifactIntegrityError):
        archive_artifact(source, data)
    assert_no_temporaries(data)
    assert not list((data / "artifacts").rglob("*.txt"))


def test_interrupted_copy_cleans_owned_temporary_without_touching_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, data = tmp_path / "source.txt", tmp_path / "data"
    source.write_bytes(b"Original immutable acquisition")

    def interrupt(stream: BinaryIO, target: Path) -> None:
        target.write_bytes(b"Partial attempt")
        raise KeyboardInterrupt("Simulated interruption")

    monkeypatch.setattr(artifacts, "_copy_payload", interrupt)
    with pytest.raises(KeyboardInterrupt):
        archive_artifact(source, data)
    assert_no_temporaries(data)
    assert source.read_bytes() == b"Original immutable acquisition"
    assert not list((data / "artifacts").rglob("*.txt"))


def test_empty_existing_archive_is_an_explicit_integrity_error(tmp_path: Path) -> None:
    source, data = tmp_path / "source.txt", tmp_path / "data"
    source.write_bytes(b"Original source")
    record = archive_artifact(source, data)
    record["path"].write_bytes(b"")
    with pytest.raises(ArtifactIntegrityError, match="invalid size"):
        locate_artifact(data, record["sha256"])
    with pytest.raises(ArtifactIntegrityError, match="invalid size"):
        archive_artifact(source, data)
    assert record["path"].stat().st_size == 0
