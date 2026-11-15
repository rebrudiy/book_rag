"""Compact Wikidata CC0 identifier snapshots and review-only local work proposals.

No production book, work ID, epoch, semantic receipt or manual link is written.
The global identifier map is fetched once, then matched locally by exact PG ID.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from book_rag import work_identity as legacy

ENDPOINT = legacy.ENDPOINT
LICENSE_URL = "https://www.wikidata.org/wiki/Wikidata:Licensing"
MAX_TOTAL_BYTES = 6 * 1024**2  # Global snapshot + 3 MiB graph pilot stays below 10 MB total.
MAX_REQUESTS = 4
MAX_MAPPING_ROWS = 100000
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHARED_STATE = PROJECT_ROOT / "data/identity/wikidata-access-state.json"
DEFAULT_COOLDOWNS = (
    PROJECT_ROOT / "data/identity/wikidata-200-v1/queries/deferred.json",
    PROJECT_ROOT / "data/identity/wikidata-50/queries/deferred.json",
    SHARED_STATE,
)
PREFIXES = """PREFIX p: <http://www.wikidata.org/prop/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX wikibase: <http://wikiba.se/ontology#>
"""
PATTERN = """?entity p:P2034 ?statement .
?statement ps:P2034 ?identifier ; wikibase:rank ?statementRank .
FILTER(?statementRank != wikibase:DeprecatedRank)
"""
COUNT_QUERY = PREFIXES + "SELECT (COUNT(*) AS ?assertions) WHERE {\n" + PATTERN + "}\n"
CATALOG_QUERY = (
    PREFIXES
    + """SELECT
(STR(?identifier) AS ?pg)
(STRAFTER(STR(?entity), "/entity/") AS ?item)
(STRAFTER(STR(?statement), "/statement/") AS ?claim)
(STRAFTER(STR(?statementRank), "#") AS ?rank)
WHERE {
"""
    + PATTERN
    + "}\n"
)


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    if path.is_symlink() or temporary.is_symlink():
        raise ValueError("Authority output/cache paths must not be symlinks")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


class ProviderDeferred(ValueError):
    def __init__(self, retry_at: float) -> None:
        self.retry_at = retry_at
        super().__init__("Authority provider deferred until " + str(legacy._iso_timestamp(retry_at)))


def check_cooldowns(paths: tuple[Path, ...]) -> None:
    """A new cache destination cannot bypass old or common provider deferrals."""
    latest = 0.0
    for path in paths:
        if not path.is_file():
            continue
        state = json.loads(path.read_text())
        until = state.get("retry_at_epoch") if isinstance(state, dict) else None
        if not isinstance(until, (int, float)) or not math.isfinite(until):
            raise ValueError("Malformed persisted authority provider cooldown: " + str(path))
        latest = max(latest, until)
    if latest > time.time():
        raise ProviderDeferred(latest)


class SnapshotClient:
    """Four serial bounded requests at most; raw bytes and exact query retained."""

    def __init__(
        self,
        directory: Path,
        cooldowns: tuple[Path, ...] = DEFAULT_COOLDOWNS,
        *,
        shared_state: Path = SHARED_STATE,
        max_requests: int = MAX_REQUESTS,
        allow_network: bool = True,
    ) -> None:
        if not 1 <= max_requests <= MAX_REQUESTS:
            raise ValueError("Authority request budget must be 1–4")
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.common = shared_state
        self.cooldowns = tuple(dict.fromkeys((*cooldowns, self.common, directory / "deferred.json")))
        self.requests = self.cache_hits = self.response_bytes = 0
        self.request_limit = max_requests
        self.allow_network = allow_network
        self.next_request = time.monotonic() + 2
        self.deadline = time.monotonic() + 120
        self.stopped = False
        self.evidence: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.client = httpx.Client(
            headers={"User-Agent": legacy.USER_AGENT, "Accept-Encoding": "gzip"},
            timeout=httpx.Timeout(25, connect=10),
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def fetch(self, query: str, media: str, extension: str, *, max_bytes: int) -> bytes | None:
        identity = hashlib.sha256((media + "\n" + query).encode()).hexdigest()
        body_path = self.directory / (identity + ".response." + extension)
        receipt_path = self.directory / (identity + ".receipt.json")
        if body_path.is_symlink() or receipt_path.is_symlink():
            raise ValueError("Authority cache paths must not be symlinks")
        if (
            body_path.is_file()
            and receipt_path.is_file()
            and not body_path.is_symlink()
            and not receipt_path.is_symlink()
        ):
            try:
                receipt = json.loads(receipt_path.read_text())
                if body_path.stat().st_size <= max_bytes:
                    body = body_path.read_bytes()
                    if (
                        receipt.get("query") == query
                        and receipt.get("endpoint") == ENDPOINT
                        and receipt.get("media_type") == media
                        and receipt.get("license") == "CC0-1.0"
                        and isinstance(receipt.get("retrieved_at"), str)
                        and receipt.get("response_sha256") == hashlib.sha256(body).hexdigest()
                    ):
                        self.cache_hits += 1
                        self.evidence.append(receipt | {"cache": True})
                        return body
            except (ValueError, OSError, AttributeError):
                pass
        if self.stopped:
            return None
        if not self.allow_network:
            self.failures.append(
                {"reason": "offline_cache_miss", "query_sha256": hashlib.sha256(query.encode()).hexdigest()}
            )
            self.stopped = True
            return None
        response = None
        try:
            check_cooldowns(self.cooldowns)
            remaining = self.deadline - time.monotonic()
            if self.requests >= self.request_limit or remaining <= 0 or self.response_bytes >= MAX_TOTAL_BYTES:
                self.failures.append({"reason": "bounded_request_byte_or_time_budget_exhausted"})
                self.stopped = True
                return None
            if self.next_request > time.monotonic():
                time.sleep(self.next_request - time.monotonic())
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                self.failures.append({"reason": "bounded_request_byte_or_time_budget_exhausted"})
                self.stopped = True
                return None
            self.requests += 1
            limit = min(max_bytes, MAX_TOTAL_BYTES - self.response_bytes)
            with self.client.stream(
                "GET",
                ENDPOINT,
                params={"query": query},
                headers={"Accept": media},
                timeout=httpx.Timeout(min(25, remaining), connect=min(10, remaining)),
            ) as response:
                if response.status_code != 200:
                    raise ValueError(f"Authority endpoint HTTP {response.status_code}")
                actual_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                accepted = {media} | ({"application/json"} if media == "application/sparql-results+json" else set())
                if actual_type not in accepted:
                    raise ValueError("Authority endpoint returned unexpected media/HTML instead of metadata")
                buffer = bytearray()
                for part in response.iter_bytes():
                    if len(buffer) + len(part) > limit or time.monotonic() >= self.deadline:
                        raise ValueError("Authority response byte/deadline bound reached")
                    buffer.extend(part)
            body = bytes(buffer)
            if media == "application/sparql-results+json":
                legacy._bindings(json.loads(body))
            elif media == "text/tab-separated-values":
                parse_catalog(body)
            receipt = {
                "query": query,
                "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
                "endpoint": ENDPOINT,
                "media_type": media,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "response_sha256": hashlib.sha256(body).hexdigest(),
                "response_bytes": len(body),
                "license": "CC0-1.0",
                "license_url": LICENSE_URL,
            }
            temporary = body_path.with_suffix(body_path.suffix + ".part")
            if temporary.is_symlink() or body_path.is_symlink():
                raise ValueError("Authority response paths must not be symlinks")
            temporary.write_bytes(body)
            temporary.replace(body_path)
            _save(receipt_path, receipt)
            self.response_bytes += len(body)
            self.evidence.append(receipt | {"cache": False})
            return body
        except (httpx.HTTPError, ValueError, csv.Error) as error:
            retry_at = (
                error.retry_at
                if isinstance(error, ProviderDeferred)
                else legacy._retry_at(response.headers.get("retry-after") if response is not None else None)
            )
            failure = {
                "endpoint": ENDPOINT,
                "error": str(error),
                "status": response.status_code if response is not None else None,
                "retry_at_epoch": retry_at,
                "retry_at": legacy._iso_timestamp(retry_at),
                "automatic_retry": False,
                "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
            }
            self.failures.append(failure)
            self.stopped = True
            # Never shorten a prior common deferral when another method fails.
            if self.common.is_file():
                previous = json.loads(self.common.read_text())
                if isinstance(previous, dict) and previous.get("retry_at_epoch", 0) > retry_at:
                    failure = previous
            _save(self.directory / "deferred.json", failure)
            _save(self.common, failure)
            return None
        finally:
            self.next_request = time.monotonic() + 2


def parse_catalog(body: bytes) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, Any]], int]:
    """Strict compact TSV: preserve all competing normal/preferred statements."""
    text = body.decode("utf-8-sig")
    if not text.endswith("\n"):
        raise ValueError("Incomplete identifier TSV response")
    rows = csv.reader(io.StringIO(text), delimiter="\t", strict=True)
    if next(rows, None) != ["?pg", "?item", "?claim", "?rank"]:
        raise ValueError("Unexpected identifier TSV header")
    mapping: dict[str, list[dict[str, str]]] = {}
    invalid = []
    seen: set[tuple[str, ...]] = set()
    count = 0
    for row in rows:
        count += 1
        if count > MAX_MAPPING_ROWS or len(row) != 4:
            raise ValueError("Identifier row bound/shape exceeded")
        pg, qid, claim, rank = row
        if not (
            re.fullmatch(r"[1-9]\d{0,8}", pg)
            and re.fullmatch(r"Q[1-9]\d*", qid)
            and re.fullmatch(re.escape(qid) + r"-[A-Fa-f0-9-]{32,36}", claim)
            and rank in {"NormalRank", "PreferredRank"}
        ):
            invalid.append({"row": count, "values": row, "reason": "invalid_identifier_item_statement_or_rank"})
            continue
        key = tuple(row)
        if key in seen:
            invalid.append({"row": count, "values": row, "reason": "duplicate_statement_row"})
            continue
        seen.add(key)
        mapping.setdefault(pg, []).append({"qid": qid, "claim_id": claim, "rank": rank, "property": "P2034"})
    return mapping, invalid, count


def acquire_identifier_catalog(
    directory: Path,
    *,
    cooldowns: tuple[Path, ...] = DEFAULT_COOLDOWNS,
    shared_state: Path = SHARED_STATE,
    allow_network: bool = True,
) -> dict[str, Any]:
    """Count then fetch one compact map, stopping rather than bypassing failure."""
    client = SnapshotClient(directory / "queries", cooldowns, shared_state=shared_state, allow_network=allow_network)
    mapping: dict[str, list[dict[str, str]]] = {}
    invalid = []
    expected = observed = None
    try:
        raw = client.fetch(COUNT_QUERY, "application/sparql-results+json", "json", max_bytes=65536)
        if raw is not None:
            bindings = legacy._bindings(json.loads(raw))
            if len(bindings) != 1 or not re.fullmatch(r"\d+", legacy._value(bindings[0], "assertions", "literal")):
                raise ValueError("Invalid property cardinality response")
            expected = int(legacy._value(bindings[0], "assertions", "literal"))
            if 0 <= expected <= MAX_MAPPING_ROWS:
                raw = client.fetch(CATALOG_QUERY, "text/tab-separated-values", "tsv", max_bytes=MAX_TOTAL_BYTES - 65536)
                if raw is not None:
                    mapping, invalid, observed = parse_catalog(raw)
        report = {
            "schema_version": 1,
            "recorded_at": datetime.now(UTC).isoformat(),
            "endpoint": ENDPOINT,
            "license": "CC0-1.0",
            "expected_non_deprecated_statements": expected,
            "observed_statement_rows": observed,
            "count_consistent": expected is not None and expected == observed,
            "distinct_gutenberg_ids": len(mapping),
            "mapped_statements": sum(len(v) for v in mapping.values()),
            "multiple_item_gutenberg_ids": sum(len({s["qid"] for s in values}) > 1 for values in mapping.values()),
            "mapping": mapping,
            "invalid_assertions": invalid,
            "query_evidence": client.evidence,
            "network_requests": client.requests,
            "cache_hits": client.cache_hits,
            "response_bytes": client.response_bytes,
            "failures": client.failures,
            "deferred": client.stopped,
            "database_modified": False,
            "identity_count_promotion_allowed": False,
            "scope": (
                "Non-deprecated P2034 statements on the main public endpoint; "
                "missing matches do not prove absence of a Wikidata work"
            ),
        }
        _save(directory / "identifier-catalog.json", report)
        return report
    finally:
        client.close()


def select_public_pg_editions(database: Path, rdf_catalog: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bind read-only indexed PG IDs to official local RDF, without private inputs."""
    import sqlite3
    from contextlib import closing

    from book_rag.catalog import _work_type
    from book_rag.corpus_planning import _text_rows

    local = {}
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute(
            "SELECT b.id,b.title,b.author,b.language,b.work_id,b.source_url,b.artifact_sha256,m.metadata "
            "FROM books b JOIN book_metadata m ON m.book_id=b.id WHERE b.status='indexed' AND b.id GLOB 'pg*-full'"
        ):
            if not re.fullmatch(r"pg[1-9]\d{0,8}-full", row["id"]):
                continue
            try:
                number = legacy._number(dict(row))
            except ValueError:
                continue
            metadata = json.loads(row["metadata"])
            local[number] = {
                "id": row["id"],
                "gutenberg_id": number,
                "title": row["title"],
                "author": row["author"],
                "language": row["language"],
                "work_id": row["work_id"],
                "source_url": row["source_url"],
                "indexed_artifact_sha256": row["artifact_sha256"],
                "work_identity_status": metadata.get("work_identity_status", "unknown"),
                "coverage": metadata.get("coverage", "unknown"),
            }
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    for row in _text_rows(rdf_catalog):
        scanned += 1
        number = row["number"]
        if number not in local:
            continue
        if row["type"] != "Text" or row["rights"].strip().rstrip(".").casefold() != "public domain in the usa":
            continue
        if number in found:
            raise ValueError("Duplicate same-ID records in the public RDF snapshot")
        found[number] = local[number] | {
            "official_title": row["title"],
            "official_author": row["author"],
            "official_rights": row["rights"],
            "work_type": _work_type(row),
            "catalog_record_sha256": row["catalog_record_sha256"],
            "catalog_member": row["catalog_member"],
        }
    with rdf_catalog.open("rb") as stream:
        catalog_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    entries = [found[number] for number in sorted(found, key=int)]
    return entries, {
        "indexed_strict_pg_candidates": len(local),
        "public_rdf_matched_editions": len(entries),
        "missing_or_rights_type_rejected_ids": sorted(set(local) - found.keys(), key=int),
        "rdf_records_scanned": scanned,
        "catalog_path": str(rdf_catalog),
        "catalog_sha256": catalog_sha,
        "private_notes_or_personal_imports_read": False,
        "database_modified": False,
    }


def build_proposals(
    entries: list[dict[str, Any]],
    catalog: dict[str, Any],
    metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Exact-ID proposals only; curated groups survive, unknown scope stays explicit."""
    from collections import Counter

    from book_rag.authority_audit import classify

    metadata = metadata or {}
    evidence = next((e for e in catalog.get("query_evidence", []) if e.get("query") == CATALOG_QUERY), {})
    proposals = []
    for entry in entries:
        proposal: dict[str, Any] = {
            "id": entry.get("id"),
            "title": entry.get("title"),
            "author": entry.get("author"),
            "work_id": entry.get("work_id"),
            "work_identity_status": entry.get("work_identity_status"),
            "manual_curation_preserved": entry.get("work_identity_status") == "curated",
            "source_scope_verified": False,
            "action": "review_only_no_merge",
        }
        try:
            number = legacy._number(entry)
        except ValueError as error:
            proposal.update(confidence="unresolved", reason="invalid_provider_identity", error=str(error))
            proposals.append(proposal)
            continue
        complete = catalog.get("count_consistent", True)
        assertions = catalog.get("mapping", {}).get(number, []) if complete else []
        qids = sorted({assertion["qid"] for assertion in assertions})
        proposal.update(
            gutenberg_id=number,
            authority_item_ids=qids,
            p2034_evidence=[
                assertion
                | {
                    "object": number,
                    "query_sha256": evidence.get("query_sha256"),
                    "response_sha256": evidence.get("response_sha256"),
                    "retrieved_at": evidence.get("retrieved_at"),
                }
                for assertion in assertions
            ],
        )
        if not complete:
            proposal.update(confidence="unresolved", reason="identifier_catalog_unavailable_or_incomplete")
        elif not qids:
            proposal.update(confidence="unresolved", reason="no_exact_P2034_claim_in_observed_snapshot")
        elif len(qids) > 1:
            proposal.update(confidence="ambiguous", reason="multiple_P2034_items")
        elif qids[0] not in metadata:
            proposal.update(confidence="ambiguous", reason="work_graph_not_in_bounded_pilot")
        else:
            candidate = classify(qids[0], metadata)
            if entry.get("work_type") == "periodical_issue":
                candidate.update(confidence="ambiguous", reason="local_periodical_scope_requires_review")
            proposal.update(
                confidence=candidate["confidence"],
                reason=candidate["reason"],
                candidate=candidate,
                candidate_work_qid=candidate["candidate_work"]["qid"],
            )
        proposals.append(proposal)
    groups: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        if proposal["confidence"] != "authority_linked":
            continue
        group = groups.setdefault(
            proposal["candidate_work_qid"],
            {
                "candidate_work_qid": proposal["candidate_work_qid"],
                "local_edition_ids": [],
                "existing_work_ids": [],
                "manual_work_ids": [],
                "action": "review_only_no_merge",
            },
        )
        group["local_edition_ids"].append(proposal["id"])
        if proposal["work_id"] not in group["existing_work_ids"]:
            group["existing_work_ids"].append(proposal["work_id"])
        if proposal["manual_curation_preserved"] and proposal["work_id"] not in group["manual_work_ids"]:
            group["manual_work_ids"].append(proposal["work_id"])
    for group in groups.values():
        group["conflicting_manual_groups"] = len(group["manual_work_ids"]) > 1
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "input_editions": len(entries),
        "exact_identifier_matched_editions": sum(bool(p.get("authority_item_ids")) for p in proposals),
        "confidence_counts": dict(Counter(p["confidence"] for p in proposals)),
        "reason_counts": dict(Counter(p["reason"] for p in proposals)),
        "competing_item_editions": sum(len(p.get("authority_item_ids", [])) > 1 for p in proposals),
        "authority_groups_for_review": list(groups.values()),
        "proposals": proposals,
        "database_modified": False,
        "curated_links_modified": False,
        "identity_count_promotion_allowed": False,
        "verified_unique_work_count": None,
        "license": "CC0-1.0",
        "scope": (
            "Exact authority assertions and bounded work graphs are proposals; "
            "missing identifiers or unknown types do not establish uniqueness"
        ),
    }


def verify_public_selection(entries: list[dict[str, Any]], database: Path) -> None:
    """Reject stale cached local guards before deriving proposals, without writes."""
    import sqlite3
    from contextlib import closing

    seen: set[str] = set()
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        for entry in entries:
            number = legacy._number(entry)
            if number in seen:
                raise ValueError("Duplicate Gutenberg IDs in cached public selection")
            seen.add(number)
            row = db.execute(
                "SELECT b.title,b.author,b.language,b.work_id,b.source_url,b.artifact_sha256,m.metadata "
                "FROM books b JOIN book_metadata m ON m.book_id=b.id WHERE b.id=? AND b.status='indexed'",
                (entry["id"],),
            ).fetchone()
            expected = tuple(
                entry[key]
                for key in ("title", "author", "language", "work_id", "source_url", "indexed_artifact_sha256")
            )
            if row is None or row[:6] != expected:
                raise ValueError("Cached public edition source/work/artifact changed: " + entry["id"])
            metadata = json.loads(row[6])
            if (
                metadata.get("work_identity_status", "unknown") != entry["work_identity_status"]
                or metadata.get("coverage", "unknown") != entry["coverage"]
            ):
                raise ValueError("Cached public edition scope/curation changed: " + entry["id"])


GRAPH_PROPERTIES = {
    "P31": "types",
    "P629": "parents",
    "P50": "authors",
    "P136": "genres",
    "P527": "has_parts",
    "P361": "part_of",
}


def graph_query(seeds: list[str]) -> str:
    if not seeds or len(set(seeds)) > 40 or any(not re.fullmatch(r"Q[1-9]\d*", q) for q in seeds):
        raise ValueError("Work-graph pilot requires 1–40 validated QIDs")
    values = " ".join("wd:" + q for q in sorted(set(seeds)))
    predicates = " ".join(f"(p:{p} ps:{p} wdt:{p})" for p in GRAPH_PROPERTIES)
    return (
        PREFIXES
        + """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?node ?predicate ?value ?statement ?rank WHERE {
 VALUES ?seed { """
        + values
        + """ }
 ?seed (p:P629/ps:P629)?/(p:P629/ps:P629)?/(p:P629/ps:P629)? ?node .
 {
  { VALUES (?claimProperty ?valueProperty ?predicate) { """
        + predicates
        + """ }
    ?node ?claimProperty ?statement . ?statement ?valueProperty ?value ; wikibase:rank ?rank .
    FILTER(?rank != wikibase:DeprecatedRank)
  } UNION {
    ?node rdfs:label ?value . BIND(rdfs:label AS ?predicate)
    FILTER(LANG(?value) IN ("en","ru","de","fr"))
  }
 }
} LIMIT 5001
"""
    )


def parse_graph(rows: list[dict[str, Any]], seeds: set[str], query_sha256: str) -> dict[str, dict[str, Any]]:
    """All non-deprecated parent assertions survive; cycle/part/type decisions are separate."""
    graph: dict[str, dict[str, Any]] = {}
    for row in rows:
        node = legacy._qid(legacy._value(row, "node", "uri"))
        predicate = legacy._value(row, "predicate", "uri")
        if node is None:
            raise ValueError("Invalid work-graph subject")
        data = graph.setdefault(
            node,
            {
                **{name: set() for name in GRAPH_PROPERTIES.values()},
                "labels": {},
                "query_sha256": query_sha256,
                "statements": [],
            },
        )
        if predicate == legacy.LABEL:
            value = legacy._value(row, "value", "literal")
            language = row["value"].get("xml:lang")
            if language not in legacy.LANGUAGES:
                raise ValueError("Unexpected work-graph label language")
            data["labels"][language] = value
            continue
        property_id = predicate.removeprefix(legacy.DIRECT)
        target = legacy._qid(legacy._value(row, "value", "uri"))
        rank = legacy._value(row, "rank", "uri")
        statement = legacy._value(row, "statement", "uri")
        if (
            property_id not in GRAPH_PROPERTIES
            or target is None
            or rank
            not in {
                "http://wikiba.se/ontology#NormalRank",
                "http://wikiba.se/ontology#PreferredRank",
            }
            or not statement.startswith("http://www.wikidata.org/entity/statement/" + node + "-")
        ):
            raise ValueError("Unexpected authority predicate, rank or statement identity")
        data[GRAPH_PROPERTIES[property_id]].add(target)
        data["statements"].append(
            {"subject": node, "property": property_id, "object": target, "rank": rank, "statement_uri": statement}
        )
    reachable = set(seeds)
    for _ in range(3):
        reachable |= {parent for q in list(reachable) for parent in graph.get(q, {}).get("parents", [])}
    # The traversal may overfetch nodes reached only through deprecated edges;
    # those edges never enter our non-deprecated graph or candidate path.
    return {qid: data for qid, data in graph.items() if qid in reachable}


def choose_graph_pilot(entries: list[dict[str, Any]], catalog: dict[str, Any], limit: int = 40) -> list[str]:
    from collections import defaultdict

    if not 1 <= limit <= 40:
        raise ValueError("Graph pilot cannot exceed forty seed entities")
    eligible: dict[str, list[dict[str, Any]]] = defaultdict(list)
    curated = []
    for entry in entries:
        qids = {a["qid"] for a in catalog.get("mapping", {}).get(entry["gutenberg_id"], [])}
        if len(qids) != 1 or entry.get("work_type") == "periodical_issue":
            continue
        value = entry | {"qid": next(iter(qids))}
        if entry.get("work_identity_status") == "curated":
            curated.append(value)
        else:
            eligible[entry["language"]].append(value)
    selected = []
    for entry in sorted(curated, key=lambda e: int(e["gutenberg_id"])):
        if entry["qid"] not in selected and len(selected) < limit:
            selected.append(entry["qid"])
    for values in eligible.values():
        values.sort(key=lambda e: hashlib.sha256(("authority-graph-v1:" + e["id"]).encode()).hexdigest())
    while len(selected) < limit and any(eligible.values()):
        for language in sorted(eligible):
            if eligible[language] and len(selected) < limit:
                item = eligible[language].pop(0)
                if item["qid"] not in selected:
                    selected.append(item["qid"])
    return selected


def acquire_graph_pilot(
    seeds: list[str],
    directory: Path,
    *,
    cooldowns: tuple[Path, ...] = DEFAULT_COOLDOWNS,
    shared_state: Path = SHARED_STATE,
    allow_network: bool = True,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Two additional requests maximum after the two-request global map."""
    query = graph_query(seeds)
    client = SnapshotClient(
        directory / "queries", cooldowns, shared_state=shared_state, max_requests=2, allow_network=allow_network
    )
    graph: dict[str, dict[str, Any]] = {}
    try:
        raw = client.fetch(query, "application/sparql-results+json", "json", max_bytes=2 * 1024**2)
        if raw is not None:
            graph = parse_graph(
                legacy._bindings(json.loads(raw)), set(seeds), hashlib.sha256(query.encode()).hexdigest()
            )
        authors = sorted({author for node in graph.values() for author in node["authors"]} - graph.keys())
        if authors:
            author_values = " ".join("wd:" + q for q in authors[:200])
            author_query = (
                """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?node ?predicate ?value WHERE { VALUES ?node { """
                + author_values
                + """ }
?node rdfs:label ?value . BIND(rdfs:label AS ?predicate)
FILTER(LANG(?value) IN ("en","ru","de","fr")) } LIMIT 5001
"""
            )
            raw = client.fetch(author_query, "application/sparql-results+json", "json", max_bytes=1024**2)
            if raw is not None:
                graph.update(
                    legacy._metadata(
                        legacy._bindings(json.loads(raw)),
                        set(authors[:200]),
                        hashlib.sha256(author_query.encode()).hexdigest(),
                    )
                )
        evidence = {
            "seed_qids": seeds,
            "max_p629_edges": 3,
            "metadata_nodes": len(graph),
            "network_requests": client.requests,
            "cache_hits": client.cache_hits,
            "response_bytes": client.response_bytes,
            "failures": client.failures,
            "deferred": client.stopped,
            "query_evidence": client.evidence,
            "author_labels_not_fetched": authors[200:],
            "database_modified": False,
            "identity_count_promotion_allowed": False,
        }
        _save(directory / "graph-pilot-evidence.json", evidence)
        serializable = {
            qid: {k: sorted(v) if isinstance(v, set) else v for k, v in data.items()} for qid, data in graph.items()
        }
        _save(directory / "graph-pilot.json", serializable)
        return graph, evidence
    finally:
        client.close()
