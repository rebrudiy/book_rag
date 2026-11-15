"""Bounded, read-only paragraph overlap proposals across canonical public books.

Sampled fingerprints generate candidates; full SHA256 paragraph sets verify
those candidates. Shared text never automatically establishes work identity.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import re
import resource
import sqlite3
import sys
import time
import unicodedata
from collections import Counter, OrderedDict
from collections.abc import Iterable, Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_MEMORY = 500 * 1024**2
MAX_OUTPUT = 100 * 1024**2
MAX_SCRATCH = 90 * 1024**2
PUBLIC_HOSTS = {
    "www.gutenberg.org",
    "gutenberg.org",
    "ru.wikisource.org",
    "ru.wikibooks.org",
    "github.com",
    "raw.githubusercontent.com",
}
FIELDS = "id,title,author,language,work_id,source_url,artifact_sha256,content_sha256,rights"
BREAKS = re.compile(r"\n[ \t]*\n+")
type Location = tuple[int, str, int, int]


def _write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    if path.is_symlink() or temporary.is_symlink():
        raise ValueError("Overlap report paths must not be symlinks")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def peak_memory() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def paragraphs(text: str) -> list[tuple[int, int]]:
    """Paragraph offsets preserve exact original text, including literal code."""
    offsets = []
    start = 0
    for boundary in itertools.chain(BREAKS.finditer(text), [None]):
        end = boundary.start() if boundary is not None else len(text)
        piece = text[start:end]
        left = start + len(piece) - len(piece.lstrip())
        right = end - len(piece) + len(piece.rstrip())
        if left < right:
            offsets.append((left, right))
        if boundary is not None:
            start = boundary.end()
    return offsets


def generated_credit(text: str) -> bool:
    """Exclude only explicit provider packaging; never guess a human preface."""
    return (
        text.startswith(("*** start of the project gutenberg ebook", "*** end of the project gutenberg ebook"))
        or text.startswith("the full project gutenberg")
        and "license" in text[:100]
        or "project gutenberg" in text
        and text.startswith("this ebook is for the use of anyone anywhere")
    )


def bottom_sample(values: Iterable[str], size: int) -> list[bytes]:
    """Bottom-k unique 64-bit hashes; memory depends on k, not book length."""
    heap: list[int] = []
    present: set[int] = set()
    for value in values:
        number = int.from_bytes(hashlib.blake2b(value.encode(), digest_size=8).digest(), "big")
        if number in present:
            continue
        if len(heap) < size:
            heapq.heappush(heap, -number)
            present.add(number)
        elif number < -heap[0]:
            present.remove(-heapq.heapreplace(heap, -number))
            present.add(number)
    return [number.to_bytes(8, "big") for number in sorted(present)]


def _eligible(
    db: sqlite3.Connection, book_id: str, minimum: int, counters: Counter[str]
) -> Iterator[tuple[str, Location]]:
    for ordinal, title, text in db.execute(
        "SELECT ordinal,title,text FROM sections WHERE book_id=? ORDER BY ordinal", (book_id,)
    ):
        counters["sections_read"] += 1
        counters["characters_read"] += len(text)
        for start, end in paragraphs(text):
            value = normalized(text[start:end])
            counters["paragraphs_seen"] += 1
            if len(value) < minimum:
                counters["short_paragraphs_not_fingerprinted"] += 1
                continue
            if generated_credit(value):
                counters["explicit_generated_credits_not_fingerprinted"] += 1
                continue
            yield value, (ordinal, title, start, end)


def _guards(rows: list[sqlite3.Row]) -> str:
    return hashlib.sha256(
        json.dumps([tuple(row) for row in rows], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _public(row: sqlite3.Row) -> bool:
    url = urlsplit(row["source_url"])
    rights = row["rights"].casefold()
    return (
        url.scheme == "https"
        and url.hostname in PUBLIC_HOSTS
        and bool(re.fullmatch(r"[a-f0-9]{64}", row["artifact_sha256"] or ""))
        and bool(re.fullmatch(r"[a-f0-9]{64}", row["content_sha256"] or ""))
        and ("public domain" in rights or "cc by-sa" in rights)
        and (url.hostname not in {"github.com", "raw.githubusercontent.com"} or url.path.startswith("/d2l-ai/"))
    )


def audit_near_duplicates(
    database: Path,
    output: Path,
    *,
    sample_size: int = 256,
    minimum_paragraph_chars: int = 80,
    max_document_frequency: int = 20,
    max_pairs: int = 200,
    candidate_cap: int = 100000,
) -> dict[str, Any]:
    """Generate measured overlap proposals without editing source SQLite or text."""
    if not 16 <= sample_size <= 256 or not 40 <= minimum_paragraph_chars <= 1000:
        raise ValueError("Bounded sample16–256 and minimum paragraph40–1000 required")
    if not 2 <= max_document_frequency <= 30 or not 1 <= max_pairs <= 500 or not 1 <= candidate_cap <= 100000:
        raise ValueError("Overlap candidate/DF/pair bounds exceeded")
    output.mkdir(parents=True, exist_ok=True)
    scratch_path = output / "fingerprints.sqlite3"
    if scratch_path.exists() or scratch_path.is_symlink():
        raise ValueError("Use a new audit output directory; existing scratch/results are retained")
    started = time.monotonic()
    counters: Counter[str] = Counter()
    source_uri = database.resolve().as_uri() + "?mode=ro"
    with (
        closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as db,
        closing(sqlite3.connect(scratch_path)) as scratch,
    ):
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA cache_size=-8192")
        db.execute("BEGIN")
        indexed = db.execute(f"SELECT {FIELDS} FROM books WHERE status='indexed' ORDER BY id").fetchall()
        books = [row for row in indexed if _public(row)]
        source_fingerprint = _guards(indexed)
        scratch.execute("PRAGMA journal_mode=OFF")
        scratch.execute("PRAGMA synchronous=OFF")
        scratch.execute("PRAGMA temp_store=MEMORY")
        scratch.execute("PRAGMA cache_size=-8192")
        scratch.execute("PRAGMA page_size=4096")
        scratch.execute(f"PRAGMA max_page_count={MAX_SCRATCH // 4096}")
        scratch.execute(
            "CREATE TABLE fingerprints(fingerprint BLOB,book INTEGER,PRIMARY KEY(fingerprint,book)) WITHOUT ROWID"
        )
        for number, book in enumerate(books):
            fingerprints = bottom_sample(
                (p for p, _ in _eligible(db, book["id"], minimum_paragraph_chars, counters)), sample_size
            )
            scratch.executemany("INSERT INTO fingerprints VALUES(?,?)", ((value, number) for value in fingerprints))
            counters["sampled_fingerprints"] += len(fingerprints)
            if (number + 1) % 100 == 0 or number + 1 == len(books):
                scratch.commit()
                if peak_memory() > MAX_MEMORY:
                    raise ValueError("Audit exceeded measured500MiB peak-memory bound; no source changes")
                _write(
                    output / "progress.json",
                    {
                        "phase": "fingerprinting",
                        "books_completed": number + 1,
                        "books_total": len(books),
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "scratch_bytes": scratch_path.stat().st_size,
                        "peak_memory_bytes": peak_memory(),
                    },
                )
        sample_seconds = time.monotonic() - started
        votes: dict[int, int] = {}
        saturated = False
        for _, group in itertools.groupby(
            scratch.execute("SELECT fingerprint,book FROM fingerprints ORDER BY fingerprint,book"),
            key=lambda row: row[0],
        ):
            members = [row[1] for row in group]
            if len(members) > max_document_frequency:
                counters["common_sampled_hashes_excluded"] += 1
                continue
            for left, right in itertools.combinations(members, 2):
                pair = left * len(books) + right
                if pair in votes:
                    votes[pair] += 1
                elif len(votes) < candidate_cap:
                    votes[pair] = 1
                else:
                    saturated = True
        candidates = sorted(((value, pair) for pair, value in votes.items() if value >= 4), reverse=True)[:max_pairs]
        verification_counters: Counter[str] = Counter()
        cache: OrderedDict[int, dict[bytes, tuple[int, Location]]] = OrderedDict()

        def full_fingerprints(number: int) -> dict[bytes, tuple[int, Location]]:
            if number not in cache:
                if len(cache) >= 3:
                    cache.popitem(last=False)
                cache[number] = {
                    hashlib.sha256(value.encode()).digest(): (len(value), location)
                    for value, location in _eligible(
                        db, books[number]["id"], minimum_paragraph_chars, verification_counters
                    )
                }
            cache.move_to_end(number)
            return cache[number]

        results = []
        for votes_count, pair in candidates:
            left, right = divmod(pair, len(books))
            left_set, right_set = full_fingerprints(left), full_fingerprints(right)
            common = left_set.keys() & right_set.keys()
            shared_chars = sum(left_set[key][0] for key in common)
            total_left = sum(value[0] for value in left_set.values())
            total_right = sum(value[0] for value in right_set.values())
            ratio_left, ratio_right = shared_chars / max(total_left, 1), shared_chars / max(total_right, 1)
            relation = (
                "cross_language_shared_text_not_translation_proof"
                if books[left]["language"] != books[right]["language"]
                else "high_overlap_edition_candidate"
                if min(ratio_left, ratio_right) >= 0.75
                else "possible_containment_or_excerpt"
                if max(ratio_left, ratio_right) >= 0.5
                else "shared_passages_only"
            )
            proofs = []
            for digest in sorted(common)[:3]:
                evidence = []
                literal_values = []
                for number, values in [(left, left_set), (right, right_set)]:
                    ordinal, title, start, end = values[digest][1]
                    source = db.execute(
                        "SELECT text FROM sections WHERE book_id=? AND ordinal=?", (books[number]["id"], ordinal)
                    ).fetchone()[0]
                    raw = source[start:end]
                    literal_values.append(normalized(raw))
                    evidence.append(
                        {
                            "book_id": books[number]["id"],
                            "section_ordinal": ordinal,
                            "section_title": title,
                            "start": start,
                            "end": end,
                            "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                            "text_preview": raw[:180],
                        }
                    )
                if literal_values[0] != literal_values[1]:
                    raise ValueError("Paragraph SHA matched but normalized source text disagrees")
                proofs.append(
                    {"normalized_sha256": digest.hex(), "normalized_literal_equal": True, "sources": evidence}
                )
            results.append(
                {
                    "left": dict(books[left]),
                    "right": dict(books[right]),
                    "sample_shared_hashes": votes_count,
                    "full_shared_distinct_paragraphs": len(common),
                    "left_distinct_eligible_paragraphs": len(left_set),
                    "right_distinct_eligible_paragraphs": len(right_set),
                    "shared_normalized_characters": shared_chars,
                    "left_character_overlap": round(ratio_left, 6),
                    "right_character_overlap": round(ratio_right, 6),
                    "paragraph_jaccard": round(len(common) / max(len(left_set.keys() | right_set.keys()), 1), 6),
                    "relationship_proposal": relation,
                    "same_existing_work_id": books[left]["work_id"] == books[right]["work_id"],
                    "source_examples": proofs,
                    "manual_review": "pending",
                    "identity_verified": False,
                }
            )
            if peak_memory() > MAX_MEMORY:
                raise ValueError("Audit exceeded measured500MiB peak-memory bound during candidate verification")
        after = db.execute(f"SELECT {FIELDS} FROM books WHERE status='indexed' ORDER BY id").fetchall()
        if _guards(after) != source_fingerprint:
            raise ValueError("Source snapshot guards changed inside read transaction")
        db.rollback()
        live = db.execute(f"SELECT {FIELDS} FROM books WHERE status='indexed' ORDER BY id").fetchall()
        live_unchanged = _guards(live) == source_fingerprint
        report = {
            "schema_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "source_database": str(database),
            "indexed_books": len(indexed),
            "public_books_audited": len(books),
            "outside_public_source_scope_skipped": len(indexed) - len(books),
            "source_guards_sha256": source_fingerprint,
            "source_guards_still_current": live_unchanged,
            "sample_size": sample_size,
            "minimum_normalized_paragraph_chars": minimum_paragraph_chars,
            "normalization": (
                "NFC, Unicode casefold, whitespace collapsed; source text and exact source offsets retained"
            ),
            "sampled_document_frequency_cap": max_document_frequency,
            "candidate_pair_cap": candidate_cap,
            "candidate_generation_truncated": saturated,
            "candidate_pairs_with_any_vote": len(votes),
            "verified_candidate_pairs": len(results),
            "counters": dict(counters),
            "candidate_verification_counters": dict(verification_counters),
            "sample_seconds": round(sample_seconds, 3),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "peak_memory_bytes": peak_memory(),
            "scratch_bytes": scratch_path.stat().st_size,
            "relationship_proposal_counts": dict(Counter(r["relationship_proposal"] for r in results)),
            "pairs": results,
            "database_modified": False,
            "models_used": False,
            "identity_count_promotion_allowed": False,
            "limitations": [
                "Bottom-k candidates have incomplete recall, especially short works inside long anthologies",
                "Common-hash document frequency is measured over samples, not every paragraph in the corpus",
                "Overlap does not establish one original work, translation identity, or source completeness",
                "Repeated paragraphs count once per book; short paragraphs below threshold are not fingerprinted",
            ],
        }
        _write(output / "report.json", report)
        extra_bytes = sum(p.stat().st_size for p in output.iterdir() if p.is_file())
        if extra_bytes > MAX_OUTPUT:
            raise ValueError("Audit output exceeded100MiB bound")
        return report
