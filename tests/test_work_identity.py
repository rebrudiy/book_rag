from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from book_rag import work_identity as identity


def _entry(number: int = 11, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"pg{number}-full",
        "source_url": f"https://www.gutenberg.org/ebooks/{number}",
        "work_id": f"manual-{number}",
        "work_identity_status": "curated",
        "title": "Local edition title",
        "author": "Local author",
        **extra,
    }


def _uri(value: str) -> dict[str, str]:
    return {"type": "uri", "value": identity.ENTITY + value}


def _literal(value: str, language: str | None = None) -> dict[str, str]:
    return {"type": "literal", "value": value, **({"xml:lang": language} if language else {})}


def _response(rows: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"head": {"vars": []}, "results": {"bindings": rows}},
        headers={"Content-Type": "application/sparql-results+json"},
    )


@pytest.fixture
def install_endpoint(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    original_client = httpx.Client
    monkeypatch.setattr(identity, "REQUEST_INTERVAL", 0)

    def install(
        mapping: dict[str, list[str]] | None = None,
        graph: dict[str, dict[str, Any]] | None = None,
        override: Callable[[httpx.Request], httpx.Response | None] | None = None,
    ) -> list[httpx.Request]:
        requests: list[httpx.Request] = []
        mapping = mapping or {"11": ["Q101"]}
        graph = (
            graph
            if graph is not None
            else {
                "Q101": {"types": ["Q3331189"], "parents": ["Q201"], "authors": ["Q301"]},
                "Q201": {"types": ["Q7725634"], "labels": {"en": "Authority work"}, "authors": ["Q301"]},
                "Q301": {"labels": {"en": "Authority author"}},
            }
        )

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.headers["User-Agent"].startswith("BookRAG/")
            assert request.url.host == "query.wikidata.org"
            assert request.url.scheme == "https"
            if override:
                custom = override(request)
                if custom is not None:
                    return custom
            query = request.url.params["query"]
            rows = []
            if "?gutenberg ?item" in query:
                for number in re.findall(r'"([0-9]+)"', query):
                    for item in mapping.get(number, []):
                        rows.append({"gutenberg": _literal(number), "item": _uri(item)})
            else:
                nodes = re.findall(r"wd:(Q[0-9]+)", query)
                assert len(nodes) <= 50
                for node in nodes:
                    value = graph.get(node, {})
                    for field, predicate in (("types", "P31"), ("parents", "P629"), ("authors", "P50")):
                        for target in value.get(field, []):
                            rows.append(
                                {
                                    "node": _uri(node),
                                    "predicate": {"type": "uri", "value": identity.DIRECT + predicate},
                                    "value": _uri(target),
                                }
                            )
                    for language, label in value.get("labels", {}).items():
                        rows.append(
                            {
                                "node": _uri(node),
                                "predicate": {"type": "uri", "value": identity.LABEL},
                                "value": _literal(label, language),
                            }
                        )
            return _response(rows)

        monkeypatch.setattr(
            identity.httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)
        )
        return requests

    return install


def test_exact_identifier_path_and_manual_curation_preserved(
    tmp_path: Path, install_endpoint: Callable[..., Any]
) -> None:
    requests = install_endpoint()
    entry = _entry()
    before = copy.deepcopy(entry)
    report = identity.propose_work_links([entry], tmp_path)
    proposal = report["proposals"][0]
    assert entry == before
    assert proposal["work_id"] == "manual-11"
    assert proposal["work_identity_status"] == "curated"
    assert proposal["manual_curation_preserved"]
    assert proposal["confidence"] == "authority_linked"
    assert proposal["candidate_qid"] == "Q201"
    assert proposal["p2034_evidence"][0]["object"] == "11"
    candidate = proposal["candidates"][0]
    assert candidate["item"]["classification"] == "edition"
    assert candidate["path"] == ["Q101", "Q201"]
    assert candidate["p629_evidence"][0]["property"] == "P629"
    assert candidate["candidate_work"]["label"] == "Authority work"
    assert candidate["authors"][0]["label"] == "Authority author"
    assert len(requests) == report["network_requests"] == 4
    assert not report["database_modified"]
    assert not report["identity_count_promotion_allowed"]
    assert report["query_evidence"][0]["response_body_sha256"]
    assert json.loads((tmp_path / "proposals.json").read_text())["counts"] == {"authority_linked": 1}


def test_duplicates_query_once_but_preserve_every_local_entry(
    tmp_path: Path, install_endpoint: Callable[..., Any]
) -> None:
    requests = install_endpoint()
    report = identity.propose_work_links([_entry(), _entry(work_id="other-manual")], tmp_path)
    assert len(report["proposals"]) == 2
    assert report["distinct_gutenberg_editions"] == 1
    assert report["duplicate_input_ids"] == {"pg11-full": 2}
    assert [p["work_id"] for p in report["proposals"]] == ["manual-11", "other-manual"]
    assert sum("?gutenberg ?item" in r.url.params["query"] for r in requests) == 1


def test_cached_rerun_performs_no_http(tmp_path: Path, install_endpoint: Callable[..., Any]) -> None:
    requests = install_endpoint()
    first = identity.propose_work_links([_entry()], tmp_path)
    requests.clear()
    second = identity.propose_work_links([_entry()], tmp_path)
    assert not requests
    assert second["network_requests"] == 0
    assert second["cache_hits"] == first["network_requests"]
    assert second["proposals"] == first["proposals"]


def test_corrupt_cached_payload_is_not_trusted(tmp_path: Path, install_endpoint: Callable[..., Any]) -> None:
    requests = install_endpoint()
    identity.propose_work_links([_entry()], tmp_path)
    path = next(p for p in (tmp_path / "queries").glob("*.json") if "?gutenberg ?item" in p.read_text())
    cached = json.loads(path.read_text())
    cached["response"]["results"]["bindings"][0]["item"] = _uri("Q999")
    path.write_text(json.dumps(cached))
    requests.clear()
    report = identity.propose_work_links([_entry()], tmp_path)
    assert len(requests) == 1
    assert report["proposals"][0]["candidate_qid"] == "Q201"


@pytest.mark.parametrize(
    "source",
    [
        "http://www.gutenberg.org/ebooks/11",
        "https://evil.test/ebooks/11",
        "https://www.gutenberg.org:443/ebooks/11",
        "https://gutenberg.org@evil.test/ebooks/11",
        "https://www.gutenberg.org/ebooks/11?other=12",
        "https://www.gutenberg.org/ebooks/011",
    ],
)
def test_invalid_provider_does_not_query(tmp_path: Path, install_endpoint: Callable[..., Any], source: str) -> None:
    requests = install_endpoint()
    report = identity.propose_work_links([_entry(source_url=source)], tmp_path)
    assert not requests
    assert report["proposals"][0]["reason"] == "invalid_provider_identity"


def test_local_id_mismatch_does_not_query(tmp_path: Path, install_endpoint: Callable[..., Any]) -> None:
    requests = install_endpoint()
    report = identity.propose_work_links([_entry(id="pg12-full")], tmp_path)
    assert not requests
    assert report["counts"] == {"unresolved": 1}


def test_multiple_matching_authority_items_stay_ambiguous(tmp_path: Path, install_endpoint: Callable[..., Any]) -> None:
    install_endpoint(
        mapping={"11": ["Q101", "Q102"]}, graph={"Q101": {"types": ["Q7725634"]}, "Q102": {"types": ["Q7725634"]}}
    )
    proposal = identity.propose_work_links([_entry()], tmp_path)["proposals"][0]
    assert proposal["confidence"] == "ambiguous"
    assert proposal["reason"] == "multiple_p2034_items"
    assert len(proposal["candidates"]) == 2
    assert "candidate_qid" not in proposal


@pytest.mark.parametrize(
    ("graph", "reason"),
    [
        ({"Q101": {"parents": ["Q102"]}, "Q102": {"parents": ["Q101"]}}, "p629_cycle"),
        ({"Q101": {"parents": ["Q102", "Q103"]}}, "multiple_p629_parents"),
        ({"Q101": {"types": ["Q3331189"]}}, "edition_without_work_parent"),
        ({"Q101": {"types": ["Q571"]}}, "root_type_unknown"),
        (
            {
                "Q101": {"parents": ["Q102"]},
                "Q102": {"parents": ["Q103"]},
                "Q103": {"parents": ["Q104"]},
                "Q104": {"parents": ["Q105"]},
                "Q105": {"types": ["Q7725634"]},
            },
            "p629_depth_limit",
        ),
    ],
)
def test_uncertain_roots_remain_ambiguous(
    tmp_path: Path, install_endpoint: Callable[..., Any], graph: dict[str, Any], reason: str
) -> None:
    install_endpoint(graph=graph)
    proposal = identity.propose_work_links([_entry()], tmp_path)["proposals"][0]
    assert proposal["confidence"] == "ambiguous"
    assert proposal["reason"] == reason
    assert len(proposal["candidates"][0]["p629_evidence"]) <= 3


def test_unknown_labels_remain_null_and_never_copy_local_title(
    tmp_path: Path, install_endpoint: Callable[..., Any]
) -> None:
    install_endpoint(graph={"Q101": {"types": ["Q47461344"], "authors": ["Q301"]}})
    proposal = identity.propose_work_links([_entry()], tmp_path)["proposals"][0]
    assert proposal["confidence"] == "authority_linked"
    assert proposal["candidates"][0]["candidate_work"]["label"] is None
    assert proposal["candidates"][0]["authors"][0]["label"] is None
    assert proposal["title"] == "Local edition title"


def test_no_match_does_not_fuzzy_match_identical_title(tmp_path: Path, install_endpoint: Callable[..., Any]) -> None:
    install_endpoint(mapping={"12": ["Q101"]})
    proposal = identity.propose_work_links([_entry()], tmp_path)["proposals"][0]
    assert proposal["reason"] == "no_p2034_match"
    assert proposal["confidence"] == "unresolved"
    assert proposal["candidates"] == []


@pytest.mark.parametrize(
    "status,content_type,body",
    [
        (429, "text/plain", b"Rate limited"),
        (200, "text/html", b"<html>Error</html>"),
        (503, "text/plain", b"Unavailable"),
        (200, "application/json", b"not-json"),
    ],
)
def test_errors_stop_queries_and_persist_cooldown(
    tmp_path: Path, install_endpoint: Callable[..., Any], status: int, content_type: str, body: bytes
) -> None:
    requests = install_endpoint(
        override=lambda request: httpx.Response(
            status, content=body, headers={"Content-Type": content_type, "Retry-After": "120"}
        )
    )
    first = identity.propose_work_links([_entry(), _entry(12)], tmp_path)
    second = identity.propose_work_links([_entry(), _entry(12)], tmp_path)
    assert len(requests) == 1
    assert first["deferred"] and second["deferred"]
    assert second["network_requests"] == 0
    assert first["failures"][0]["automatic_retry"] is False
    assert first["counts"] == {"unresolved": 2}
    assert (tmp_path / "queries" / "deferred.json").exists()


def test_exact_literal_binding_is_verified_not_just_query_text(
    tmp_path: Path, install_endpoint: Callable[..., Any]
) -> None:
    install_endpoint(override=lambda request: _response([{"gutenberg": _literal("12"), "item": _uri("Q101")}]))
    report = identity.propose_work_links([_entry()], tmp_path)
    assert report["counts"] == {"unresolved": 1}
    assert report["proposals"][0]["p2034_evidence"] == []
    assert report["deferred"]


def test_timeout_halts_without_retry(tmp_path: Path, install_endpoint: Callable[..., Any]) -> None:
    def fail(request: httpx.Request) -> None:
        raise httpx.ReadTimeout("Bounded query timed out", request=request)

    requests = install_endpoint(override=fail)
    report = identity.propose_work_links([_entry()], tmp_path)
    assert len(requests) == 1
    assert report["deferred"]
    assert "timed out" in report["failures"][0]["error"]


def test_prototype_bound_rejects_large_unbounded_jobs(tmp_path: Path, install_endpoint: Callable[..., Any]) -> None:
    requests = install_endpoint()
    with pytest.raises(ValueError, match="at most 50"):
        identity.propose_work_links([_entry(number) for number in range(1, 52)], tmp_path)
    assert not requests


def test_shared_authority_does_not_overwrite_conflicting_manual_groups(
    tmp_path: Path, install_endpoint: Callable[..., Any]
) -> None:
    install_endpoint()
    report = identity.propose_work_links([_entry(), _entry(work_id="other-manual")], tmp_path)
    assert report["authority_groups"] == [
        {
            "candidate_qid": "Q201",
            "local_edition_ids": ["pg11-full", "pg11-full"],
            "existing_work_ids": ["manual-11", "other-manual"],
            "manual_work_ids": ["manual-11", "other-manual"],
            "multiple_manual_groups": True,
            "action": "review_only_no_merge",
        }
    ]


def test_extreme_retry_after_still_defers_without_datetime_overflow(
    tmp_path: Path, install_endpoint: Callable[..., Any]
) -> None:
    requests = install_endpoint(override=lambda request: httpx.Response(429, headers={"Retry-After": "1e100"}))
    report = identity.propose_work_links([_entry()], tmp_path)
    assert report["deferred"]
    assert len(requests) == 1
    assert report["failures"][0]["retry_at_epoch"] >= 1e100
    assert report["failures"][0]["retry_at"] is None


def test_unknown_entity_metadata_response_rejected(tmp_path: Path, install_endpoint: Callable[..., Any]) -> None:
    def invalid(request: httpx.Request) -> httpx.Response | None:
        if "?node ?predicate ?value" in request.url.params["query"]:
            return _response(
                [
                    {
                        "node": _uri("Q999"),
                        "predicate": {"type": "uri", "value": identity.DIRECT + "P31"},
                        "value": _uri("Q7725634"),
                    }
                ]
            )
        return None

    requests = install_endpoint(override=invalid)
    report = identity.propose_work_links([_entry()], tmp_path)
    assert len(requests) == 2
    assert report["counts"] == {"ambiguous": 1}
    assert report["proposals"][0]["reason"] == "authority_metadata_unavailable"


def test_large_authority_response_fails_before_parsing(
    tmp_path: Path, install_endpoint: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(identity, "MAX_BYTES", 50)
    requests = install_endpoint(
        override=lambda request: httpx.Response(
            200, content=b"x" * 100, headers={"Content-Type": "application/sparql-results+json"}
        )
    )
    report = identity.propose_work_links([_entry()], tmp_path)
    assert len(requests) == 1
    assert report["deferred"]
    assert "byte limit" in report["failures"][0]["error"]


def test_missing_cache_provenance_causes_safe_refetch(tmp_path: Path, install_endpoint: Callable[..., Any]) -> None:
    requests = install_endpoint()
    identity.propose_work_links([_entry()], tmp_path)
    path = next(p for p in (tmp_path / "queries").glob("*.json") if "?gutenberg ?item" in p.read_text())
    cached = json.loads(path.read_text())
    del cached["provenance"]
    path.write_text(json.dumps(cached))
    requests.clear()
    report = identity.propose_work_links([_entry()], tmp_path)
    assert len(requests) == 1
    assert report["counts"] == {"authority_linked": 1}


def test_row_bound_cannot_silently_promote_truncated_matches(
    tmp_path: Path, install_endpoint: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(identity, "MAX_ROWS", 1)
    install_endpoint(mapping={"11": ["Q101", "Q102"]})
    report = identity.propose_work_links([_entry()], tmp_path)
    assert report["counts"] == {"unresolved": 1}
    assert report["deferred"]
    assert "row bound" in report["failures"][0]["error"]


def test_author_metadata_cannot_exceed_total_node_bound(
    tmp_path: Path, install_endpoint: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(identity, "MAX_NODES", 3)
    requests = install_endpoint(
        graph={
            "Q101": {"types": ["Q3331189"], "parents": ["Q201"]},
            "Q201": {"types": ["Q7725634"], "authors": ["Q301", "Q302", "Q303"]},
        }
    )
    report = identity.propose_work_links([_entry()], tmp_path)
    queried = {node for request in requests for node in re.findall(r"wd:(Q[0-9]+)", request.url.params["query"])}
    assert len(queried) == 3
    assert report["counts"] == {"authority_linked": 1}
    assert all(author["label"] is None for author in report["proposals"][0]["candidates"][0]["authors"])


def test_damaged_host_cooldown_does_not_provoke_new_requests(
    tmp_path: Path, install_endpoint: Callable[..., Any]
) -> None:
    requests = install_endpoint()
    directory = tmp_path / "queries"
    directory.mkdir()
    (directory / "deferred.json").write_text(json.dumps({"retry_at_epoch": "damaged"}))
    report = identity.propose_work_links([_entry()], tmp_path)
    assert not requests
    assert report["deferred"]
    assert report["counts"] == {"unresolved": 1}
