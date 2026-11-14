"""One-page acquisition, source rights and regex/code/table preservation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from book_rag import wikibooks_sources as wiki
from book_rag.parsers import parse_book
from book_rag.sources import DownloadDeferred, load_manifest

RAW = (
    "{{Название учебника}}\n== Раздел ==\n=== Подраздел ===\n"
    "Текст <code><nowiki>(<.*>)</nowiki></code> и <code>{{Highlight|a  b}}</code>.\n"
    "<source>\n  x\t = 'a  b'\n+ ?\n</source>\n"
    "{|\n! Шаблон !! Значение\n|-\n| a  b || Первый\n|}\n"
)
HTML = (
    '<div class="mw-parser-output"><style>hidden decoration</style><img src="https://not-fetched.example/image"/>'
    '<div class="mw-heading"><h2 id="first">Раздел</h2></div><h3 id="second">Подраздел</h3>'
    "<p>Текст <code>(&lt;.*&gt;)</code> и <code><span>a  b</span></code>.</p>"
    "<div><pre>  x\t = 'a  b'\n+\u00a0?</pre></div>"
    "<table><tr><th>Шаблон</th><th>Значение</th></tr><tr><td><code>a  b</code></td><td>Первый</td></tr>"
    '<tr><td colspan="2">Общая строка</td></tr></table></div>'
)


@pytest.fixture
def fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # Add the third code element corresponding to the explicit table literal.
    raw = RAW.replace("| a  b ||", "| <code>a  b</code> ||")
    monkeypatch.setattr(wiki, "WIKITEXT_SHA256", hashlib.sha256(raw.encode()).hexdigest())
    monkeypatch.setattr(wiki.time, "sleep", lambda seconds: None)
    return {
        "parse": {
            "title": wiki.TITLE,
            "pageid": wiki.PAGE_ID,
            "revid": wiki.REVISION_ID,
            "wikitext": raw,
            "text": HTML,
            "sections": [{"line": "Раздел"}, {"line": "Подраздел"}],
            "parsewarnings": [],
        }
    }


def talk() -> dict[str, Any]:
    return {
        "query": {
            "rightsinfo": {"url": wiki.LICENSE_URL + "deed.ru", "text": "Creative Commons Attribution-Share Alike 4.0"},
            "pages": [
                {"revisions": [{"revid": 106231, "slots": {"main": {"content": "Обсуждение ошибки в примере."}}}]}
            ],
        }
    }


def history() -> dict[str, Any]:
    return {
        "query": {
            "pages": [
                {
                    "pageid": wiki.PAGE_ID,
                    "revisions": [
                        {"revid": 25968, "parentid": 0, "comment": "начато на основе [[w:Регулярные выражения]]"},
                        {"revid": 60681, "comment": "дополнение из англ версии"},
                    ],
                }
            ]
        }
    }


def install_http(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    *,
    status: int = 200,
    retry_after: str = "60",
    corrupt_policy: bool = False,
) -> list[httpx.Request]:
    client_type = httpx.Client
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["user-agent"] == wiki.USER_AGENT
        assert request.headers["accept-encoding"] == "gzip"
        if status != 200:
            return httpx.Response(status, headers={"Retry-After": retry_after})
        if request.url.path == "/w/api.php":
            assert request.url.params["maxlag"] == "5"
            if request.url.params["action"] == "parse":
                assert request.url.params["oldid"] == str(wiki.REVISION_ID)
                return httpx.Response(200, json=payload)
            if "meta" in request.url.params:
                return httpx.Response(200, json=talk())
            return httpx.Response(200, json=history())
        body = (
            "Авторские права CC BY-SA 4.0; Licensing of Content; Re-use; Attribution; "
            "Attribution-ShareAlike 4.0 International; Section 3"
        )
        return httpx.Response(200, text="<html>" + ("captcha" if corrupt_policy else body) + "</html>")

    def client(**kwargs: Any) -> httpx.Client:
        return client_type(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(wiki.httpx, "Client", client)
    return calls


def test_end_to_end_preserves_literals_tables_credits_and_partial_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any]
) -> None:
    calls = install_http(monkeypatch, fixture)
    result = wiki.acquire_wikibooks_regex(tmp_path)
    assert len(calls) == result["network_requests"] == 6
    assert result["cache_hits"] == 0 and result["coverage"] == "partial"
    entry = load_manifest(result["manifest"])[0]
    assert entry["work_identity_status"] == "provisional"
    assert entry["book_completion"] == "unverified"
    assert entry["illustrations_included"] is False
    assert "templates may reflect current versions" in entry["render_provenance"]
    assert len(entry["upstream_attribution"]) == 2
    assert entry["uncertain_upstream_notes"][0]["revision_id"] == 60681
    parsed = parse_book(result["book"])
    body = "\n\n".join(s.text for s in parsed.sections)
    titles = "\n".join(s.title for s in parsed.sections)
    assert "Раздел / Подраздел" in titles
    assert "\n  x\t = 'a  b'\n+ ?\n" in body
    assert "(<.*>)" in body and "a  b" in body
    assert "Шаблон\tЗначение\na  b\tПервый\nОбщая строка\t" in body
    assert "hidden decoration" not in body
    provenance = json.loads((tmp_path / "provenance.json").read_text())
    audit = provenance["projection_audit"]
    assert audit["main_content_text_nodes_consumed_exactly_once"] is True
    assert audit["source_blocks"][0]["render_changes_beyond_edge_lf"] is True
    assert audit["tables"][0]["rows"] == 3
    assert len(audit["inline_code"]) == 3
    assert all(r["request_sha256"] and r["response_sha256"] and r["retrieved_at"] for r in provenance["responses"])
    assert provenance["library_modified"] is False


def test_resume_is_byte_identical_and_sends_no_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any]
) -> None:
    calls = install_http(monkeypatch, fixture)
    first = wiki.acquire_wikibooks_regex(tmp_path)
    second = wiki.acquire_wikibooks_regex(tmp_path)
    assert len(calls) == 6
    assert second["network_requests"] == 0 and second["cache_hits"] == 6
    assert first["artifact_sha256"] == second["artifact_sha256"]
    assert first["source_bundle_sha256"] == second["source_bundle_sha256"]


def test_cached_packaging_keeps_original_network_retrieval_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any]
) -> None:
    calls = install_http(monkeypatch, fixture)
    first = wiki.acquire_wikibooks_regex(tmp_path)
    before = json.loads(first["manifest"].read_text())[0]
    later = datetime.now(UTC) + timedelta(hours=1)
    monkeypatch.setattr(wiki, "datetime", SimpleNamespace(now=lambda tz: later))
    second = wiki.acquire_wikibooks_regex(tmp_path)
    after = json.loads(second["manifest"].read_text())[0]
    assert after["retrieved_at"] == before["retrieved_at"] == before["rendered_at"]
    assert after["packaged_at"] == later.isoformat() != before["packaged_at"]
    assert after["artifact_sha256"] == before["artifact_sha256"]
    assert after["source_bundle_sha256"] == before["source_bundle_sha256"]
    assert second["network_requests"] == 0 and len(calls) == 6


def test_tampered_response_cache_is_refetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any]
) -> None:
    calls = install_http(monkeypatch, fixture)
    wiki.acquire_wikibooks_regex(tmp_path)
    source = next(
        p
        for p in (tmp_path / "responses").glob("*.json")
        if not p.name.endswith(".source.json") and '"parse"' in p.read_text()
    )
    source.write_text("corrupt")
    resumed = wiki.acquire_wikibooks_regex(tmp_path)
    assert len(calls) == 7 and resumed["network_requests"] == 1


@pytest.mark.parametrize("bad", ["revision", "page", "title", "raw-sha", "parsewarning"])
def test_pinned_identity_or_source_mismatch_stops_before_more_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any], bad: str
) -> None:
    if bad == "revision":
        fixture["parse"]["revid"] += 1
    elif bad == "page":
        fixture["parse"]["pageid"] += 1
    elif bad == "title":
        fixture["parse"]["title"] = "Другая книга"
    elif bad == "raw-sha":
        fixture["parse"]["wikitext"] += "changed"
    else:
        fixture["parse"]["parsewarnings"] = ["unsupported content"]
    calls = install_http(monkeypatch, fixture)
    with pytest.raises(ValueError):
        wiki.acquire_wikibooks_regex(tmp_path)
    assert len(calls) == 1 and not (tmp_path / "manifest.json").exists()


@pytest.mark.parametrize("bad", ["license", "restriction", "upstream"])
def test_rights_checks_do_not_guess_permission(fixture: dict[str, Any], bad: str) -> None:
    t, h = talk(), history()
    raw = fixture["parse"]["wikitext"]
    if bad == "license":
        t["query"]["rightsinfo"]["url"] = "https://example.com/all-rights-reserved"
    elif bad == "restriction":
        raw += " All rights reserved"
    else:
        h["query"]["pages"][0]["revisions"][0]["comment"] = "unknown"
    with pytest.raises(ValueError):
        wiki._rights_review(raw, t, h)


@pytest.mark.parametrize("bad", ["missing-pre", "inline-code", "nested-table", "rowspan", "math", "headings"])
def test_projection_rejects_lossy_or_unsupported_structures(fixture: dict[str, Any], bad: str) -> None:
    parsed = fixture["parse"]
    if bad == "missing-pre":
        parsed["text"] = parsed["text"].replace("<pre>", "<p>").replace("</pre>", "</p>")
    elif bad == "inline-code":
        parsed["text"] = parsed["text"].replace("(&lt;.*&gt;)", "changed")
    elif bad == "nested-table":
        parsed["text"] = parsed["text"].replace("Первый", "Первый<table><tr><td>x</td></tr></table>")
    elif bad == "rowspan":
        parsed["text"] = parsed["text"].replace("<th>", "<th rowspan='2'>", 1)
    elif bad == "math":
        parsed["text"] = parsed["text"].replace("Текст", "<math>x^2</math>Текст")
    else:
        parsed["sections"] = []
    with pytest.raises(ValueError):
        wiki._project(parsed)


def test_license_captcha_does_not_produce_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any]
) -> None:
    install_http(monkeypatch, fixture, corrupt_policy=True)
    with pytest.raises(ValueError, match="policy"):
        wiki.acquire_wikibooks_regex(tmp_path)
    assert not (tmp_path / "manifest.json").exists()


@pytest.mark.parametrize("status", [403, 429, 503])
def test_provider_defer_persists_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any], status: int
) -> None:
    calls = install_http(monkeypatch, fixture, status=status, retry_after="3600")
    with pytest.raises(DownloadDeferred):
        wiki.acquire_wikibooks_regex(tmp_path)
    with pytest.raises(ValueError, match="cooldown"):
        wiki.acquire_wikibooks_regex(tmp_path)
    assert len(calls) == 1
    assert not (tmp_path / "manifest.json").exists()


def test_response_size_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any]
) -> None:
    install_http(monkeypatch, fixture)
    monkeypatch.setattr(wiki, "MAX_RESPONSE_BYTES", 100)
    with pytest.raises(ValueError, match="limit"):
        wiki.acquire_wikibooks_regex(tmp_path)
    assert not (tmp_path / "manifest.json").exists()


def test_fetch_rejects_unapproved_provider_and_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any]
) -> None:
    calls = install_http(monkeypatch, fixture, status=302)
    fetcher = wiki._Fetch(tmp_path)
    try:
        with pytest.raises(ValueError, match="approved"):
            fetcher.fetch("https://example.com/w/api.php", "invalid")
        assert not calls
        with pytest.raises(httpx.HTTPStatusError):
            fetcher.fetch(wiki._render_request(), "redirect")
        assert len(calls) == 1
    finally:
        fetcher.close()


def test_pacing_survives_restart_and_cache_hits_do_not_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any]
) -> None:
    delays: list[float] = []
    monkeypatch.setattr(wiki.time, "sleep", delays.append)
    calls = install_http(monkeypatch, fixture)
    wiki.acquire_wikibooks_regex(tmp_path)
    assert len(delays) == 5 and all(0 < delay <= wiki.REQUEST_INTERVAL for delay in delays)
    delays.clear()
    wiki.acquire_wikibooks_regex(tmp_path)
    assert not delays and len(calls) == 6
    # Force one cache miss immediately in a fresh fetcher: persisted spacing
    # still applies even though its in-memory monotonic clock starts at zero.
    next(p for p in (tmp_path / "responses").glob("*.html")).unlink()
    wiki.acquire_wikibooks_regex(tmp_path)
    assert len(delays) == 1 and delays[0] > 0


@pytest.mark.parametrize("state", [{"retry_at_epoch": "bad"}, {"retry_at_epoch": 1, "host": "evil.example"}])
def test_corrupt_cooldown_fails_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any], state: dict[str, Any]
) -> None:
    calls = install_http(monkeypatch, fixture)
    (tmp_path / "provider-state.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="cooldown"):
        wiki.acquire_wikibooks_regex(tmp_path)
    assert not calls
