"""Explicit exploratory smoke on known development books, never the closed final."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_rag.answers import ask
from book_rag.library import Library
from book_rag.local_generation import LocalGenerator


class RecordingGenerator(LocalGenerator):
    def __init__(self) -> None:
        super().__init__(context_tokens=8192)
        self.outputs: list[dict[str, Any]] = []

    def generate(self, messages: Any, limits: Any = None, *, response_schema: Any = None) -> dict[str, Any]:
        result = super().generate(messages, limits, response_schema=response_schema)
        self.outputs.append(result)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/full"))
    parser.add_argument("--output", type=Path, default=Path("data/bench/local-rag-smoke.json"))
    parser.add_argument("--case", help="Run one named exploratory case")
    arguments = parser.parse_args()
    library = Library(arguments.data_dir)
    generator = RecordingGenerator()
    cases: list[dict[str, Any]] = [
        {
            "name": "known-alice-scoped-en",
            "query": "What does the White Rabbit take out of its waistcoat-pocket?",
            "book_ids": ["pg11-full"],
            "mode": "lexical",
        },
        {
            "name": "known-alice-global-en",
            "query": "Who follows a white rabbit and falls down a rabbit hole?",
            "mode": "hybrid",
        },
        {
            "name": "known-alice-wrong-topic",
            "query": "What does Alice explain about quantum encryption?",
            "book_ids": ["pg11-full"],
            "mode": "lexical",
        },
    ]
    if arguments.case:
        cases = [case for case in cases if case["name"] == arguments.case]
        if not cases:
            parser.error("Unknown exploratory case")
    report: dict[str, Any] = {
        "experiment": "local-rag-exploratory-smoke-v1",
        "started_at": datetime.now(UTC).isoformat(),
        "closed_final_used": False,
        "human_rated": False,
        "quality_benchmark": False,
        "corpus_before": library.stats(),
        "results": [],
    }
    try:
        for case in cases:
            first_generation = len(generator.outputs)
            print(json.dumps({"starting": case["name"]}), flush=True)
            result = ask(library, **{key: value for key, value in case.items() if key != "name"}, generator=generator)
            report["results"].append(
                {
                    "case": case,
                    "response": result,
                    "raw_generations": generator.outputs[first_generation:],
                }
            )
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "name": case["name"],
                        "status": result["status"],
                        "claims": result["claims"],
                        "source_count": len(result["sources"]),
                        "elapsed_ms": result["elapsed_ms"],
                        "warnings": result["warnings"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        report["completed_at"] = datetime.now(UTC).isoformat()
        report["corpus_after"] = library.stats()
        arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    finally:
        generator.close()


if __name__ == "__main__":
    main()
