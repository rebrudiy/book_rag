"""Optional multilingual cross-encoder ranking of a bounded retrieval candidate pool.

Scores are raw ranking logits, never calibrated answer probabilities. The model
only changes ordering; exact source text, offsets and attribution stay intact.
"""

from __future__ import annotations

import importlib
import math
import os
import threading
import time
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
DEFAULT_REVISION = "1427fd652930e4ba29e8149678df786c240d8825"
MAX_CANDIDATES = 50
MAX_QUERY_TOKENS = 96
MAX_PAIR_TOKENS = 512
_INFERENCE_LOCK = threading.RLock()


@lru_cache(maxsize=1)
def _load_model(name: str, cache: str, revision: str | None) -> Any:
    transformers = importlib.import_module("sentence_transformers")
    torch = importlib.import_module("torch")
    model = transformers.CrossEncoder(
        name,
        revision=revision,
        cache_folder=cache,
        trust_remote_code=False,
        model_kwargs={"use_safetensors": True},
        activation_fn=torch.nn.Identity(),
        device=os.getenv("BOOKRAG_RERANKER_DEVICE") or None,
    )
    # A tokenizer's huge sentinel value is not a meaningful sequence length.
    capacity = model.max_seq_length or model.tokenizer.model_max_length
    max_length = min(MAX_PAIR_TOKENS, int(capacity))
    model.max_seq_length = max_length
    if max_length < 16:
        raise ValueError("Reranker sequence capacity is too small for query-passage pairs")
    return model


def rank_scored_results(
    results: list[dict[str, Any]], scores: Sequence[float], limit: int = 10
) -> list[dict[str, Any]]:
    """Stably order a copied candidate list using finite, uncalibrated logits."""
    if len(results) != len(scores):
        raise ValueError("Reranker must return exactly one score per candidate")
    if not 1 <= limit <= MAX_CANDIDATES:
        raise ValueError("limit must be between 1 and 50")
    scored: list[dict[str, Any]] = []
    for result, score in zip(results, scores, strict=True):
        if isinstance(score, bool) or not math.isfinite(float(score)):
            raise ValueError("Reranker scores must be finite numbers")
        scored.append(
            result | {"retrieval_score": result.get("score"), "score": float(score), "reranker_score": float(score)}
        )
    scored.sort(key=lambda result: -result["reranker_score"])
    return scored[:limit]


def _pairs(model: Any, query: str, results: list[dict[str, Any]]) -> tuple[list[tuple[str, str]], list[str]]:
    warnings = []
    tokenizer = model.tokenizer
    query_ids = tokenizer.encode(query, add_special_tokens=False)
    query_budget = min(MAX_QUERY_TOKENS, max(1, model.max_seq_length // 3))
    if len(query_ids) > query_budget:
        query = tokenizer.decode(query_ids[:query_budget], skip_special_tokens=True)
        query_ids = query_ids[:query_budget]
        warnings.append(f"Reranker query truncated to {query_budget} model tokens.")
    special_tokens = tokenizer.num_special_tokens_to_add(pair=True)
    passage_budget = model.max_seq_length - len(query_ids) - special_tokens
    if passage_budget <= 0:
        raise ValueError("Query leaves no reranker passage token budget")
    truncated = 0
    pairs = []
    for result in results:
        text = result["text"]
        ids = tokenizer.encode(text, add_special_tokens=False)
        if len(ids) > passage_budget:
            # Decode only the bounded model input. The returned source result
            # remains byte-for-byte identical to the caller's evidence record.
            text = tokenizer.decode(ids[:passage_budget], skip_special_tokens=True)
            truncated += 1
        pairs.append((query, text))
    if truncated:
        warnings.append(
            f"Reranker truncated {truncated} candidate passages to fit {model.max_seq_length}-token pairs; "
            "relevance near their ends may be missed. Source quotations are unchanged."
        )
    return pairs, warnings


def rerank(
    query: str,
    results: list[dict[str, Any]],
    cache_dir: Path,
    limit: int = 10,
    model_name: str | None = None,
) -> dict[str, Any]:
    """Rerank at most fifty candidates locally, with explicit availability state.

    Install the existing ``semantic`` extra for runtime support. Missing
    dependencies, inaccessible weights or inference failures return no ranked
    results and an explicit warning/``available=False``. Callers can offer a
    labeled retrieval fallback, but must not call it reranked output.
    """
    if not query.strip() or len(query) > 4000:
        raise ValueError("Reranker query must contain 1–4000 nonblank characters")
    if not 1 <= limit <= MAX_CANDIDATES:
        raise ValueError("limit must be between 1 and 50")
    if any(not isinstance(result.get("text"), str) or not result["text"].strip() for result in results):
        raise ValueError("Every reranking candidate needs nonempty source text")
    started = time.perf_counter()
    name = model_name or os.getenv("BOOKRAG_RERANKER_MODEL", DEFAULT_MODEL)
    revision = DEFAULT_REVISION if name == DEFAULT_MODEL else os.getenv("BOOKRAG_RERANKER_REVISION")
    candidates = results[:MAX_CANDIDATES]
    warnings = []
    if len(results) > MAX_CANDIDATES:
        warnings.append(f"Only the first {MAX_CANDIDATES} retrieval candidates were reranked.")
    if name != DEFAULT_MODEL and not revision:
        warnings.append("Custom reranker revision is unpinned; record its resolved revision for reproducibility.")
    report: dict[str, Any] = {
        "results": [],
        "model": name,
        "revision": revision,
        "elapsed_ms": 0.0,
        "warnings": warnings,
        "available": None,
        "scored_candidates": 0,
        "score_semantics": "raw ranking logits; not answer probabilities",
    }
    if not candidates:
        report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return report
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with _INFERENCE_LOCK:
            model = _load_model(name, str(cache_dir.resolve()), revision)
            pairs, truncation_warnings = _pairs(model, query, candidates)
            warnings.extend(truncation_warnings)
            scores = model.predict(pairs, batch_size=8, convert_to_numpy=True, show_progress_bar=False)
            report["results"] = rank_scored_results(candidates, scores, limit)
            report.update(
                available=True,
                scored_candidates=len(candidates),
                model_max_tokens=model.max_seq_length,
                device=str(getattr(model, "device", "unknown")),
            )
    except ImportError:
        report.update(available=False)
        warnings.append("Reranker unavailable: install optional dependencies with uv sync --extra semantic.")
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        report.update(available=False)
        warnings.append(f"Reranker failed ({type(exc).__name__}): {exc}")
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return report
