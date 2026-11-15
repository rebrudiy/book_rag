"""Offline review packets provide evidence without approving source coverage."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import httpx
import pytest

from book_rag import russian_short_sources as short
from book_rag import russian_sources as ws

inspect_sources = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "audit_russian_short_works.py"))[
    "inspect_short_sources"
]


def test_packet_is_unapproved_and_never_requests_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chosen = short.SHORT_WORKS[0]
    raw = (
        '<div class="mw-parser-output"><h2>I<span class="mw-editsection">править</span></h2>'
        '<p>Полный исходный абзац.</p><ol class="references"><li id="cite_note-a">'
        "Примечание автора.</li></ol></div>"
    ).encode()
    receipt = {"raw_sha256": ws._sha(raw), "revision_id": 7}
    monkeypatch.setattr(short, "SHORT_WORKS", (chosen,))
    monkeypatch.setattr(short, "_cached_page", lambda cache, title: (raw, receipt))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("Audit created an HTTP client"))
    cache, output = tmp_path / "provider", tmp_path / "audit"
    cache.mkdir()
    state = cache / "state.json"
    state.write_text('{"source":"must stay unchanged"}')
    old_state = state.read_bytes()
    packet = inspect_sources(cache, output)
    assert packet["network_requests"] == 0 and packet["library_mutations"] == 0
    assert packet["captured_pages"] == 1
    assert packet["works"][0]["completion_claim"] == "not_verified_by_this_script"
    assert packet["works"][0]["headings"] == [{"tag": "h2", "text": "I"}]
    template = json.loads((output / "reviews-template.json").read_text())[chosen.work.slug]
    assert template["review_status"] == "review_required" and template["body_start"] == ""
    assert template["author_note_ids"] == template["editorial_note_ids"] == []
    assert template["unclassified_note_ids"] == ["cite_note-a"]
    assert state.read_bytes() == old_state


def test_missing_sources_are_reported_without_a_work_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(cache: Path, title: str) -> None:
        raise ValueError("No verified capture")

    monkeypatch.setattr(short, "_cached_page", missing)
    packet = inspect_sources(tmp_path / "cache", tmp_path / "audit")
    assert packet["captured_pages"] == 0 and len(packet["missing"]) == 10
    assert json.loads((tmp_path / "audit" / "reviews-template.json").read_text()) == {}


def test_previous_review_packet_is_never_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "audit"
    output.mkdir()
    evidence = output / "source-audit-packet.json"
    evidence.write_text('{"original":"evidence"}')
    with pytest.raises(FileExistsError):
        inspect_sources(tmp_path / "cache", output)
    assert evidence.read_text() == '{"original":"evidence"}'
    assert not (output / "reviews-template.json").exists()


def test_audit_output_cannot_modify_provider_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="separate"):
        inspect_sources(tmp_path / "provider", tmp_path / "provider" / "audit")
