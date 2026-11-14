"""Bounded exact-ID authority evidence, never production work merges."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from book_rag import authority_audit as audit
from book_rag import work_identity as legacy


def entry(number: int, language: str = "en") -> dict[str, Any]:
    return {
        "id": f"pg{number}-full",
        "source_url": f"https://www.gutenberg.org/ebooks/{number}",
        "title": "Example",
        "author": "Author, Alice",
        "language": language,
        "work_id": f"pg{number}",
        "work_identity_status": "provisional",
        "work_type": "book_or_text",
        "coverage": "full",
        "catalog_rights_checked": True,
        "rights": "Public domain in the USA.",
    }


def binding(
    number: int, item: str, node: str | None = None, predicate: str = "P31", value: str = "Q8261"
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "gutenberg": {"type": "literal", "value": str(number)},
        "item": {"type": "uri", "value": legacy.ENTITY + item},
    }
    if node:
        row.update(
            node={"type": "uri", "value": legacy.ENTITY + node},
            predicate={"type": "uri", "value": legacy.DIRECT + predicate},
            value={"type": "uri", "value": legacy.ENTITY + value},
        )
    return row


def install_http(monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]) -> list[httpx.Request]:
    client_type = httpx.Client
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "query.wikidata.org"
        assert request.headers["user-agent"] == legacy.USER_AGENT
        assert request.method == "GET"
        return responses.pop(0)

    monkeypatch.setattr(audit.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        audit.httpx, "Client", lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(handler))
    )
    return calls


def response(rows: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(
        200, json={"results": {"bindings": rows}}, headers={"content-type": "application/sparql-results+json"}
    )


def test_exact_authority_chain_and_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        binding(100, "Q1", "Q1", "P31", "Q3331189"),
        binding(100, "Q1", "Q1", "P629", "Q2"),
        binding(100, "Q1", "Q2"),
        binding(100, "Q1", "Q2", "P50", "Q3"),
    ]
    calls = install_http(monkeypatch, [response(rows)])
    first = audit.propose_authority_audit([entry(100)], tmp_path)
    second = audit.propose_authority_audit([entry(100)], tmp_path)
    proposal = first["proposals"][0]
    assert proposal["confidence"] == "authority_linked" and proposal["candidate_qid"] == "Q2"
    assert proposal["candidates"][0]["path"] == ["Q1", "Q2"]
    assert proposal["candidates"][0]["candidate_work"]["label"] is None
    assert proposal["work_id"] == "pg100" and proposal["source_scope_verified"] is False
    assert first["identity_count_promotion_allowed"] is False and first["database_modified"] is False
    assert len(calls) == 1 and second["network_requests"] == 0 and second["cache_hits"] == 1


@pytest.mark.parametrize(
    "kind", ["multiple-items", "multiple-parents", "cycle", "parts", "unknown-type", "edition-root"]
)
def test_ambiguity_is_not_promoted(kind: str) -> None:
    rows = [binding(100, "Q1", "Q1")]
    if kind == "multiple-items":
        rows += [binding(100, "Q2", "Q2")]
    elif kind == "multiple-parents":
        rows += [binding(100, "Q1", "Q1", "P629", "Q2"), binding(100, "Q1", "Q1", "P629", "Q3")]
    elif kind == "cycle":
        rows += [binding(100, "Q1", "Q1", "P629", "Q2"), binding(100, "Q1", "Q2", "P629", "Q1")]
    elif kind == "parts":
        rows += [binding(100, "Q1", "Q1", "P527", "Q2")]
    else:
        rows = [binding(100, "Q1", "Q1", "P31", "Q571" if kind == "unknown-type" else "Q3331189")]
    mapping, metadata = audit.parse_batch(rows, {"100"}, "a" * 64)
    if kind == "multiple-items":
        assert len(mapping["100"]) == 2
    else:
        result = audit.classify("Q1", metadata)
        assert result["confidence"] == "ambiguous"
        assert result["source_scope_verified"] is False


def test_invalid_or_duplicate_provider_identity_never_queries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_http(monkeypatch, [])
    with pytest.raises(ValueError):
        audit.propose_authority_audit([entry(100), entry(100)], tmp_path)
    bad = entry(100)
    bad["source_url"] = "https://example.com/ebooks/100"
    with pytest.raises(ValueError):
        audit.propose_authority_audit([bad], tmp_path)
    assert not calls


@pytest.mark.parametrize("status", [429, 403, 503])
def test_defer_stops_remaining_batches_and_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    calls = install_http(monkeypatch, [httpx.Response(status, headers={"retry-after": "3600"})])
    entries = [entry(n) for n in range(100, 201)]
    first = audit.propose_authority_audit(entries, tmp_path)
    second = audit.propose_authority_audit(entries, tmp_path)
    assert len(calls) == 1 and first["network_requests"] == 1 and second["network_requests"] == 0
    assert first["counts"] == {"unresolved": 101}
    assert all(p["reason"] == "authority_unavailable" for p in first["proposals"])
    assert first["deferred"] and (tmp_path / "queries/deferred.json").is_file()


def test_semantically_invalid_response_rejected_whole_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_http(monkeypatch, [response([binding(100, "Q1", "Q1"), binding(999, "Q2", "Q2")])])
    result = audit.propose_authority_audit([entry(100), entry(101)], tmp_path)
    assert result["counts"] == {"unresolved": 2} and len(calls) == 1
    assert result["proposals"][0]["p2034_evidence"] == []


def test_no_exact_match_is_distinct_from_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_http(monkeypatch, [response([])])
    result = audit.propose_authority_audit([entry(100)], tmp_path)
    assert result["proposals"][0]["reason"] == "no_exact_p2034_claim_in_response"
    assert result["proposals"][0]["mapping_query_complete"] is True


def test_answered_50_is_separate_from_deferred_150(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_http(monkeypatch, [response([binding(100, "Q1", "Q1")]), httpx.Response(503)])
    result = audit.propose_authority_audit([entry(n) for n in range(100, 300)], tmp_path)
    assert len(calls) == 2
    assert sum(p["mapping_query_complete"] for p in result["proposals"]) == 50
    assert result["reason_counts"]["authority_unavailable"] == 150
    assert result["reason_counts"]["no_exact_p2034_claim_in_response"] == 49
    assert result["distinct_candidate_work_roots"] == 1


def test_four_batches_need_no_followup_author_or_parent_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = install_http(monkeypatch, [response([]) for _ in range(4)])
    result = audit.propose_authority_audit([entry(n) for n in range(100, 300)], tmp_path)
    assert len(calls) == 4 and all(p["mapping_query_complete"] for p in result["proposals"])
    assert result["identity_count_promotion_allowed"] is False


def test_deadline_can_stop_before_first_network_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_http(monkeypatch, [])
    result = audit.propose_authority_audit([entry(100)], tmp_path, deadline_seconds=1e-12)
    assert not calls and result["network_requests"] == 0 and result["deferred"]


@pytest.mark.parametrize("bad", ["html", "oversized", "rows"])
def test_error_payloads_stop_without_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    payload = httpx.Response(200, text="<html>blocked</html>", headers={"content-type": "text/html"})
    if bad == "oversized":
        payload = httpx.Response(200, content=b"x" * 100, headers={"content-type": "application/json"})
        monkeypatch.setattr(audit, "MAX_RESPONSE_BYTES", 50)
    elif bad == "rows":
        payload = response([binding(100, "Q1")] * 5001)
    calls = install_http(monkeypatch, [payload])
    result = audit.propose_authority_audit([entry(100)], tmp_path)
    assert len(calls) == 1 and result["deferred"] and result["counts"] == {"unresolved": 1}


def test_query_is_explicit_bounded_values_not_unbounded_path() -> None:
    q = audit.batch_query(["100", "101"])
    assert 'VALUES ?gutenberg { "100" "101" }' in q
    assert "(wdt:P629?)/(wdt:P629?)/(wdt:P629?)" in q
    assert "LIMIT 5001" in q and "wdt:P629*" not in q
    with pytest.raises(ValueError):
        audit.batch_query([str(n) for n in range(1, 52)])


def test_selection_is_public_indexed_balanced_and_skips_reviews(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    manifest = tmp_path / "public.json"
    entries = [entry(n, language) for n, language in enumerate(["en", "de", "fr"] * 5, 100)]
    entries[4]["work_identity_status"] = "curated"
    entries[5]["work_type"] = "periodical_issue"
    entries[6]["coverage"] = "partial"
    entries[7]["catalog_rights_checked"] = False
    manifest.write_text(json.dumps(entries))
    with sqlite3.connect(database) as c:
        c.executescript(
            "CREATE TABLE books(id TEXT,status TEXT,work_id TEXT,artifact_sha256 TEXT,local_path TEXT,source_url TEXT);"
            "CREATE TABLE book_metadata(book_id TEXT,metadata TEXT);"
        )
        c.executemany(
            "INSERT INTO books VALUES(?,?,?,?,?,?)",
            [(e["id"], "indexed", e["work_id"], "a" * 64, "/ignored/public.epub", e["source_url"]) for e in entries],
        )
        c.execute("UPDATE books SET status='discovered' WHERE id='pg108-full'")
        c.execute("UPDATE books SET source_url='https://private.invalid/import' WHERE id='pg112-full'")
    selected, report = audit.select_public_entries([manifest], database, {"pg100-full"}, limit=6)
    assert len(selected) == 6 and report["selected_by_language"] == {"en": 2, "de": 2, "fr": 2}
    assert not {e["id"] for e in selected} & {
        "pg100-full",
        "pg104-full",
        "pg105-full",
        "pg106-full",
        "pg107-full",
        "pg108-full",
        "pg112-full",
    }
    assert report["skipped"]["indexed_source_not_same_public_provider_id"] == 1


def test_cached_epub_frontmatter_bound_and_observations(tmp_path: Path) -> None:
    path = tmp_path / "source.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        z.writestr(
            "OEBPS/content.opf",
            '<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>Example</dc:title>'
            '<dc:creator>Alice Author</dc:creator></metadata><manifest><item id="title" href="title.xhtml"/>'
            '</manifest><spine><itemref idref="title"/></spine></package>',
        )
        z.writestr("OEBPS/title.xhtml", "<html><body>Example by Alice Author. Printed edition.</body></html>")
    e = {
        **entry(100),
        "indexed_local_path": str(path),
        "indexed_artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    evidence = audit.cached_frontmatter(e)
    p = {"confidence": "unresolved", "reason": "no_exact_p2034_claim_in_response", "candidates": []}
    result = audit.frontmatter_observations(e, p, evidence)
    assert result["catalog_title_matches_opf"] and result["catalog_author_surname_present"]
    assert evidence["status"] == "cached_artifact_sha_verified" and result["work_root_verified"] is False
