"""Compact statement metadata and shared access state; no real endpoint requests."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import httpx
import pytest

from book_rag import authority_catalog as ac

GUID = "01234567-89ab-cdef-0123-456789abcdef"
HEADER = "?pg\t?item\t?claim\t?rank\n"


def tsv(rows: list[tuple[str, str, str]]) -> bytes:
    return (HEADER + "".join(f'"{pg}"\t"{qid}"\t"{qid}-{GUID}"\t"{rank}"\n' for pg, qid, rank in rows)).encode()


def count_response(count: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={"results": {"bindings": [{"assertions": {"type": "literal", "value": str(count)}}]}},
        headers={"content-type": "application/sparql-results+json"},
    )


def mock_client(monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]) -> list[httpx.Request]:
    calls: list[httpx.Request] = []
    original = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "query.wikidata.org" and request.method == "GET"
        assert request.headers["user-agent"].startswith("BookRAG/")
        return responses.pop(0)

    monkeypatch.setattr(ac.httpx, "Client", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(ac.time, "sleep", lambda seconds: None)
    return calls


def test_all_statement_ranks_and_competing_exact_items_are_preserved() -> None:
    mapping, invalid, rows = ac.parse_catalog(tsv([("11", "Q1", "NormalRank"), ("11", "Q2", "PreferredRank")]))
    assert rows == 2 and not invalid and {s["qid"] for s in mapping["11"]} == {"Q1", "Q2"}
    assert {s["rank"] for s in mapping["11"]} == {"NormalRank", "PreferredRank"}
    assert "DeprecatedRank" in ac.CATALOG_QUERY and "p:P2034" in ac.CATALOG_QUERY
    assert "LIMIT" not in ac.CATALOG_QUERY  # No silently truncated map.


@pytest.mark.parametrize(
    "body", [b"html", (HEADER + '"11"\t"Q1"\n').encode(), b"?wrong\t?header\n", tsv([("11", "Q1", "NormalRank")])[:-1]]
)
def test_malformed_or_truncated_tsv_is_never_a_partial_catalog(body: bytes) -> None:
    with pytest.raises(ValueError):
        ac.parse_catalog(body)


def test_invalid_identifiers_deprecated_claims_and_duplicates_are_explicit_not_bound() -> None:
    body = tsv(
        [
            ("11", "Q1", "NormalRank"),
            ("11", "Q1", "NormalRank"),
            ("not-a-pg-id", "Q2", "NormalRank"),
            ("12", "Q3", "DeprecatedRank"),
        ]
    )
    mapping, invalid, rows = ac.parse_catalog(body)
    assert rows == 4 and len(invalid) == 3 and list(mapping) == ["11"] and len(mapping["11"]) == 1


def test_catalog_fetch_is_two_cached_requests_with_stable_original_retrieval_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = mock_client(
        monkeypatch,
        [
            count_response(2),
            httpx.Response(
                200,
                content=tsv([("11", "Q1", "NormalRank"), ("12", "Q2", "PreferredRank")]),
                headers={"content-type": "text/tab-separated-values"},
            ),
        ],
    )
    state = tmp_path / "common.json"
    first = ac.acquire_identifier_catalog(tmp_path / "catalog", cooldowns=(), shared_state=state)
    assert first["count_consistent"] and first["distinct_gutenberg_ids"] == 2
    assert first["network_requests"] == 2 and len(calls) == 2
    times = [r["retrieved_at"] for r in first["query_evidence"]]
    # Successful immutable caches remain readable while the provider is deferred.
    state.write_text(json.dumps({"retry_at_epoch": time.time() + 3600}))
    again = ac.acquire_identifier_catalog(tmp_path / "catalog", cooldowns=(), shared_state=state)
    assert again["cache_hits"] == 2 and again["network_requests"] == 0 and len(calls) == 2
    assert [r["retrieved_at"] for r in again["query_evidence"]] == times
    assert again["identity_count_promotion_allowed"] is False
    assert again["database_modified"] is False


def test_count_mismatch_does_not_invent_complete_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client(
        monkeypatch,
        [
            count_response(2),
            httpx.Response(
                200, content=tsv([("11", "Q1", "NormalRank")]), headers={"content-type": "text/tab-separated-values"}
            ),
        ],
    )
    result = ac.acquire_identifier_catalog(tmp_path / "catalog", cooldowns=(), shared_state=tmp_path / "common.json")
    assert not result["count_consistent"] and result["observed_statement_rows"] == 1


def test_new_destination_cannot_bypass_the_longest_existing_provider_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = mock_client(monkeypatch, [])
    old, older, common = tmp_path / "old.json", tmp_path / "older.json", tmp_path / "common.json"
    until = time.time() + 7200
    old.write_text(json.dumps({"retry_at_epoch": time.time() + 60}))
    older.write_text(json.dumps({"retry_at_epoch": until}))
    result = ac.acquire_identifier_catalog(tmp_path / "new-destination", cooldowns=(old, older), shared_state=common)
    assert result["network_requests"] == 0 and not calls and result["deferred"]
    assert json.loads(common.read_text())["retry_at_epoch"] == until


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, headers={"retry-after": "7200"}),
        httpx.Response(200, text="<html>error</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, content=b"{" * 65537, headers={"content-type": "application/sparql-results+json"}),
    ],
)
def test_http_throttle_html_and_oversize_stop_without_retry_or_endpoint_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    calls = mock_client(monkeypatch, [response])
    common = tmp_path / "common.json"
    result = ac.acquire_identifier_catalog(tmp_path / "catalog", cooldowns=(), shared_state=common)
    assert len(calls) == 1 and result["deferred"] and not result["mapping"]
    state = json.loads(common.read_text())
    assert state["automatic_retry"] is False and state["retry_at_epoch"] > time.time()
    if response.status_code == 429:
        assert state["retry_at_epoch"] >= time.time() + 7190
    again = ac.acquire_identifier_catalog(tmp_path / "other-destination", cooldowns=(), shared_state=common)
    assert again["network_requests"] == 0 and len(calls) == 1


def test_raw_body_cache_digest_is_independently_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        count_response(1),
        httpx.Response(
            200, content=tsv([("11", "Q1", "NormalRank")]), headers={"content-type": "text/tab-separated-values"}
        ),
    ]
    calls = mock_client(monkeypatch, responses)
    directory, common = tmp_path / "catalog", tmp_path / "common.json"
    ac.acquire_identifier_catalog(directory, cooldowns=(), shared_state=common)
    count_cache = next((directory / "queries").glob("*.response.json"))
    count_cache.write_bytes(b"corrupt")
    responses.append(count_response(1))
    result = ac.acquire_identifier_catalog(directory, cooldowns=(), shared_state=common)
    assert result["network_requests"] == 1 and result["cache_hits"] == 1 and len(calls) == 3
    receipt = next(
        p
        for p in (directory / "queries").glob("*.receipt.json")
        if json.loads(p.read_text())["query"] == ac.COUNT_QUERY
    )
    assert json.loads(receipt.read_text())["response_sha256"] == hashlib.sha256(count_cache.read_bytes()).hexdigest()


def relation(node: str, prop: str, target: str, rank: str = "NormalRank") -> dict:
    return {
        "node": {"type": "uri", "value": ac.legacy.ENTITY + node},
        "predicate": {"type": "uri", "value": ac.legacy.DIRECT + prop},
        "value": {"type": "uri", "value": ac.legacy.ENTITY + target},
        "statement": {"type": "uri", "value": "http://www.wikidata.org/entity/statement/" + node + "-" + GUID},
        "rank": {"type": "uri", "value": "http://wikiba.se/ontology#" + rank},
    }


def local(number: int, manual: str = "manual-A") -> dict:
    return {
        "id": f"pg{number}-full",
        "gutenberg_id": str(number),
        "source_url": f"https://www.gutenberg.org/ebooks/{number}",
        "title": "Local title must not become an authority label",
        "author": "Local author",
        "work_id": manual,
        "work_identity_status": "curated",
        "work_type": "book_or_text",
        "language": "en",
    }


def test_multiple_parent_ranks_are_not_hidden_by_truthy_preference() -> None:
    rows = [
        relation("Q10", "P31", "Q3331189"),
        relation("Q10", "P629", "Q20"),
        relation("Q10", "P629", "Q30", "PreferredRank"),
        relation("Q20", "P31", "Q8261"),
        relation("Q30", "P31", "Q8261"),
    ]
    graph = ac.parse_graph(rows, {"Q10"}, "query")
    assert graph["Q10"]["parents"] == {"Q20", "Q30"}
    catalog = {"mapping": {"11": [{"qid": "Q10", "rank": "NormalRank", "claim_id": "id"}]}}
    report = ac.build_proposals([local(11)], catalog, graph)
    assert report["proposals"][0]["reason"] == "multiple_p629_parents"
    assert report["proposals"][0]["confidence"] == "ambiguous"
    assert "p:P629/ps:P629" in ac.graph_query(["Q10"])


def test_shared_work_root_is_a_proposal_and_conflicting_manual_groups_survive() -> None:
    rows = [
        relation("Q10", "P31", "Q3331189"),
        relation("Q10", "P629", "Q20"),
        relation("Q11", "P629", "Q20"),
        relation("Q20", "P31", "Q8261"),
    ]
    graph = ac.parse_graph(rows, {"Q10", "Q11"}, "query")
    entries = [local(11), local(12, "manual-B")]
    report = ac.build_proposals(entries, {"mapping": {"11": [{"qid": "Q10"}], "12": [{"qid": "Q11"}]}}, graph)
    assert report["confidence_counts"] == {"authority_linked": 2}
    group = report["authority_groups_for_review"][0]
    assert group["conflicting_manual_groups"] and group["action"] == "review_only_no_merge"
    assert [e["work_id"] for e in entries] == ["manual-A", "manual-B"]
    assert report["verified_unique_work_count"] is None and not report["identity_count_promotion_allowed"]
    assert report["proposals"][0]["candidate"]["candidate_work"]["label"] is None


@pytest.mark.parametrize(
    "variant,reason",
    [
        ("cycle", "p629_cycle"),
        ("parts", "collection_or_component_scope_requires_review"),
        ("unknown", "root_type_unknown"),
    ],
)
def test_cycles_compound_scopes_and_unknown_types_do_not_become_confirmed_roots(variant: str, reason: str) -> None:
    rows = [relation("Q10", "P629", "Q20"), relation("Q20", "P31", "Q8261")]
    if variant == "cycle":
        rows.append(relation("Q20", "P629", "Q10"))
    if variant == "parts":
        rows.append(relation("Q20", "P527", "Q30"))
    if variant == "unknown":
        rows[-1] = relation("Q20", "P31", "Q571")
    report = ac.build_proposals([local(11)], {"mapping": {"11": [{"qid": "Q10"}]}}, ac.parse_graph(rows, {"Q10"}, "q"))
    assert report["proposals"][0]["confidence"] == "ambiguous"
    assert report["proposals"][0]["reason"] == reason


def test_deprecated_path_overfetch_is_not_used_as_a_parent_and_invalid_statement_is_rejected() -> None:
    graph = ac.parse_graph([relation("Q10", "P31", "Q3331189"), relation("Q99", "P31", "Q8261")], {"Q10"}, "q")
    assert "Q99" not in graph and not graph["Q10"]["parents"]
    bad = relation("Q10", "P629", "Q20")
    bad["statement"]["value"] = "http://www.wikidata.org/entity/statement/Q99-" + GUID
    with pytest.raises(ValueError):
        ac.parse_graph([bad], {"Q10"}, "q")
    with pytest.raises(ValueError):
        ac.graph_query(["Q1 } UNION { ?x ?y ?z"])


def test_multiple_item_and_invalid_provider_bindings_stay_uncertain() -> None:
    catalog = {"mapping": {"11": [{"qid": "Q10"}, {"qid": "Q20"}]}}
    good, bad = local(11), local(12)
    bad["source_url"] = "https://not-gutenberg.example/ebooks/12"
    report = ac.build_proposals([good, bad], catalog)
    assert report["proposals"][0]["reason"] == "multiple_P2034_items"
    assert report["proposals"][1]["reason"] == "invalid_provider_identity"
    assert report["competing_item_editions"] == 1


def test_public_selection_uses_official_rdf_and_never_opens_private_notes(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "books.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE books(id,title,author,language,work_id,source_url,artifact_sha256,status)")
        db.execute("CREATE TABLE book_metadata(book_id,metadata)")
        db.execute("CREATE TABLE saved(note)")
        db.execute("INSERT INTO saved VALUES('private-note-never-read')")
        for bid in ["pg11-full", "personal-copy"]:
            db.execute(
                "INSERT INTO books VALUES(?,?,?,?,?,?,?,?)",
                (bid, "Local", "Author", "en", "manual", "https://www.gutenberg.org/ebooks/11", "a" * 64, "indexed"),
            )
            db.execute(
                "INSERT INTO book_metadata VALUES(?,?)",
                (bid, json.dumps({"coverage": "full", "work_identity_status": "curated"})),
            )
    rdf = tmp_path / "pg11.rdf"
    rdf.write_text(
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:pg="http://www.gutenberg.org/2009/pgterms/" xmlns:dc="http://purl.org/dc/terms/">'
        '<pg:ebook rdf:about="ebooks/11"><dc:title>Official title</dc:title>'
        "<dc:rights>Public domain in the USA.</dc:rights><dc:type><rdf:Description>"
        "<rdf:value>Text</rdf:value></rdf:Description></dc:type></pg:ebook></rdf:RDF>"
    )
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    entries, selection = ac.select_public_pg_editions(database, rdf)
    assert len(entries) == 1 and entries[0]["id"] == "pg11-full"
    assert entries[0]["official_title"] == "Official title" and entries[0]["title"] == "Local"
    assert entries[0]["work_id"] == "manual" and not selection["private_notes_or_personal_imports_read"]
    assert "private-note" not in json.dumps([entries, selection])
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    ac.verify_public_selection(entries, database)
    with pytest.raises(ValueError, match="source/work/artifact changed"):
        ac.verify_public_selection([entries[0] | {"indexed_artifact_sha256": "b" * 64}], database)
    with pytest.raises(ValueError, match="Duplicate"):
        ac.verify_public_selection([entries[0], entries[0]], database)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_offline_mode_never_requests_or_creates_provider_deferral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = mock_client(monkeypatch, [])
    state = tmp_path / "common.json"
    report = ac.acquire_identifier_catalog(tmp_path / "catalog", cooldowns=(), shared_state=state, allow_network=False)
    assert not calls and report["network_requests"] == 0 and not state.exists()
    assert report["failures"][0]["reason"] == "offline_cache_miss"
    proposals = ac.build_proposals([local(11)], report)
    assert proposals["proposals"][0]["reason"] == "identifier_catalog_unavailable_or_incomplete"


def test_incomplete_snapshot_never_promotes_even_matching_graph() -> None:
    graph = ac.parse_graph([relation("Q10", "P31", "Q8261")], {"Q10"}, "q")
    report = ac.build_proposals([local(11)], {"count_consistent": False, "mapping": {"11": [{"qid": "Q10"}]}}, graph)
    assert report["confidence_counts"] == {"unresolved": 1}
    assert not report["authority_groups_for_review"]
