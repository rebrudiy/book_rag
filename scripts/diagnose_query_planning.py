"""One explicit open-dev planner call, retaining bounded rejected output privately."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from book_rag.local_generation import LocalGenerator
from book_rag.query_planning import PlanningGenerator, PlanningLimits, _books, _validate_plan
from scripts.probe_query_planning import run_probe


class RecordingPlanner:
    def __init__(self, runtime: PlanningGenerator, raw_path: Path) -> None:
        self.runtime = runtime
        self.raw_path = raw_path
        self.calls = 0
        self.validation_error = None

    def generate(
        self, messages: list[dict[str, str]], limits: Any = None, *, response_schema: Any = None
    ) -> dict[str, Any]:
        if self.calls >= 1:
            raise ValueError("One explicit diagnostic model request maximum")
        self.calls += 1
        record = {
            "request": messages,
            "schema": response_schema,
            "scope": "Ignored open research only, never UX/debug answer output",
        }
        try:
            result = self.runtime.generate(messages, limits, response_schema=response_schema)
            record["generation"] = result
            request = json.loads(messages[-1]["content"])
            try:
                text = result.get("text")
                if not isinstance(text, str):
                    raise ValueError("Planner did not return text")
                _validate_plan(
                    text,
                    _books(request["books"]),
                    PlanningLimits(
                        timeout_seconds=20,
                        max_queries=request["max_queries"],
                        max_queries_per_book=request["max_queries_per_book"],
                    ),
                    request.get("response_language_hint"),
                )
            except (ValueError, TypeError, KeyError, RecursionError) as error:
                self.validation_error = type(error).__name__ + ": " + str(error)
                record["validation_error"] = self.validation_error
            return result
        except Exception as error:
            record["runtime_error"] = type(error).__name__ + ": " + str(error)
            raise
        finally:
            self.raw_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/full"))
    parser.add_argument("--fixture", type=Path, default=Path("evals/rag-dev-v1.json"))
    parser.add_argument("--case", default="ru-query-smith-dinner")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    raw = args.report.with_suffix(".raw.json")
    project_data = Path("data").resolve()
    if (
        args.report.exists()
        or raw.exists()
        or not args.report.resolve().is_relative_to(project_data)
        or args.report.resolve().is_relative_to(args.source.resolve())
    ):
        raise ValueError("Use a new ignored research report underdata, outside the live library")
    if os.getenv("BOOKRAG_GENERATION_PROFILE") != "qwen3-4b-instruct-2507":
        raise ValueError("Explicit reviewed4Bprofile required")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    runtime = LocalGenerator()
    generator = RecordingPlanner(runtime, raw)
    try:
        report = run_probe(
            args.source,
            args.fixture,
            [args.case],
            generator=generator,
            limits=PlanningLimits(timeout_seconds=20, max_queries_per_book=1),
        )
    finally:
        runtime.close()
    report.update(
        diagnostic_generation_attempts=generator.calls,
        private_raw_path=str(raw),
        private_validation_error=generator.validation_error,
        model_workers_closed=True,
    )
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "summary": report["summary"],
                "used": report["cases"][0]["plan"]["used"],
                "reason": report["cases"][0]["plan"]["reason"],
                "validation_error": generator.validation_error,
                "model_workers_closed": True,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
