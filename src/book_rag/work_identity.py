"""Read-only, evidence-based edition/work proposals from Wikidata CC0 data.

Only an exact Gutenberg P2034 identifier binds an input to an authority item.
P629 links are followed for at most three edges; titles never merge records.
Successful query snapshots are immutable caches until explicitly removed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "BookRAG/0.1 (https://github.com/rebrudiy/book_rag; public book metadata research)"
ENTITY = "http://www.wikidata.org/entity/"
DIRECT = "http://www.wikidata.org/prop/direct/"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
EDITION_TYPES = {"Q3331189"}  # version, edition or translation
WORK_TYPES = {"Q7725634", "Q47461344", "Q8261"}  # literary work, written work, novel
MAX_EDITIONS = 50
MAX_NODES = 300
MAX_DEPTH = 3
MAX_ROWS = 5000
MAX_BYTES = 2 * 1024 * 1024
REQUEST_INTERVAL = 2.0
LANGUAGES = ("en", "ru", "fr", "de")


def _write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _number(entry: dict[str, Any]) -> str:
    parsed = urlparse(str(entry.get("source_url", "")))
    match = re.fullmatch(r"/ebooks/([1-9][0-9]{0,8})/?", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"www.gutenberg.org", "gutenberg.org"}
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        raise ValueError("Only an official HTTPS Gutenberg ebook source URL is supported")
    number = match.group(1)
    stable_id = str(entry.get("id", ""))
    if not stable_id:
        raise ValueError("A stable local edition ID is required")
    id_number = re.fullmatch(r"pg([0-9]+)(?:[-_.].*)?", stable_id)
    if id_number and id_number.group(1) != number:
        raise ValueError("Local edition ID and Gutenberg source URL disagree")
    return number


def _qid(value: str) -> str | None:
    if value.startswith(ENTITY) and re.fullmatch(r"Q[1-9][0-9]*", value[len(ENTITY) :]):
        return value[len(ENTITY) :]
    return None


def _bindings(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("results"), dict):
        raise ValueError("Endpoint did not return SPARQL results JSON")
    rows = data["results"].get("bindings")
    if not isinstance(rows, list) or len(rows) >= MAX_ROWS + 1:
        raise ValueError("SPARQL result missing or exceeds complete-result row bound")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Malformed SPARQL binding row")
    return rows


def _value(row: dict[str, Any], key: str, kind: str | None = None) -> str:
    binding = row.get(key)
    if not isinstance(binding, dict) or (kind is not None and binding.get("type") != kind):
        raise ValueError(f"Invalid SPARQL binding: {key}")
    value = binding.get("value")
    if not isinstance(value, str):
        raise ValueError(f"Missing SPARQL binding: {key}")
    return value


def _iso_timestamp(value: float) -> str | None:
    try:
        return datetime.fromtimestamp(value, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _retry_at(header: str | None) -> float:
    now = time.time()
    if header:
        try:
            seconds = float(header)
            if math.isfinite(seconds) and seconds >= 0:
                return now + max(60.0, seconds)
        except ValueError:
            try:
                date = parsedate_to_datetime(header)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                return max(now + 60, date.timestamp())
            except (ValueError, TypeError, OverflowError):
                pass
    return now + 3600


@dataclass(slots=True)
class _Queries:
    directory: Path
    client: httpx.Client
    requests: int = 0
    cache_hits: int = 0
    last_request: float = 0
    stopped: bool = False
    evidence: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def fetch(self, query: str) -> tuple[list[dict[str, Any]] | None, str]:
        digest = hashlib.sha256(query.encode()).hexdigest()
        path = self.directory / f"{digest}.json"
        cached = _read(path)
        if cached and cached.get("query") == query and cached.get("endpoint") == ENDPOINT:
            payload = cached.get("response")
            raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            provenance = cached.get("provenance")
            if (
                hashlib.sha256(raw).hexdigest() == cached.get("response_sha256")
                and isinstance(provenance, dict)
                and provenance.get("endpoint") == ENDPOINT
                and provenance.get("license") == "CC0-1.0"
                and isinstance(provenance.get("retrieved_at"), str)
                and re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("response_body_sha256", "")))
            ):
                try:
                    rows = _bindings(payload)
                except ValueError:
                    pass
                else:
                    self.cache_hits += 1
                    self.evidence.append({"query_sha256": digest, "cache": True, **provenance})
                    return rows, digest
        if self.stopped:
            return None, digest
        cooldown = _read(self.directory / "deferred.json")
        cooldown_until = cooldown.get("retry_at_epoch", 0) if cooldown else 0
        if not isinstance(cooldown_until, (int, float)) or not math.isfinite(cooldown_until):
            cooldown_until = math.inf  # Damaged cooldown must not trigger requests.
        if cooldown and cooldown_until > time.time():
            self.stopped = True
            self.failures.append(cooldown)
            return None, digest
        pause = REQUEST_INTERVAL - (time.monotonic() - self.last_request)
        if pause > 0:
            time.sleep(pause)
        self.last_request = time.monotonic()
        self.requests += 1
        response = None
        try:
            with self.client.stream("GET", ENDPOINT, params={"query": query, "format": "json"}) as response:
                if response.status_code != 200:
                    raise ValueError(f"Wikidata endpoint returned HTTP {response.status_code}")
                media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if media not in {"application/sparql-results+json", "application/json"}:
                    raise ValueError("Wikidata returned non-JSON content (possibly an HTML error page)")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise ValueError("Wikidata metadata response exceeds the byte limit")
                payload = json.loads(body)
                rows = _bindings(payload)
            provenance = {
                "retrieved_at": datetime.now(UTC).isoformat(),
                "endpoint": ENDPOINT,
                "response_body_sha256": hashlib.sha256(body).hexdigest(),
                "license": "CC0-1.0",
            }
            canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            _write(
                path,
                {
                    "query": query,
                    "endpoint": ENDPOINT,
                    "response": payload,
                    "response_sha256": hashlib.sha256(canonical).hexdigest(),
                    "provenance": provenance,
                },
            )
            self.evidence.append({"query_sha256": digest, "cache": False, **provenance})
            return rows, digest
        except (httpx.HTTPError, ValueError) as exc:
            retry = _retry_at(response.headers.get("Retry-After") if response is not None else None)
            failure = {
                "query_sha256": digest,
                "endpoint": ENDPOINT,
                "error": str(exc),
                "status": response.status_code if response is not None else None,
                "retry_at_epoch": retry,
                "retry_at": _iso_timestamp(retry),
                "automatic_retry": False,
            }
            _write(self.directory / "deferred.json", failure)
            self.failures.append(failure)
            self.stopped = True
            return None, digest


def _mapping_query(numbers: list[str]) -> str:
    values = " ".join(f'"{number}"' for number in sorted(numbers, key=int))
    return (
        "SELECT DISTINCT ?gutenberg ?item WHERE { VALUES ?gutenberg { "
        + values
        + " } ?item <"
        + DIRECT
        + "P2034> ?gutenberg . } LIMIT 5001"
    )


def _metadata_query(nodes: list[str]) -> str:
    values = " ".join(f"wd:{node}" for node in sorted(nodes))
    return (
        "SELECT ?node ?predicate ?value WHERE { VALUES ?node { "
        + values
        + " } VALUES ?predicate { wdt:P31 wdt:P629 wdt:P50 rdfs:label } "
        '?node ?predicate ?value . FILTER (?predicate != rdfs:label || LANG(?value) IN ("en", "ru", "fr", "de")) '
        "} LIMIT 5001"
    )


def _metadata(rows: list[dict[str, Any]], requested: set[str], query: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {
        node: {"types": set(), "parents": set(), "authors": set(), "labels": {}, "query_sha256": query}
        for node in requested
    }
    for row in rows:
        node = _qid(_value(row, "node", "uri"))
        predicate = _value(row, "predicate", "uri")
        if node not in requested:
            raise ValueError("Endpoint returned metadata for an unrequested entity")
        value = _value(row, "value")
        if predicate == LABEL:
            binding = row["value"]
            language = binding.get("xml:lang")
            if language in LANGUAGES and binding.get("type") == "literal":
                result[node]["labels"][language] = value
        elif predicate in {DIRECT + "P31", DIRECT + "P629", DIRECT + "P50"}:
            target = _qid(_value(row, "value", "uri"))
            if target is None:
                raise ValueError("Endpoint returned a non-Wikidata entity relation")
            field_name = {"P31": "types", "P629": "parents", "P50": "authors"}[predicate.rsplit("/", 1)[-1]]
            result[node][field_name].add(target)
        else:
            raise ValueError("Unexpected authority predicate")
    return result


def _describe(node: str, metadata: dict[str, dict[str, Any]]) -> dict[str, Any]:
    data = metadata.get(node, {})
    labels = data.get("labels", {})
    return {
        "qid": node,
        "url": f"https://www.wikidata.org/wiki/{node}",
        "labels": labels,
        "label": next((labels[lang] for lang in LANGUAGES if lang in labels), None),
        "types": sorted(data.get("types", [])),
        "classification": "edition"
        if EDITION_TYPES & set(data.get("types", []))
        else "work"
        if WORK_TYPES & set(data.get("types", []))
        else "unknown",
    }


def _candidate(start: str, metadata: dict[str, dict[str, Any]]) -> dict[str, Any]:
    path: list[str] = []
    edges = []
    current = start
    reason = ""
    for _ in range(MAX_DEPTH + 1):
        if current in path:
            reason = "p629_cycle"
            break
        path.append(current)
        data = metadata.get(current)
        if data is None:
            reason = "authority_metadata_unavailable"
            break
        parents = sorted(data["parents"])
        if len(parents) > 1:
            reason = "multiple_p629_parents"
            break
        if not parents:
            kind = _describe(current, metadata)["classification"]
            reason = (
                "explicit_work_root"
                if kind == "work"
                else "edition_without_work_parent"
                if kind == "edition"
                else "root_type_unknown"
            )
            break
        if len(edges) == MAX_DEPTH:
            reason = "p629_depth_limit"
            break
        edges.append(
            {"subject": current, "property": "P629", "object": parents[0], "query_sha256": data["query_sha256"]}
        )
        current = parents[0]
    root = _describe(path[-1], metadata)
    authors = sorted(metadata.get(path[-1], {}).get("authors", []))
    return {
        "item": _describe(start, metadata),
        "candidate_work": root,
        "path": path,
        "p629_evidence": edges,
        "reason": reason,
        "authors": [_describe(author, metadata) for author in authors],
        "confidence": "authority_linked" if reason == "explicit_work_root" else "ambiguous",
    }


def propose_work_links(entries: list[dict[str, Any]], cache_dir: Path) -> dict[str, Any]:
    """Propose authority links for up to 50 distinct Gutenberg edition IDs.

    Returns a proposal for every input entry, with original work IDs preserved.
    Network failure, throttling, malformed data or HTML errors halt further
    requests and persist a host cooldown. Cached successful queries remain
    usable during cooldown. No input manifests or databases are modified.
    """
    started = time.monotonic()
    cache_dir.mkdir(parents=True, exist_ok=True)
    query_dir = cache_dir / "queries"
    query_dir.mkdir(exist_ok=True)
    prepared: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        item: dict[str, Any] = {
            "entry_index": index,
            "id": entry.get("id"),
            "title": entry.get("title"),
            "author": entry.get("author"),
            "work_id": entry.get("work_id"),
            "work_identity_status": entry.get("work_identity_status"),
            "manual_curation_preserved": entry.get("work_identity_status") == "curated",
        }
        try:
            item["gutenberg_id"] = _number(entry)
        except ValueError as exc:
            item.update(confidence="unresolved", reason="invalid_provider_identity", error=str(exc))
        prepared.append(item)
    numbers: set[str] = {item["gutenberg_id"] for item in prepared if "gutenberg_id" in item}
    if len(numbers) > MAX_EDITIONS:
        raise ValueError("This bounded identity prototype accepts at most 50 distinct Gutenberg editions")
    mapping: dict[str, set[str]] = {number: set() for number in numbers}
    mapping_queries: dict[str, str] = {}
    metadata: dict[str, dict[str, Any]] = {}
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json", "Accept-Encoding": "gzip"},
        timeout=httpx.Timeout(25, connect=10),
        follow_redirects=False,
    ) as client:
        queries = _Queries(query_dir, client)
        if numbers:
            rows, digest = queries.fetch(_mapping_query(sorted(numbers, key=int)))
            if rows is not None:
                try:
                    for row in rows:
                        number = _value(row, "gutenberg", "literal")
                        node = _qid(_value(row, "item", "uri"))
                        if number not in mapping or node is None:
                            raise ValueError("Endpoint returned an unmatched Gutenberg identifier or entity")
                        mapping[number].add(node)
                    mapping_queries = dict.fromkeys(numbers, digest)
                except ValueError as exc:
                    # Reject the whole mapping response, avoiding partial false matches.
                    mapping = {number: set() for number in numbers}
                    queries.failures.append({"error": str(exc), "query_sha256": digest})
                    queries.stopped = True
        frontier = set().union(*mapping.values()) if mapping else set()
        for _ in range(MAX_DEPTH + 1):
            frontier -= metadata.keys()
            if not frontier or queries.stopped:
                break
            if len(metadata) + len(frontier) > MAX_NODES:
                queries.failures.append({"error": "Authority graph exceeds the bounded node limit"})
                break
            for offset in range(0, len(frontier), 50):
                batch = set(sorted(frontier)[offset : offset + 50])
                rows, digest = queries.fetch(_metadata_query(sorted(batch)))
                if rows is None:
                    break
                try:
                    metadata.update(_metadata(rows, batch, digest))
                except ValueError as exc:
                    queries.failures.append({"error": str(exc), "query_sha256": digest})
                    queries.stopped = True
                    break
            frontier = {parent for node in metadata.values() for parent in node["parents"]} - metadata.keys()
        # Author labels are descriptive evidence only and never an identity key.
        author_ids = sorted({author for node in metadata.values() for author in node["authors"]} - metadata.keys())
        author_ids = author_ids[: max(0, MAX_NODES - len(metadata))]
        for offset in range(0, len(author_ids), 50):
            if queries.stopped:
                break
            batch = set(author_ids[offset : offset + 50])
            rows, digest = queries.fetch(_metadata_query(sorted(batch)))
            if rows is not None:
                try:
                    metadata.update(_metadata(rows, batch, digest))
                except ValueError as exc:
                    queries.failures.append({"error": str(exc), "query_sha256": digest})
                    queries.stopped = True
        for item in prepared:
            number = item.get("gutenberg_id")
            if number is None:
                continue
            nodes = sorted(mapping[number])
            candidates = [_candidate(node, metadata) for node in nodes]
            item.update(
                candidates=candidates,
                p2034_evidence=[
                    {
                        "subject": node,
                        "property": "P2034",
                        "object": number,
                        "query_sha256": mapping_queries.get(number),
                    }
                    for node in nodes
                ],
            )
            if not nodes:
                item.update(
                    confidence="unresolved",
                    reason="no_p2034_match" if number in mapping_queries else "authority_unavailable",
                )
            elif len(nodes) > 1:
                item.update(confidence="ambiguous", reason="multiple_p2034_items")
            else:
                item.update(
                    confidence=candidates[0]["confidence"],
                    reason=candidates[0]["reason"],
                    candidate_qid=candidates[0]["candidate_work"]["qid"],
                )
        counts = Counter(item["confidence"] for item in prepared)
        duplicate_ids = {
            str(key): count for key, count in Counter(item["id"] for item in prepared).items() if count > 1
        }
        groups: dict[str, dict[str, Any]] = {}
        for item in prepared:
            if item["confidence"] != "authority_linked":
                continue
            qid = item["candidate_qid"]
            group = groups.setdefault(
                qid, {"candidate_qid": qid, "local_edition_ids": [], "existing_work_ids": [], "manual_work_ids": []}
            )
            group["local_edition_ids"].append(item["id"])
            for key, value in (
                ("existing_work_ids", item.get("work_id")),
                ("manual_work_ids", item.get("work_id") if item["manual_curation_preserved"] else None),
            ):
                if value and value not in group[key]:
                    group[key].append(value)
        for group in groups.values():
            group["multiple_manual_groups"] = len(group["manual_work_ids"]) > 1
            group["action"] = "review_only_no_merge"
        report = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "provider": "Wikidata",
            "license": "CC0-1.0",
            "endpoint": ENDPOINT,
            "input_entries": len(entries),
            "distinct_gutenberg_editions": len(numbers),
            "duplicate_input_ids": duplicate_ids,
            "authority_groups": list(groups.values()),
            "counts": dict(counts),
            "proposals": prepared,
            "network_requests": queries.requests,
            "cache_hits": queries.cache_hits,
            "deferred": queries.stopped,
            "failures": queries.failures,
            "query_evidence": queries.evidence,
            "database_modified": False,
            "identity_count_promotion_allowed": False,
            "limitations": [
                "Authority assertions are proposals requiring review, not verified unique-work counts.",
                "Unknown types, multiple matches, cycles and chains beyond three edges stay ambiguous.",
                "Only direct P31 work types Q7725634/Q47461344/Q8261 are recognized; no fuzzy merges.",
                "Missing labels remain null; the main WDQS graph may omit some entity metadata.",
            ],
        }
    _write(cache_dir / "proposals.json", report)
    return report
