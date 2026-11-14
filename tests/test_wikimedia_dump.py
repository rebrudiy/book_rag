"""Pinned acquisition and bounded raw-wikitext discovery, without search imports."""

from __future__ import annotations

import bz2
import hashlib
import io
import json
import sqlite3
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import httpx
import pytest
from defusedxml.common import DTDForbidden

from book_rag import wikimedia_dump as dump
from book_rag.sources import DownloadDeferred


def page(
    title: str,
    text: str,
    page_id: int = 1,
    *,
    namespace: int = 0,
    redirect: str | None = None,
    model: str = "wikitext",
    revision_id: int | None = None,
) -> str:
    target = f"<redirect title={quoteattr(redirect)}/>" if redirect else ""
    return (
        f"<page><title>{escape(title)}</title><ns>{namespace}</ns><id>{page_id}</id>{target}"
        f"<revision><id>{revision_id or page_id + 100}</id><timestamp>2026-10-31T12:00:00Z</timestamp>"
        f"<model>{model}</model><format>text/x-wiki</format><text bytes='{len(text.encode())}' "
        f"xml:space='preserve'>{escape(text)}</text><sha1>provider-base36-sha1</sha1></revision></page>"
    )


def xml(pages: list[str], *, site: str = "ruwikibooks") -> bytes:
    return (
        '<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/" xml:lang="en">'
        f"<siteinfo><dbname>{site}</dbname><base>https://ru.wikibooks.org/wiki/Заглавная_страница</base>"
        "<generator>MediaWiki test</generator><namespaces><namespace key='0'/></namespaces></siteinfo>"
        + "".join(pages)
        + "</mediawiki>"
    ).encode()


def artifact(tmp_path: Path, body: bytes) -> tuple[Path, dump.DumpSpec]:
    compressed = bz2.compress(body)
    name = "ruwikibooks-2026-11-01-p1p99.xml.bz2"
    path = tmp_path / name
    path.write_bytes(compressed)
    spec = dump.DumpSpec(
        "ruwikibooks",
        "2026-11-01",
        name,
        len(compressed),
        hashlib.sha256(compressed).hexdigest(),
        "https://dumps.wikimedia.org/other/mediawiki_content_current/ruwikibooks/2026-11-01/xml/bzip2/" + name,
    )
    return path, spec


def build(tmp_path: Path, pages: list[str], candidates: list[str]) -> dict[str, Any]:
    path, spec = artifact(tmp_path, xml(pages))
    return dump.build_wikimedia_catalog(path, tmp_path / "catalog", candidates, spec=spec)


def test_raw_text_and_revision_provenance_are_preserved(tmp_path: Path) -> None:
    raw = "{{Название учебника|100 %}}\n== Глава ==\n<pre>\tif x:  print('a  b')\n</pre>\nЁ & <math>a^2</math>\n"
    report = build(
        tmp_path, [page("Учебник", raw), page("Обсуждение:Учебник", "служебное", 2, namespace=1)], ["Учебник"]
    )
    assert report["pages_total"] == 2
    assert report["main_namespace_pages"] == 1
    assert report["book_completeness_claimed"] is False
    assert report["library_modified"] is False
    with sqlite3.connect(report["catalog"]) as connection:
        row = connection.execute(
            "SELECT wikitext,wikitext_sha256,revision_url,history_url FROM pages WHERE page_id=1"
        ).fetchone()
        assert row[0] == raw
        assert row[1] == hashlib.sha256(raw.encode()).hexdigest()
        assert row[2] == "https://ru.wikibooks.org/w/index.php?oldid=101"
        assert "action=history" in row[3]
        assert connection.execute("SELECT wikitext FROM pages WHERE page_id=2").fetchone()[0] is None
        assert dict(connection.execute("SELECT * FROM metadata"))["dump_sha256"] == report["dump_sha256"]
    candidate = report["candidates"][0]
    assert candidate["status"] == "graph_review_candidate"
    assert candidate["explicit_status_claims"][0]["percent_literals"] == ["100 %"]
    assert candidate["coverage"] == "unverified"
    assert candidate["full_book_claimed"] is False


def test_relative_chapters_redirects_and_navigation_cycles(tmp_path: Path) -> None:
    report = build(
        tmp_path,
        [
            page("Курс", "[[/Первая/|1]] [[/Алиас]] [[#Раздел]] <nowiki>[[/Не глава]]</nowiki>"),
            page("Курс/Первая", "[[../Вторая/]] [[Курс]]", 2),
            page("Курс/Вторая", "[[Курс/Первая]]", 3),
            page("Курс/Алиас", "#REDIRECT [[Курс/Вторая]]", 4, redirect="Курс/Вторая"),
        ],
        ["Курс", "Курс"],
    )
    assert len(report["candidates"]) == 1
    c = report["candidates"][0]
    assert c["root_link_order"] == ["Курс/Первая", "Курс/Вторая"]
    assert c["reachable_family_pages"] == 3
    assert c["explicit_inclusion_cycle"] is False
    assert c["graph_closed_on_explicit_links"] is True
    assert not c["missing_family_targets"]


def test_missing_orphan_and_transclusion_cycle_prevent_graph_closed_claim(tmp_path: Path) -> None:
    report = build(
        tmp_path,
        [
            page("Курс", "{{:Курс/Один}} [[/Нет]]"),
            page("Курс/Один", "{{:Курс}}", 2),
            page("Курс/Сирота", "Есть исходный текст", 3),
        ],
        ["Курс"],
    )
    c = report["candidates"][0]
    assert c["status"] == "graph_partial_or_template_navigation"
    assert c["explicit_inclusion_cycle"] is True
    assert c["unreachable_family_titles"] == ["Курс/Сирота"]
    assert c["missing_family_targets"][0]["target"] == "Курс/Нет"


def test_redirect_cycle_and_wrong_namespace_are_not_books(tmp_path: Path) -> None:
    report = build(
        tmp_path,
        [
            page("Один", "#REDIRECT [[Два]]", redirect="Два"),
            page("Два", "#REDIRECT [[Один]]", 2, redirect="Один"),
            page("Шаблон:Книга", "Исходный шаблон", 3, namespace=10),
        ],
        ["Один", "Шаблон:Книга", "Отсутствует"],
    )
    assert [c["status"] for c in report["candidates"]] == ["redirect_cycle", "noncontent_root", "missing_target"]


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        ("{{Название учебника|готовность=50%}}", "explicit_incomplete_or_draft"),
        ("Учебник ещё пишется", "explicit_incomplete_or_draft"),
        ("Примеры из книги Шилдт: Полный справочник по Java", "rights_review_required"),
        ("Это перевод с английского учебника", "rights_review_required"),
        ("Завершённый исходный текст", "graph_review_candidate"),
    ],
)
def test_source_completion_claims_and_rights_alerts(tmp_path: Path, raw: str, status: str) -> None:
    c = build(tmp_path, [page("Курс", raw)], ["Курс"])["candidates"][0]
    assert c["status"] == status
    assert c["full_book_claimed"] is False


def test_unsupported_missing_and_size_mismatch_are_explicit(tmp_path: Path) -> None:
    suppressed = page("Курс/Скрытая", "нет", 2).replace("<text bytes='6'", "<text deleted='deleted' bytes='6'")
    mismatch = page("Курс/Сломанная", "текст", 3).replace("bytes='10'", "bytes='11'")
    report = build(
        tmp_path,
        [
            page("Курс", "[[/Скрытая]] [[/Сломанная]] [[/JSON]]"),
            suppressed,
            mismatch,
            page("Курс/JSON", "{}", 4, model="json"),
        ],
        ["Курс"],
    )
    assert report["content_states"] == {
        "available": 1,
        "declared_size_mismatch": 1,
        "missing_or_suppressed": 1,
        "unsupported_model": 1,
    }
    assert len(report["candidates"][0]["content_issues"]) == 3
    assert not report["candidates"][0]["graph_closed_on_explicit_links"]


def test_per_page_metadata_limits_keep_raw_text_and_report_unsupported_graph(tmp_path: Path) -> None:
    raw = "{{small}} " * 2005
    c = build(tmp_path, [page("Курс", raw)], ["Курс"])["candidates"][0]
    assert {i["reason"] for i in c["content_issues"]} == {"template_call_extraction_limit"}
    assert len(c["root_templates"]) == 2000
    assert c["root_wikitext_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert not c["graph_closed_on_explicit_links"]


def test_links_preserve_offsets_but_ignore_literal_markup() -> None:
    raw = "Ё [[/Глава/#Заголовок|глава]] <pre>[[/Код]] {{Код}}</pre><!-- [[/Комментарий]] -->"
    links, templates, issues = dump._markup(raw, "Курс")
    assert len(links) == 1
    assert links[0]["target"] == "Курс/Глава"
    assert links[0]["fragment"] == "Заголовок"
    assert raw[links[0]["start"] : links[0]["end"]] == links[0]["raw"]
    assert not templates and not issues


def test_deep_graph_cycle_check_is_iterative() -> None:
    edges = {str(i): {str(i + 1)} for i in range(2000)}
    assert not dump._cycle(edges)
    edges["2000"] = {"0"}
    assert dump._cycle(edges)


def test_review_packet_preserves_pages_toc_and_license_without_library_manifest(tmp_path: Path) -> None:
    raw = "{{Название учебника|готовность=100%}}\n[[/Один]] [[Другой курс/Практикум]]"
    report = build(
        tmp_path,
        [
            page("Курс", raw),
            page("Курс/Один", "<pre>\tcode  verbatim\n</pre>", 2),
            page("Другой курс/Практикум", "Текст", 3),
        ],
        ["Курс"],
    )
    catalog = Path(report["catalog"])
    catalog_hash = hashlib.sha256(catalog.read_bytes()).hexdigest()
    result = dump.export_wikimedia_review_packet(catalog, "Курс", tmp_path / "packets")
    packet = json.loads(result["packet"].read_text())
    assert len(packet["pages"]) == 2
    assert packet["source_dump"]["dump_sha256"] == report["dump_sha256"]
    assert packet["license"]["id"] == "CC-BY-SA-4.0"
    assert packet["full_book_claimed"] is False and packet["library_modified"] is False
    assert packet["review"]["root_outside_family_links"][0]["target"] == "Другой курс/Практикум"
    assert packet["review"]["graph_scope"] == "title_subpage_family_only_not_authoritative_book_toc"
    assert (result["packet"].parent / packet["pages"][0]["raw_wikitext_file"]).read_text() == raw
    assert packet["pages"][0]["history_url"].endswith("?action=history")
    again = dump.export_wikimedia_review_packet(catalog, "Курс", tmp_path / "packets")
    assert again == result
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() == catalog_hash


def test_review_export_rejects_tampered_catalog_text(tmp_path: Path) -> None:
    report = build(tmp_path, [page("Курс", "Original text")], ["Курс"])
    catalog = Path(report["catalog"])
    with sqlite3.connect(catalog) as connection:
        connection.execute("UPDATE pages SET wikitext='tampered'")
    with pytest.raises(ValueError, match="size/SHA"):
        dump.export_wikimedia_review_packet(catalog, "Курс", tmp_path / "packets")
    assert not list((tmp_path / "packets").glob("*/review-packet.json"))


def test_dynamic_links_are_exposed_not_invented(tmp_path: Path) -> None:
    c = build(tmp_path, [page("Курс", "[[{{TALKPAGENAME}}|обсудить]]")], ["Курс"])["candidates"][0]
    assert {i["reason"] for i in c["content_issues"]} == {"dynamic_link_target"}
    assert not c["graph_closed_on_explicit_links"]


def test_graph_bound_does_not_claim_truncated_graph_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dump, "MAX_GRAPH_RECORDS", 2)
    c = build(tmp_path, [page("Курс", "[[/Один]] [[/Два]] [[/Три]]")], ["Курс"])["candidates"][0]
    assert c["status"] == "graph_exceeds_review_bound"
    assert c["full_book_claimed"] is False


def test_read_zero_does_not_consume_compressed_stream() -> None:
    reader = dump._BoundedBzipReader(io.BytesIO(bz2.compress(b"text")), 4)
    assert reader.read(0) == b""
    assert reader.total == 0
    assert reader.read(100) == b"text"
    assert reader.read(100) == b""


def test_main_revision_slot_preserves_text(tmp_path: Path) -> None:
    document = xml([page("Курс", "<syntaxhighlight>code  exact</syntaxhighlight>")])
    document = document.replace(b"<model>", b"<slots><slot role='main'><model>")
    document = document.replace(b"</revision>", b"</slot></slots></revision>")
    path, spec = artifact(tmp_path, document)
    report = dump.build_wikimedia_catalog(path, tmp_path / "catalog", ["Курс"], spec=spec)
    assert report["content_states"] == {"available": 1}
    assert (
        report["candidates"][0]["root_wikitext_sha256"]
        == hashlib.sha256(b"<syntaxhighlight>code  exact</syntaxhighlight>").hexdigest()
    )


def test_nested_xml_inside_text_is_rejected_instead_of_silently_losing_body(tmp_path: Path) -> None:
    document = xml([page("Курс", "plain")]).replace(b"plain</text>", b"plain<b>lost</b></text>")
    path, spec = artifact(tmp_path, document)
    with pytest.raises(ValueError, match="nested XML"):
        dump.build_wikimedia_catalog(path, tmp_path / "catalog", ["Курс"], spec=spec)


@pytest.mark.parametrize("bad", ["wrong-site", "duplicate-id", "dtd", "history", "wrong-root"])
def test_rejected_build_keeps_previous_published_catalog(tmp_path: Path, bad: str) -> None:
    old = build(tmp_path, [page("Курс", "Проверенный источник")], ["Курс"])
    catalog = Path(old["catalog"])
    old_hash = hashlib.sha256(catalog.read_bytes()).hexdigest()
    body = xml([page("Курс", "Новый источник")])
    if bad == "wrong-site":
        body = body.replace(b"<dbname>ruwikibooks", b"<dbname>ruwikisource")
    elif bad == "duplicate-id":
        body = xml([page("Курс", "Один"), page("Другой", "Два")])
    elif bad == "dtd":
        body = b'<!DOCTYPE mediawiki [<!ENTITY bad "content">]>' + body
    elif bad == "history":
        body = body.replace(b"</revision>", b"</revision><revision><id>500</id></revision>")
    elif bad == "wrong-root":
        body = body.replace(b"export-0.11", b"export-7.00")
    path, spec = artifact(tmp_path, body)
    with pytest.raises((ValueError, sqlite3.IntegrityError, DTDForbidden)):
        dump.build_wikimedia_catalog(path, catalog.parent, ["Курс"], spec=spec)
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() == old_hash
    assert not catalog.with_name("catalog.sqlite3.building").exists()


def test_exact_artifact_hash_and_safe_spec_are_required(tmp_path: Path) -> None:
    path, spec = artifact(tmp_path, xml([page("Курс", "Текст")]))
    path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="size/SHA"):
        dump.build_wikimedia_catalog(path, tmp_path / "catalog", [], spec=spec)
    unsafe = dump.DumpSpec(spec.wiki, spec.date, "../outside", spec.bytes, spec.sha256, "https://evil.example/dump")
    with pytest.raises(ValueError, match="official"):
        dump.build_wikimedia_catalog(path, tmp_path / "catalog", [], spec=unsafe)


@pytest.mark.parametrize("mode", ["limit", "truncated", "concatenated", "trailing"])
def test_bzip2_decoder_limits_before_xml_parsing(mode: str) -> None:
    compressed = bz2.compress(b"a" * 100_000)
    limit = 100 if mode == "limit" else 200_000
    if mode == "truncated":
        compressed = compressed[:-10]
    elif mode == "concatenated":
        compressed += bz2.compress(b"second")
    elif mode == "trailing":
        compressed += b"other"
    reader = dump._BoundedBzipReader(io.BytesIO(compressed), limit)
    with pytest.raises(ValueError):
        while reader.read(1024):
            pass


def install_http(monkeypatch: pytest.MonkeyPatch, body: bytes, status: int = 200, retry_after: str = "60") -> list[str]:
    client_type = httpx.Client
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["user-agent"] == dump.USER_AGENT
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(status, content=body, headers={"Retry-After": retry_after})

    def client(**kwargs: Any) -> httpx.Client:
        return client_type(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(dump.httpx, "Client", client)
    return calls


def test_pinned_acquisition_reuses_only_verified_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, spec = artifact(tmp_path, xml([page("Курс", "Текст")]))
    raw = path.read_bytes()
    path.unlink()
    monkeypatch.setattr(dump, "WIKIBOOKS_SPEC", spec)
    calls = install_http(monkeypatch, raw)
    first = dump.acquire_wikibooks_dump(tmp_path)
    assert first["cached"] is False and first["network_requests"] == 1
    second = dump.acquire_wikibooks_dump(tmp_path)
    assert second["cached"] is True and second["network_requests"] == 0
    assert len(calls) == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == spec.sha256
    assert not path.with_name(path.name + ".part").exists()


@pytest.mark.parametrize("mode", ["wrong-hash", "too-big", "redirect"])
def test_failed_download_is_not_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    path, spec = artifact(tmp_path, xml([page("Курс", "Текст")]))
    raw = path.read_bytes()
    path.unlink()
    monkeypatch.setattr(dump, "WIKIBOOKS_SPEC", spec)
    payload = raw[:-1] + b"X" if mode == "wrong-hash" else raw + b"large" if mode == "too-big" else raw
    calls = install_http(monkeypatch, payload, status=302 if mode == "redirect" else 200)
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        dump.acquire_wikibooks_dump(tmp_path)
    assert len(calls) == 1
    assert not path.exists() and not path.with_name(path.name + ".part").exists()


def test_long_provider_defer_is_persisted_without_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, spec = artifact(tmp_path, xml([page("Курс", "Текст")]))
    path.unlink()
    monkeypatch.setattr(dump, "WIKIBOOKS_SPEC", spec)
    calls = install_http(monkeypatch, b"", status=429, retry_after="1e100")
    with pytest.raises(DownloadDeferred):
        dump.acquire_wikibooks_dump(tmp_path)
    state = json.loads((tmp_path / "acquisition.json").read_text())
    assert state["retry_at_epoch"] > 1e90 and state["retry_at"] is None
    with pytest.raises(ValueError, match="deferred"):
        dump.acquire_wikibooks_dump(tmp_path)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "state", [[], {"source_url": "bad"}, {"source_url": dump.WIKIBOOKS_SPEC.url, "retry_at_epoch": "broken"}]
)
def test_damaged_cooldown_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: Any) -> None:
    calls = install_http(monkeypatch, b"")
    (tmp_path / "acquisition.json").write_text(json.dumps(state))
    with pytest.raises(ValueError):
        dump.acquire_wikibooks_dump(tmp_path)
    assert not calls


def test_valid_symlink_cache_is_rejected_before_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, spec = artifact(tmp_path, xml([page("Курс", "Текст")]))
    target = tmp_path / "outside.bz2"
    path.replace(target)
    path.symlink_to(target)
    monkeypatch.setattr(dump, "WIKIBOOKS_SPEC", spec)
    calls = install_http(monkeypatch, b"")
    with pytest.raises(ValueError, match="symlink"):
        dump.acquire_wikibooks_dump(tmp_path)
    assert not calls
