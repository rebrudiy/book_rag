"""Bounded, read-only authority proposals for public Gutenberg catalog editions.

Exact P2034 and explicit bounded P629 paths are evidence, never permission to
merge collections/components or promote confirmed unique-work counts.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import sqlite3
import time
import unicodedata
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET

from book_rag import work_identity as legacy

MAX_EDITIONS = 200
MAX_REQUESTS = 4
MAX_RESPONSE_BYTES = 2 * 1024**2  # <=8 MiB across four responses, below 10 MiB budget.
DEADLINE_SECONDS = 120.0
MAX_ROWS = 5000
LANGUAGES = ("en", "de", "fr")
PREDICATES = {
    "P31": "types",
    "P629": "parents",
    "P50": "authors",
    "P136": "genres",
    "P527": "has_parts",
    "P361": "part_of",
}
COLLECTION_TYPES = {"Q2668072", "Q105420"}


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.is_symlink() or path.is_symlink():
        raise ValueError("Audit outputs must not be symlinks")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def select_public_entries(
    manifests: list[Path], database: Path, reviewed: set[str], limit: int = MAX_EDITIONS
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Choose deterministic language-balanced indexed editions from allowlisted manifests only."""
    if not 1 <= limit <= MAX_EDITIONS:
        raise ValueError("Authority audit limit must be 1..200")
    candidates: dict[str, dict[str, Any]] = {}
    sources = []
    for manifest in manifests:
        body = manifest.read_bytes()
        entries = json.loads(body)
        if not isinstance(entries, list):
            raise ValueError("Public source manifest must be a list")
        sources.append({"path": str(manifest), "sha256": hashlib.sha256(body).hexdigest(), "entries": len(entries)})
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Invalid public source entry")
            try:
                number = legacy._number(entry)
            except ValueError:
                continue
            if entry["id"] in candidates and candidates[entry["id"]]["source_url"] != entry["source_url"]:
                raise ValueError("Conflicting public edition identities")
            candidates[entry["id"]] = {**entry, "gutenberg_id": number}
    eligible: dict[str, list[dict[str, Any]]] = {language: [] for language in LANGUAGES}
    skipped: Counter[str] = Counter()
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    try:
        for entry in candidates.values():
            if entry.get("language") not in eligible:
                skipped["outside_language_scope"] += 1
                continue
            if entry["id"] in reviewed or entry["gutenberg_id"] in reviewed:
                skipped["already_reviewed"] += 1
                continue
            if entry.get("coverage") != "full" or entry.get("work_type") != "book_or_text":
                skipped["partial_or_periodical_or_unclassified"] += 1
                continue
            if (
                not entry.get("catalog_rights_checked")
                or "public domain in the usa" not in str(entry.get("rights", "")).casefold()
            ):
                skipped["rights_not_verified_in_manifest"] += 1
                continue
            row = connection.execute(
                "SELECT status,work_id,artifact_sha256,local_path,source_url FROM books WHERE id=?",
                (entry["id"],),
            ).fetchone()
            if not row or row[0] != "indexed":
                skipped["not_indexed"] += 1
                continue
            try:
                if legacy._number({"id": entry["id"], "source_url": row[4]}) != entry["gutenberg_id"]:
                    raise ValueError("Different indexed provider identity")
            except ValueError:
                skipped["indexed_source_not_same_public_provider_id"] += 1
                continue
            metadata_row = connection.execute(
                "SELECT metadata FROM book_metadata WHERE book_id=?", (entry["id"],)
            ).fetchone()
            metadata = json.loads(metadata_row[0]) if metadata_row else {}
            if entry.get("work_identity_status") == "curated" or metadata.get("work_identity_status") == "curated":
                skipped["curated"] += 1
                continue
            if (
                metadata.get("coverage", entry["coverage"]) != "full"
                or metadata.get("work_type", entry["work_type"]) != "book_or_text"
            ):
                skipped["indexed_scope_not_full_book_or_text"] += 1
                continue
            eligible[entry["language"]].append(
                {**entry, "work_id": row[1], "indexed_artifact_sha256": row[2], "indexed_local_path": row[3]}
            )
    finally:
        connection.close()
    for entries in eligible.values():
        entries.sort(key=lambda e: hashlib.sha256(("authority-audit-200-v1:" + e["id"]).encode()).hexdigest())
    selected = []
    positions = dict.fromkeys(LANGUAGES, 0)
    while len(selected) < limit:
        progress = False
        for language in LANGUAGES:
            if positions[language] < len(eligible[language]) and len(selected) < limit:
                selected.append(eligible[language][positions[language]])
                positions[language] += 1
                progress = True
        if not progress:
            break
    return selected, {
        "public_manifests": sources,
        "eligible_by_language": {k: len(v) for k, v in eligible.items()},
        "selected_by_language": dict(Counter(e["language"] for e in selected)),
        "skipped": dict(skipped),
        "selection_method": "seeded sha256 within language; EN/DE/FR round-robin; public manifests + ro indexed guard",
    }


def batch_query(numbers: list[str]) -> str:
    if not numbers or len(set(numbers)) > 50 or any(not re.fullmatch(r"[1-9][0-9]{0,8}", n) for n in numbers):
        raise ValueError("A VALUES batch needs 1..50 validated Gutenberg IDs")
    values = " ".join('"' + n + '"' for n in sorted(set(numbers), key=int))
    predicates = " ".join("wdt:" + name for name in PREDICATES) + " rdfs:label"
    return (
        "PREFIX wdt: <http://www.wikidata.org/prop/direct/>\n"
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "SELECT DISTINCT ?gutenberg ?item ?node ?predicate ?value WHERE {\n"
        " VALUES ?gutenberg { " + values + " }\n"
        " ?item wdt:P2034 ?gutenberg .\n"
        " OPTIONAL {\n"
        "  ?item (wdt:P629?)/(wdt:P629?)/(wdt:P629?) ?workNode .\n"
        "  ?workNode wdt:P50? ?node .\n"
        "  VALUES ?predicate { " + predicates + " }\n"
        "  ?node ?predicate ?value .\n"
        '  FILTER (?predicate != rdfs:label || LANG(?value) IN ("en","de","fr","ru"))\n'
        " }\n} LIMIT 5001"
    )


class _BatchClient:
    def __init__(self, directory: Path, deadline_seconds: float) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.deadline = time.monotonic() + deadline_seconds
        self.requests = self.cache_hits = self.bytes = 0
        self.next_request = 0.0
        self.failures: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.stopped = False
        self.client = httpx.Client(
            headers={
                "User-Agent": legacy.USER_AGENT,
                "Accept": "application/sparql-results+json",
                "Accept-Encoding": "gzip",
            },
            timeout=httpx.Timeout(25, connect=10),
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def fetch(self, query: str) -> tuple[list[dict[str, Any]] | None, str]:
        digest = hashlib.sha256(query.encode()).hexdigest()
        body_path = self.directory / (digest + ".response.json")
        receipt_path = self.directory / (digest + ".receipt.json")
        if body_path.is_symlink() or receipt_path.is_symlink():
            raise ValueError("Authority query cache paths must not be symlinks")
        if body_path.is_file() and receipt_path.is_file() and body_path.stat().st_size <= MAX_RESPONSE_BYTES:
            try:
                receipt = json.loads(receipt_path.read_text())
                body = body_path.read_bytes()
                if (
                    isinstance(receipt, dict)
                    and receipt.get("query") == query
                    and receipt.get("endpoint") == legacy.ENDPOINT
                    and receipt.get("license") == "CC0-1.0"
                    and isinstance(receipt.get("retrieved_at"), str)
                    and hashlib.sha256(body).hexdigest() == receipt.get("response_sha256")
                ):
                    rows = legacy._bindings(json.loads(body))
                    self.cache_hits += 1
                    self.evidence.append({**receipt, "cache": True})
                    return rows, digest
            except (ValueError, OSError):
                pass
        if self.stopped:
            return None, digest
        deferred = self.directory / "deferred.json"
        if deferred.exists():
            try:
                state = json.loads(deferred.read_text())
                until = state.get("retry_at_epoch") if isinstance(state, dict) else None
            except (ValueError, OSError):
                state, until = {"error": "damaged persisted provider cooldown"}, None
            if not isinstance(until, (int, float)) or not math.isfinite(until) or until > time.time():
                self.stopped = True
                self.failures.append(state)
                return None, digest
        response = None
        try:
            if self.requests >= MAX_REQUESTS or time.monotonic() >= self.deadline:
                raise ValueError("Bounded authority query/deadline budget exhausted")
            pause = self.next_request - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Authority audit deadline reached")
            self.requests += 1
            with self.client.stream(
                "GET",
                legacy.ENDPOINT,
                params={"query": query, "format": "json"},
                timeout=httpx.Timeout(min(25, remaining), connect=min(10, remaining)),
            ) as response:
                if response.status_code != 200:
                    raise ValueError(f"Wikidata endpoint HTTP {response.status_code}")
                media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media not in {"application/json", "application/sparql-results+json"}:
                    raise ValueError("Wikidata returned non-JSON/HTML response")
                body_buffer = bytearray()
                for block in response.iter_bytes(chunk_size=64 * 1024):
                    if len(body_buffer) + len(block) > MAX_RESPONSE_BYTES or time.monotonic() > self.deadline:
                        raise ValueError("Wikidata response byte/deadline limit reached")
                    body_buffer.extend(block)
                body = bytes(body_buffer)
                rows = legacy._bindings(json.loads(body))
            self.bytes += len(body)
            receipt = {
                "query": query,
                "query_sha256": digest,
                "endpoint": legacy.ENDPOINT,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "response_sha256": hashlib.sha256(body).hexdigest(),
                "response_bytes": len(body),
                "license": "CC0-1.0",
                "request_method": "GET",
            }
            temporary = body_path.with_suffix(".tmp")
            if temporary.is_symlink():
                raise ValueError("Authority query temporary paths must not be symlinks")
            temporary.write_bytes(body)
            temporary.replace(body_path)
            _write(receipt_path, receipt)
            self.evidence.append({**receipt, "cache": False})
            return rows, digest
        except (ValueError, httpx.HTTPError) as error:
            retry = legacy._retry_at(response.headers.get("retry-after") if response is not None else None)
            failure = {
                "error": str(error),
                "status": response.status_code if response is not None else None,
                "query_sha256": digest,
                "endpoint": legacy.ENDPOINT,
                "retry_at_epoch": retry,
                "retry_at": legacy._iso_timestamp(retry),
                "automatic_retry": False,
            }
            _write(deferred, failure)
            self.failures.append(failure)
            self.stopped = True
            return None, digest
        finally:
            self.next_request = time.monotonic() + 2.0


def parse_batch(
    rows: list[dict[str, Any]], numbers: set[str], query_sha: str
) -> tuple[dict[str, set[str]], dict[str, dict[str, Any]]]:
    mapping: dict[str, set[str]] = {number: set() for number in numbers}
    metadata: dict[str, dict[str, Any]] = {}
    for row in rows:
        number = legacy._value(row, "gutenberg", "literal")
        item = legacy._qid(legacy._value(row, "item", "uri"))
        if number not in numbers or item is None:
            raise ValueError("Batch returned an unrequested Gutenberg ID or invalid authority item")
        mapping[number].add(item)
        if "node" not in row:
            if "predicate" in row or "value" in row:
                raise ValueError("Incomplete authority triple")
            continue
        node = legacy._qid(legacy._value(row, "node", "uri"))
        predicate = legacy._value(row, "predicate", "uri")
        if node is None:
            raise ValueError("Invalid metadata subject")
        data = metadata.setdefault(
            node, {**{name: set() for name in PREDICATES.values()}, "labels": {}, "query_sha256": query_sha}
        )
        if predicate == legacy.LABEL:
            binding = row["value"]
            language = binding.get("xml:lang")
            if binding.get("type") != "literal" or language not in legacy.LANGUAGES:
                raise ValueError("Unexpected authority label type/language")
            data["labels"][language] = legacy._value(row, "value", "literal")
        else:
            field = (
                PREDICATES.get(predicate.removeprefix(legacy.DIRECT)) if predicate.startswith(legacy.DIRECT) else None
            )
            value = legacy._qid(legacy._value(row, "value", "uri"))
            if field is None or value is None:
                raise ValueError("Unexpected authority relation")
            data[field].add(value)
    if len(metadata) > 1500:
        raise ValueError("Authority node limit exceeded")
    return mapping, metadata


def classify(start: str, metadata: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result = legacy._candidate(start, metadata)
    for qid in result["path"]:
        data = metadata.get(qid, {})
        if (
            COLLECTION_TYPES & (set(data.get("types", [])) | set(data.get("genres", [])))
            or data.get("has_parts")
            or data.get("part_of")
        ):
            result.update(confidence="ambiguous", reason="collection_or_component_scope_requires_review")
            break
    result["source_scope_verified"] = False
    return result


def propose_authority_audit(
    entries: list[dict[str, Any]], directory: Path, *, deadline_seconds: float = DEADLINE_SECONDS
) -> dict[str, Any]:
    if len(entries) > MAX_EDITIONS or not 0 < deadline_seconds <= DEADLINE_SECONDS:
        raise ValueError("Audit exceeds the 200-edition/120-second bound")
    numbers = [legacy._number(e) for e in entries]
    if len(set(numbers)) != len(numbers):
        raise ValueError("Audit requires distinct Gutenberg edition IDs")
    proposals = []
    client = _BatchClient(directory / "queries", deadline_seconds)
    try:
        for offset in range(0, len(entries), 50):
            batch = entries[offset : offset + 50]
            batch_numbers = {legacy._number(e) for e in batch}
            rows, query_sha = client.fetch(batch_query(list(batch_numbers)))
            mapping, metadata = {n: set() for n in batch_numbers}, {}
            complete = rows is not None
            if complete:
                try:
                    mapping, metadata = parse_batch(rows or [], batch_numbers, query_sha)
                except ValueError as error:
                    client.failures.append({"error": str(error), "query_sha256": query_sha})
                    client.stopped = True
                    complete = False
            for entry in batch:
                number = legacy._number(entry)
                items = sorted(mapping[number]) if complete else []
                candidates = [classify(item, metadata) for item in items]
                item: dict[str, Any] = {
                    "id": entry["id"],
                    "gutenberg_id": number,
                    "title": entry["title"],
                    "author": entry["author"],
                    "language": entry["language"],
                    "work_id": entry.get("work_id"),
                    "work_identity_status": entry.get("work_identity_status"),
                    "query_sha256": query_sha,
                    "mapping_query_complete": complete,
                    "candidates": candidates,
                    "p2034_evidence": [
                        {"subject": qid, "property": "P2034", "object": number, "query_sha256": query_sha}
                        for qid in items
                    ],
                    "source_scope_verified": False,
                }
                if not complete:
                    item.update(confidence="unresolved", reason="authority_unavailable")
                elif not items:
                    item.update(confidence="unresolved", reason="no_exact_p2034_claim_in_response")
                elif len(items) > 1:
                    item.update(confidence="ambiguous", reason="multiple_p2034_items")
                else:
                    item.update(
                        confidence=candidates[0]["confidence"],
                        reason=candidates[0]["reason"],
                        candidate_qid=candidates[0]["candidate_work"]["qid"],
                    )
                proposals.append(item)
        report = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "input_editions": len(entries),
            "languages": dict(Counter(e["language"] for e in entries)),
            "counts": dict(Counter(p["confidence"] for p in proposals)),
            "reason_counts": dict(Counter(p["reason"] for p in proposals)),
            "distinct_candidate_work_roots": len(
                {p["candidate_qid"] for p in proposals if p["confidence"] == "authority_linked"}
            ),
            "proposals": proposals,
            "query_evidence": client.evidence,
            "network_requests": client.requests,
            "cache_hits": client.cache_hits,
            "response_bytes": client.bytes,
            "failures": client.failures,
            "deferred": client.stopped,
            "database_modified": False,
            "identity_count_promotion_allowed": False,
            "license": "CC0-1.0",
            "endpoint": legacy.ENDPOINT,
            "bounds": {
                "max_requests": 4,
                "max_response_bytes_each": MAX_RESPONSE_BYTES,
                "max_total_response_bytes": 4 * MAX_RESPONSE_BYTES,
                "deadline_seconds": deadline_seconds,
                "max_p629_edges": 3,
            },
        }
        _write(directory / "proposals.json", report)
        return report
    finally:
        client.close()


def cached_frontmatter(entry: dict[str, Any]) -> dict[str, Any]:
    """Read bounded local public-source frontmatter; never fetch or extract files."""
    path = Path(entry.get("indexed_local_path") or "")
    result: dict[str, Any] = {
        "id": entry["id"],
        "source_url": entry["source_url"],
        "artifact_sha256": entry.get("indexed_artifact_sha256"),
        "status": "unavailable",
        "book_scope_verified": False,
    }
    if not path.is_file() or not re.fullmatch(r"[a-f0-9]{64}", str(result["artifact_sha256"])):
        return result
    if path.stat().st_size > 100 * 1024**2:
        return {**result, "status": "artifact_exceeds_bounded_local_review"}
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != result["artifact_sha256"]:
            return {**result, "status": "artifact_sha_mismatch"}
    previews, opf_titles, opf_authors = [], [], []
    if path.suffix.lower() == ".epub":
        with zipfile.ZipFile(path) as archive:

            def member(name: str, bound: int = 512 * 1024) -> bytes:
                with archive.open(name) as stream:
                    return stream.read(bound)

            container = ET.fromstring(
                member("META-INF/container.xml", 64 * 1024), forbid_dtd=True, forbid_entities=True, forbid_external=True
            )
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None:
                return {**result, "status": "missing_epub_package"}
            opf_path = rootfile.get("full-path", "")
            if opf_path.startswith("/") or ".." in opf_path.split("/"):
                return {**result, "status": "unsafe_epub_package_path"}
            opf = ET.fromstring(member(opf_path), forbid_dtd=True, forbid_entities=True, forbid_external=True)
            opf_titles = ["".join(n.itertext()) for n in opf.findall(".//{http://purl.org/dc/elements/1.1/}title")]
            opf_authors = ["".join(n.itertext()) for n in opf.findall(".//{http://purl.org/dc/elements/1.1/}creator")]
            manifest = {n.get("id"): n.get("href", "") for n in opf.findall(".//{*}manifest/{*}item")}
            for item in opf.findall(".//{*}spine/{*}itemref")[:4]:
                href = unquote(manifest.get(item.get("idref"), "").split("#", 1)[0])
                target = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), href))
                if not href or ":" in href or target.startswith(("/", "../")):
                    continue
                body = member(target)
                soup = BeautifulSoup(body, "html.parser")
                for n in soup.select("script, style, link, meta"):
                    n.decompose()
                visible = soup.get_text(" ", strip=True)
                previews.append(
                    {
                        "member": target,
                        "prefix_bytes": len(body),
                        "prefix_sha256": hashlib.sha256(body).hexdigest(),
                        "text_excerpt": visible[:2500],
                    }
                )
    elif path.suffix.lower() in {".txt", ".html", ".htm"}:
        with path.open("rb") as stream:
            body = stream.read(512 * 1024)
        visible = (
            BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
            if path.suffix.lower() != ".txt"
            else body.decode("utf-8", "replace")
        )
        previews.append(
            {
                "member": path.name,
                "prefix_bytes": len(body),
                "prefix_sha256": hashlib.sha256(body).hexdigest(),
                "text_excerpt": visible[:10000],
            }
        )
    else:
        return {**result, "status": "unsupported_local_frontmatter_format"}
    return {
        **result,
        "status": "cached_artifact_sha_verified",
        "opf_titles": opf_titles,
        "opf_authors": opf_authors,
        "frontmatter": previews,
        "scope": "OPF and first four spine member prefixes only; not a full book or edition audit",
    }


def frontmatter_observations(
    entry: dict[str, Any], proposal: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, Any]:
    def normalized(value: str) -> str:
        return " ".join(re.findall(r"[^\W\d_]+", unicodedata.normalize("NFKD", value).casefold()))

    text = normalized(
        " ".join(
            [
                *evidence.get("opf_titles", []),
                *evidence.get("opf_authors", []),
                *(v["text_excerpt"] for v in evidence.get("frontmatter", [])),
            ]
        )
    )
    surname = normalized(entry["author"].split(",")[0])
    candidates = proposal.get("candidates", [])
    authority_labels = {
        lang: label for c in candidates for lang, label in c["candidate_work"].get("labels", {}).items()
    }
    authority_authors = sorted({a.get("label") for c in candidates for a in c.get("authors", []) if a.get("label")})
    return {
        "id": entry["id"],
        "language": entry["language"],
        "catalog_title": entry["title"],
        "catalog_author": entry["author"],
        "authority_confidence": proposal["confidence"],
        "authority_reason": proposal["reason"],
        "candidate_qid": proposal.get("candidate_qid"),
        "cached_artifact_status": evidence["status"],
        "opf_titles": evidence.get("opf_titles", []),
        "opf_authors": evidence.get("opf_authors", []),
        "catalog_title_matches_opf": any(
            normalized(t) == normalized(entry["title"]) for t in evidence.get("opf_titles", [])
        ),
        "catalog_author_surname_present": bool(surname and surname in text),
        "authority_labels": authority_labels,
        "authority_authors": authority_authors,
        "authority_title_label_present": any(normalized(label) in text for label in authority_labels.values()),
        "authority_author_label_present": any(normalized(label) in text for label in authority_authors),
        "interpretation": "Lexical observations for manual review, never a fuzzy-title identity proof",
        "work_root_verified": False,
    }
