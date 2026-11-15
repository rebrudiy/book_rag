"""Real offline runtime smoke on explicit invented CC0 context and a greeting."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from book_rag.local_generation import GenerationLimits, LocalGenerator


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/bench/local-generation-smoke.json"))
    parser.add_argument("--json-schema", action="store_true")
    arguments = parser.parse_args()
    if arguments.output.exists():
        parser.error("Use a new owned output path")
    print(json.dumps({"pid": os.getpid(), "stage": "offline-smoke", "synthetic_only": True}), flush=True)
    generator = LocalGenerator()
    started = time.perf_counter()
    try:
        schema = (
            {
                "type": "object",
                "properties": {"drawer": {"type": "integer"}},
                "required": ["drawer"],
                "additionalProperties": False,
            }
            if arguments.json_schema
            else None
        )
        instruction = "Верни JSON с целочисленным полем drawer." if schema else "Ответь только номером."
        synthetic = generator.generate(
            [
                {
                    "role": "user",
                    "content": "В вымышленной комнате синяя папка лежит в ящике № 7, красная — "
                    "в ящике № 2. В каком ящике синяя папка? " + instruction,
                }
            ],
            GenerationLimits(max_tokens=32, timeout_seconds=60),
            response_schema=schema,
        )
        first_pid = generator.worker_pid
        neutral = generator.generate(
            [{"role": "user", "content": "Write one short greeting in English."}],
            GenerationLimits(max_tokens=32, timeout_seconds=30),
        )
        report = {
            "experiment": "local-generation-runtime-v1",
            "context_license": "CC0-1.0",
            "synthetic_context_only": True,
            "retrieval_used": False,
            "sealed_cases_used": False,
            "synthetic": synthetic,
            "synthetic_number_present": "7" in synthetic["text"],
            "synthetic_schema_checked": json.loads(synthetic["text"]) == {"drawer": 7} if schema else None,
            "neutral": neutral,
            "same_worker_reused": first_pid == generator.worker_pid,
            "worker_pid": generator.worker_pid,
            "total_ms": (time.perf_counter() - started) * 1000,
            "limitations": "Runtime smoke only; not RAG relevance, groundedness, citation "
            "or multilingual quality evaluation.",
        }
        if not report["synthetic_number_present"] or not neutral["text"].strip():
            raise RuntimeError("Local synthetic runtime smoke did not return the expected bounded text")
        if schema and report["synthetic_schema_checked"] is not True:
            raise RuntimeError("Local synthetic JSON-schema smoke did not return the expected typed field")
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        generator.close()


if __name__ == "__main__":
    main()
