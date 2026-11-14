"""Pinned acquisition, exact source preservation and restartable packaging."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from defusedxml import ElementTree as ET

from book_rag import modern_sources
from book_rag.modern_sources import acquire_d2l
from book_rag.parsers import parse_book
from book_rag.sources import load_manifest


@pytest.fixture
def source_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    sources = {
        "LICENSE": b"Mock CC BY-SA 4.0 text license.",
        "LICENSE-SAMPLECODE": b"Mock original sample code license.",
        "LICENSE-SUMMARY": b"Mock text and code license separation.",
        "chapter_intro/index.md": (
            b"# Introduction\n\nA paragraph with <xml> & literal Markdown.\n\n## A subsection\n\nMore text."
        ),
        "chapter_math/attention.md": (
            b"```python\n# This comment must not become a chapter title\n```\n\n# Attention\n\n"
            b"Paragraph with $x^2$ and :cite:`paper`.\n\n"
            b'```python\n\tif True:\n    message = "a  b"\n    print(message)\n```\n\n'
            b"$$\n  y = x  +  z\n$$\n"
        ),
    }
    monkeypatch.setattr(
        modern_sources,
        "D2L_LICENSES",
        {k: hashlib.sha256(v).hexdigest() for k, v in sources.items() if k.startswith("LICENSE")},
    )
    monkeypatch.setattr(modern_sources, "D2L_CHAPTERS", tuple(k for k in sources if not k.startswith("LICENSE")))
    return sources


def install_http(
    monkeypatch: pytest.MonkeyPatch,
    sources: dict[str, bytes],
    override: Callable[[str], httpx.Response | None] | None = None,
) -> list[str]:
    client = httpx.Client
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        assert url.startswith(modern_sources.D2L_BASE)
        source_path = url[len(modern_sources.D2L_BASE) :]
        if override is not None:
            response = override(source_path)
            if response is not None:
                return response
        return httpx.Response(200, content=sources[source_path])

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    return calls


def test_pinned_acquisition_keeps_exact_raw_sources_and_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fixture: dict[str, bytes],
) -> None:
    calls = install_http(monkeypatch, source_fixture)
    report = acquire_d2l(tmp_path)
    assert (report["chapters"], report["downloaded_sources"]) == (2, 5)
    assert report["bytes"] == sum(len(v) for v in source_fixture.values())
    manifest = load_manifest(report["manifest"])
    assert len(manifest) == 1
    entry = manifest[0]
    assert entry["work_id"] == "d2l-en"
    assert entry["coverage"] == "selected_chapters"
    assert "not the complete book" in entry["coverage_note"]
    assert entry["source_revision"] in entry["edition"]
    assert "CC BY-SA 4.0" in entry["rights"] and "modified MIT" in entry["rights"]
    provenance = json.loads((tmp_path / "provenance.json").read_text())
    assert provenance["artifact_sha256"] == hashlib.sha256(report["book"].read_bytes()).hexdigest()
    assert len(provenance["licenses"]) == 3
    assert [c["title"] for c in provenance["chapters"]] == ["Introduction", "Attention"]
    for record in [*provenance["licenses"], *provenance["chapters"]]:
        original = source_fixture[record["source_path"]]
        assert record["sha256"] == hashlib.sha256(original).hexdigest()
        assert (tmp_path / record["local_path"]).read_bytes() == original
        assert record["retrieved_at"]
        assert modern_sources.D2L_REVISION in record["source_url"]
    assert len(calls) == 5


def test_code_math_and_xml_sensitive_text_survive_conversion_and_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fixture: dict[str, bytes],
) -> None:
    install_http(monkeypatch, source_fixture)
    report = acquire_d2l(tmp_path)
    root = ET.fromstring(report["book"].read_bytes())
    code_values = ["".join(e.itertext()) for e in root.findall(".//{*}code")]
    literal_code = '```python\n\tif True:\n    message = "a  b"\n    print(message)\n```'
    literal_math = "$$\n  y = x  +  z\n$$"
    assert literal_code in code_values and literal_math in code_values
    parsed = parse_book(report["book"])
    text = "\n\n".join(s.text for s in parsed.sections)
    assert literal_code in text and literal_math in text
    assert "A paragraph with <xml> & literal Markdown." in text
    assert any(s.title == "A subsection" for s in parsed.sections)
    assert "CC BY-SA 4.0" in text
    assert "modified MIT" in text


def test_resume_reuses_verified_downloads_and_artifact_is_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fixture: dict[str, bytes],
) -> None:
    calls = install_http(monkeypatch, source_fixture)
    first = acquire_d2l(tmp_path)
    before = first["book"].read_bytes()
    manifest_before = first["manifest"].read_bytes()
    second = acquire_d2l(tmp_path)
    assert (second["cached_sources"], second["downloaded_sources"]) == (5, 0)
    assert len(calls) == 5
    assert second["book"].read_bytes() == before
    assert second["manifest"].read_bytes() == manifest_before


def test_interrupted_acquisition_preserves_old_manifest_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fixture: dict[str, bytes],
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]\n")
    original_manifest = manifest.read_bytes()
    fail = True

    def override(source_path: str) -> httpx.Response | None:
        return httpx.Response(404) if fail and source_path.endswith("attention.md") else None

    calls = install_http(monkeypatch, source_fixture, override)
    with pytest.raises(httpx.HTTPStatusError):
        acquire_d2l(tmp_path)
    assert manifest.read_bytes() == original_manifest
    assert not list(tmp_path.glob("*.fb2"))
    fail = False
    report = acquire_d2l(tmp_path)
    assert report["chapters"] == 2
    assert report["cached_sources"] == 4
    assert report["downloaded_sources"] == 1
    assert len(calls) == 6


def test_unverified_license_stops_before_chapter_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = {"LICENSE": b"An unrelated or altered license response."}
    calls = install_http(monkeypatch, sources)
    with pytest.raises(ValueError, match="license SHA-256"):
        acquire_d2l(tmp_path)
    assert len(calls) == 1
    assert not (tmp_path / "manifest.json").exists()


def test_unterminated_source_blocks_fail_without_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fixture: dict[str, bytes],
) -> None:
    source_fixture["chapter_math/attention.md"] = b"# Attention\n\n```python\nunterminated"
    install_http(monkeypatch, source_fixture)
    with pytest.raises(ValueError, match="Unterminated"):
        acquire_d2l(tmp_path)
    assert not (tmp_path / "manifest.json").exists()


def test_invalid_xml_controls_fail_without_silent_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fixture: dict[str, bytes],
) -> None:
    source_fixture["chapter_math/attention.md"] = b"# Attention\n\nSource\x00text."
    install_http(monkeypatch, source_fixture)
    with pytest.raises(ValueError, match="control characters"):
        acquire_d2l(tmp_path)
    assert not (tmp_path / "manifest.json").exists()
