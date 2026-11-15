"""Bounded scoped Russian word-form fallback, separate from production retrieval.

Only an empty, unquoted lexical result in one indexed Russian edition is eligible.
Canonical quotes are never rewritten. Stem overlap is a retrieval heuristic, not
proof that a passage answers a question or supports a generated assertion.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from book_rag.retrieval import prepare_query, tokenize

MAX_TERMS = 12
MAX_PREFIX_VARIANTS = 4
CANDIDATE_LIMIT = 200
MIN_STEM_LENGTH = 3
QUOTE_MARKS = frozenset("\"'«»“”„‘’")
NEGATIONS = frozenset(("не", "нет", "без", "ни"))
_RUSSIAN = re.compile(r"[а-яё]+\Z")
_LOCAL = threading.local()


@dataclass(frozen=True, slots=True)
class RussianFallbackPlan:
    terms: tuple[str, ...]
    keys: tuple[str, ...]
    expression: str
    exact_constraints: tuple[str, ...]


def _stemmer() -> Any:
    # Snowball's mutable generated stemmer is not safe to share between threads.
    if not hasattr(_LOCAL, "stemmer"):
        import snowballstemmer

        _LOCAL.stemmer = snowballstemmer.stemmer("russian")
    return _LOCAL.stemmer


@lru_cache(maxsize=32768)
def stem(token: str) -> str:
    return _stemmer().stemWord(token.replace("ё", "е")) if _RUSSIAN.fullmatch(token) else token


def _prefixes(value: str) -> tuple[str, ...]:
    # unicode61 keeps Cyrillic е/ё distinct; Snowball folds them. Retain bounded
    # spelling variants for retrieval, then verify exact Snowball stem equality.
    positions = [index for index, char in enumerate(value) if char == "е"]
    variants = [value]
    if len(positions) <= 2:
        for mask in range(1, 1 << len(positions)):
            chars = list(value)
            for bit, position in enumerate(positions):
                if mask & (1 << bit):
                    chars[position] = "ё"
            variants.append("".join(chars))
    else:
        variants += [value[:position] + "ё" + value[position + 1 :] for position in positions[:3]]
    return tuple(variants[:MAX_PREFIX_VARIANTS])


def prepare_russian_fallback(query: str) -> RussianFallbackPlan | None:
    if any(mark in query for mark in QUOTE_MARKS):
        return None
    original = prepare_query(query)
    if not original.terms or original.phrases:
        return None
    keys: list[str] = []
    terms: list[str] = []
    expressions: list[str] = []
    constraints = [term for term in original.terms if term in NEGATIONS or term.isdecimal()]
    has_prefix = False
    for term in original.terms:
        if term in constraints:
            continue
        key = stem(term)
        russian_prefix = bool(_RUSSIAN.fullmatch(key) and len(key) >= MIN_STEM_LENGTH and term not in NEGATIONS)
        if _RUSSIAN.fullmatch(term) and not russian_prefix and term not in NEGATIONS:
            return None
        if key in keys:
            continue
        terms.append(term)
        keys.append(key)
        if russian_prefix:
            has_prefix = True
            expressions.extend('"' + value + '"*' for value in _prefixes(key))
        else:
            # Tokens originate from prepare_query, never raw FTS operators.
            expressions.append('"' + term.replace('"', '""') + '"')
        if len(keys) > MAX_TERMS:
            return None
    if not has_prefix:
        return None
    return RussianFallbackPlan(tuple(terms), tuple(keys), " OR ".join(expressions), tuple(constraints))


def rerank_russian_fallback(
    plan: RussianFallbackPlan, results: Sequence[Mapping[str, Any]], *, limit: int = 10
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 50:
        raise ValueError("Fallback limit must be between1 and50")
    minimum = 1 if len(plan.keys) == 1 else max(2, math.ceil(len(plan.keys) * 0.5))
    ranked: list[dict[str, Any]] = []
    for result in results:
        if result.get("verified") is not True:
            continue
        tokens = set(tokenize(str(result.get("text") or "")))
        if not set(plan.exact_constraints) <= tokens:
            continue
        forms: dict[str, list[str]] = {}
        for token in tokens:
            forms.setdefault(stem(token), []).append(token)
        matched = []
        for term, key in zip(plan.terms, plan.keys, strict=True):
            if key not in forms:
                continue
            # Very short stripped stems can collide with a bare unrelated word.
            # Require an inflected source form for a longer query reduced to3
            # characters; exact three-letter query words keep their literal role.
            if (
                _RUSSIAN.fullmatch(key)
                and len(key) == 3
                and len(term) > 3
                and all(len(word) == 3 for word in forms[key])
            ):
                continue
            matched.append(key)
        if len(matched) < minimum:
            continue
        coverage = len(matched) / len(plan.keys)
        ranked.append(
            dict(result)
            | {
                "baseline_score": result.get("score"),
                "score": coverage,
                "stem_coverage": coverage,
                "matched_stems": matched,
                "retrieval_method": "russian-snowball-prefix-v1",
            }
        )
    ranked.sort(key=lambda result: (-result["score"], float(result["baseline_score"]), int(result["id"])))
    return ranked[:limit]


def scoped_russian_fallback(
    library: Any,
    query: str,
    book_id: str | None,
    language: str | None = None,
    *,
    prior_results: Sequence[Mapping[str, Any]],
    mode: str = "lexical",
    limit: int = 10,
) -> dict[str, Any]:
    """Prototype integration hook: caller retains prior results when used=False.

    It never calls Library.search, so a future root integration cannot recurse.
    The caller must pass the already completed normal lexical result explicitly.
    """
    started = time.perf_counter()
    response: dict[str, Any] = {"used": False, "results": [], "reason": "ineligible", "candidates": 0, "warnings": []}
    if mode != "lexical" or prior_results or not book_id or not book_id.strip() or language not in (None, "ru"):
        return response
    if not 1 <= limit <= 50:
        raise ValueError("Fallback limit must be between1 and50")
    if any(mark in query for mark in QUOTE_MARKS):
        return response | {"reason": "quoted_query_unchanged"}
    try:
        plan = prepare_russian_fallback(query)
    except ImportError:
        return response | {"reason": "snowball_unavailable"}
    if plan is None:
        return response | {"reason": "no_bounded_russian_stems"}
    with library.connect() as connection:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        book = connection.execute("SELECT language,status FROM books WHERE id=?", (book_id,)).fetchone()
        if book is None or book["language"] != "ru" or book["status"] != "indexed":
            return response | {"reason": "book_not_indexed_russian"}
        rows = library._lexical_rows(connection, plan.expression, book_id, language, CANDIDATE_LIMIT)
        candidates = [library._result(row) for row in rows]
    results = rerank_russian_fallback(plan, candidates, limit=limit)
    warnings = ["Использовано сопоставление русских словоформ; проверьте фрагменты в контексте."] if results else []
    if any(candidate.get("verified") is not True for candidate in candidates):
        warnings.append("Corrupt citation suppressed; rebuild the affected book")
    return response | {
        "used": True,
        "results": results,
        "reason": "stem_matches" if results else "no_supported_stem_matches",
        "candidates": len(candidates),
        "terms": list(plan.terms),
        "stems": list(plan.keys),
        "expression": plan.expression,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "warnings": warnings,
    }
