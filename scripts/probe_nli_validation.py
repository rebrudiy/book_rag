"""Offline NLI development probe; isolated CPU worker, bounded RAM and deadline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_rag.nli_validation import (
    DEFAULT_DIRECTORY,
    LABELS,
    MAX_MEMORY,
    REPOSITORY,
    REVISION,
    NLIPredictor,
    _save,
    peak_resident_bytes,
)

RUN04_SHA256 = "4709e7d4d7753c6e4309a6a0ee32222563a8ad71262f7d07a0ed22aed61329c6"


def make_pairs(captured: dict[str, Any], authored: dict[str, Any]) -> list[dict[str, Any]]:
    pairs = []
    for case_id, response in captured["responses"].items():
        sources = {s["source_id"]: s for s in response["sources"]}
        for number, claim in enumerate(response.get("claims", [])):
            quotes = []
            used = []
            provenance = []
            for quote in claim["supporting_quotes"]:
                source = sources[quote["source_id"]]
                start, end = quote["start"], quote["end"]
                base = source["start"]
                if (
                    type(start) is not int
                    or type(end) is not int
                    or type(base) is not int
                    or not base <= start < end <= base + len(source["text"])
                ):
                    raise ValueError("Invalid captured support offsets")
                if source["text"][start - base : end - base] != quote["quote"]:
                    raise ValueError("Captured quote does not match its original source text")
                quotes.append(quote["quote"])
                if quote["source_id"] not in used:
                    used.append(quote["source_id"])
                provenance.append(
                    {
                        "source_id": quote["source_id"],
                        "start": start,
                        "end": end,
                        "book_id": source["book_id"],
                        "artifact_sha256": source["artifact_sha256"],
                        "quote_sha256": hashlib.sha256(quote["quote"].encode()).hexdigest(),
                    }
                )
            for variant, premise in [
                ("quote_only", "\n\n".join(quotes)),
                ("captured_source_context", "\n\n".join(sources[sid]["text"] for sid in used)),
            ]:
                pairs.append(
                    {
                        "id": f"{case_id}:claim{number + 1}:{variant}",
                        "case_id": case_id,
                        "claim_index": number + 1,
                        "variant": variant,
                        "origin": "captured_run04",
                        "question": response["query"],
                        "premise": premise,
                        "hypothesis": claim["text"],
                        "support_provenance": provenance,
                        "author_assumed_relation": None,
                    }
                )
    for case in authored["cases"]:
        pairs.append(case | {"origin": "authored_CC0_development", "variant": "authored"})
    pairs.append(
        {
            "id": "explicit-context-limit",
            "origin": "authored_CC0_development",
            "variant": "context_limit",
            "premise": "A traveller crossed the river and entered the village. " * 600,
            "hypothesis": "The traveller entered a village.",
            "author_assumed_relation": None,
        }
    )
    return pairs


def worker(args: argparse.Namespace) -> int:
    capture_bytes = args.captures.read_bytes()
    if hashlib.sha256(capture_bytes).hexdigest() != RUN04_SHA256:
        raise ValueError("Open run04 capture SHA differs from independently reviewed pinned capture")
    captured = json.loads(capture_bytes)
    authored_bytes = args.authored.read_bytes()
    authored = json.loads(authored_bytes)
    if authored.get("sealed_evaluation") is not False or authored.get("license") != "CC0-1.0":
        raise ValueError("Probe requires explicitly open authored CC0 development inputs")
    pairs = make_pairs(captured, authored)
    review_bytes = args.review.read_bytes()
    review = json.loads(review_bytes)
    categories = {c["id"]: c["agent_review"]["category"] for c in review["cases"]}
    predictor = NLIPredictor(args.model)
    started = time.monotonic()
    report: dict[str, Any] = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "model": REPOSITORY,
        "revision": REVISION,
        "device": "cpu",
        "dtype": "float32",
        "threads": 2,
        "labels": LABELS,
        "captured_run04_sha256": RUN04_SHA256,
        "authored_fixture_sha256": hashlib.sha256(authored_bytes).hexdigest(),
        "AI_review_sha256": hashlib.sha256(review_bytes).hexdigest(),
        "human_gold_labels": False,
        "semantic_truth_verified": False,
        "question_axis_coverage_verified": False,
        "production_integrated": False,
        "truncation_allowed": False,
        "results": [],
        "status": "running",
    }
    _save(args.output, report)
    for pair in pairs:
        if time.monotonic() - started > 600:
            raise TimeoutError("NLI total worker deadline600s")
        result = pair | {
            "premise_sha256": hashlib.sha256(pair["premise"].encode()).hexdigest(),
            "premise_characters": len(pair["premise"]),
            "AI_case_review_category": categories.get(pair.get("case_id")),
        }
        try:
            result.update(predictor.score(pair["premise"], pair["hypothesis"]))
        except (ValueError, RuntimeError, OSError) as error:
            report["status"] = "failed_closed"
            report["error"] = type(error).__name__ + ": " + str(error)
            _save(args.output, report)
            raise
        result.pop("premise")  # hashes + capture/fixture pins reproduce text; no copied book context in public summary.
        report["results"].append(result)
        report.update(
            elapsed_seconds=round(time.monotonic() - started, 3),
            peak_memory_bytes=peak_resident_bytes(),
            model_load_seconds=round(predictor.load_seconds, 3),
        )
        _save(args.output, report)
    import torch
    import transformers

    devices = sorted({str(parameter.device) for parameter in predictor.model.parameters()})
    dtypes = sorted({str(parameter.dtype) for parameter in predictor.model.parameters()})
    if devices != ["cpu"] or dtypes != ["torch.float32"] or torch.get_num_threads() != 2:
        raise ValueError("Observed NLI device/dtype/thread configuration differs from the CPU prototype contract")
    report.update(status="complete", torch_version=torch.__version__, transformers_version=transformers.__version__)
    report.update(
        observed_parameter_devices=devices,
        observed_parameter_dtypes=dtypes,
        observed_threads=torch.get_num_threads(),
        observed_interop_threads=torch.get_num_interop_threads(),
        tokenizer_is_fast=bool(predictor.tokenizer.is_fast),
    )
    report["status_counts"] = dict(Counter(r["status"] for r in report["results"]))
    report["predicted_label_counts"] = dict(
        Counter(r.get("predicted_label") for r in report["results"] if r["status"] == "scored")
    )
    report["authored_assumption_agreement"] = dict(
        Counter(
            "agrees" if r["predicted_label"] == r["author_assumed_relation"] else "disagrees"
            for r in report["results"]
            if r.get("author_assumed_relation") and r["status"] == "scored"
        )
    )
    report["agreement_interpretation"] = (
        "Exploratory agreement with authored assumptions, not measured accuracy or independent human validation"
    )
    _save(args.output, report)
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False, indent=2), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument("--captures", type=Path, default=Path("data/evaluations/rag-dev-v1/run-04.responses.json"))
    parser.add_argument("--authored", type=Path, default=Path("evals/nli-contrastives-v1.json"))
    parser.add_argument("--review", type=Path, default=Path("experiments/rag-run04-agent-review.json"))
    parser.add_argument("--output", type=Path, default=Path("data/evaluations/nli-v1/results.json"))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    for path in [args.captures, args.authored, args.review]:
        if any("sealed" in part.casefold() for part in path.parts):
            parser.error("This experiment may not access sealed inputs")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.worker:
        return worker(args)
    command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker"]
    env = os.environ | {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "HF_HUB_DISABLE_PROGRESS_BARS": "1",
        "OMP_NUM_THREADS": "2",
        "MKL_NUM_THREADS": "2",
    }
    started = time.monotonic()
    child = subprocess.Popen(command, env=env, start_new_session=True)

    def record_failure(reason: str) -> None:
        report = json.loads(args.output.read_text()) if args.output.is_file() else {}
        report.update(status="failed_supervisor", supervisor_error=reason, worker_returncode=child.returncode)
        _save(args.output, report)

    try:
        while child.poll() is None:
            if time.monotonic() - started > 650:
                raise TimeoutError("NLI supervisor deadline650s")
            rss = subprocess.run(["ps", "-o", "rss=", "-p", str(child.pid)], capture_output=True, text=True, timeout=5)
            if rss.stdout.strip() and int(rss.stdout.strip()) * 1024 > MAX_MEMORY:
                raise MemoryError("NLI supervisor observed worker RSS above2.5GiB")
            time.sleep(0.5)
        if child.returncode:
            record_failure("NLI worker exited unsuccessfully")
        return child.returncode
    except (KeyboardInterrupt, TimeoutError, MemoryError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        record_failure(type(error).__name__ + ": " + str(error))
        raise
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
