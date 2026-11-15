"""Bounded read-only old-CTE/native equality run on the public development fixture.

Only the public 54-query development fixture and lexical/baseline modes are
accepted. One captured control and candidate execution per case/mode, no model
or relevance/final-test evaluation. Source remains immutable/query-only and
guarded across the whole run; gold records stay local under ignored data/.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import runpy
import sqlite3
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LARGE = runpy.run_path(str(ROOT / "scripts/benchmark_lexical_large.py"))
PROTOTYPE = LARGE["PROTOTYPE"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_class(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise ValueError("Cannot load the declared benchmark implementation")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.Library


def reader(base: Any, shared: Any, name: str) -> Any:
    captured: list[dict[str, Any]] = []

    def initialize(self: Any) -> None:
        self.data_dir = shared.data_dir
        self.db_path = shared.db_path
        self.captured = captured

    @contextmanager
    def connect(self: Any) -> Generator[sqlite3.Connection]:
        with shared.connect() as connection:
            yield connection

    def result(row: sqlite3.Row) -> dict[str, Any]:
        captured.append(dict(row))
        return base._result(row)

    adapter = type(name, (base,), {"__init__": initialize, "connect": connect, "_result": staticmethod(result)})
    return adapter()


def run(arguments: Any) -> dict[str, Any]:
    fixture = json.loads(arguments.dataset.read_text())
    queries = fixture.get("queries", [])
    if fixture.get("split") != "dev" or len(queries) != 54:
        raise ValueError("Only the declared 54-query development fixture is allowed")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", query["id"]) for query in queries):
        raise ValueError("Query IDs must be safe local receipt basenames")
    if len({query["id"] for query in queries}) != 54:
        raise ValueError("Duplicate development IDs")
    if (
        not arguments.modes
        or len(set(arguments.modes)) != len(arguments.modes)
        or not set(arguments.modes)
        <= {
            "lexical",
            "baseline",
        }
    ):
        raise ValueError("Only unique lexical/baseline modes are allowed; model modes are excluded")
    if (
        not 0 < arguments.query_seconds <= 20
        or not 0 < arguments.control_seconds <= 240
        or not 0 < arguments.budget_seconds <= 300
    ):
        raise ValueError("Bounds are per-query20s/control240s/overall300s")
    arguments.output.mkdir(parents=True, exist_ok=False)
    gold = arguments.output / "unvalidated-control"
    gold.mkdir()
    started = time.monotonic()
    guard = LARGE["SourceGuard"](arguments.source)
    shared = LARGE["ReadOnlyLibrary"](guard, started + arguments.budget_seconds)
    with shared.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM books WHERE status='indexed'").fetchone()[0]
    if count != arguments.expected_books:
        raise ValueError("Source differs from the declared indexed-edition count")
    control = reader(load_class(arguments.control, "_gold_cte_control"), shared, "GoldControlReader")
    candidate = reader(load_class(arguments.candidate, "_candidate_native"), shared, "CandidateReader")
    state = {
        "status": "running",
        "validated": False,
        "pid": os.getpid(),
        "dataset_sha256": sha(arguments.dataset),
        "runner_sha256": sha(Path(__file__)),
        "source_generation": guard.generation,
        "indexed_editions": count,
        "control_sha256": sha(arguments.control),
        "candidate_sha256": sha(arguments.candidate),
        "modes": arguments.modes,
    }
    write(arguments.output / "state.json", state)
    jobs = [(mode, query) for mode in arguments.modes for query in queries]
    controls: dict[tuple[str, str], dict[str, Any]] = {}
    control_spent = 0.0
    records = []
    try:
        for index, (mode, query) in enumerate(jobs, 1):
            state.update(phase="control", case=query["id"], mode=mode, index=index, total=len(jobs))
            write(arguments.output / "state.json", state)
            if control_spent >= arguments.control_seconds:
                status, response = {"status": "skipped_control_budget"}, None
            else:
                control.captured.clear()
                filters = query.get("filters", {})
                status, response = LARGE["phase"](
                    shared,
                    f"control:{mode}:{query['id']}",
                    lambda q=query, m=mode, f=filters: control.search(
                        q["query"], m, f.get("book_id"), f.get("language"), 5
                    ),
                    min(arguments.query_seconds, arguments.control_seconds - control_spent),
                )
                control_spent += status.get("elapsed_ms", 0) / 1000
            entry = {
                "status": status,
                "mode": mode,
                "id": query["id"],
                "response": response,
                "raw": list(control.captured) if response is not None else [],
            }
            write(gold / f"{mode}-{query['id']}.json", entry)
            controls[(mode, query["id"])] = {"status": status, "path": str(gold / f"{mode}-{query['id']}.json")}
        guard.ensure()
        for index, (mode, query) in enumerate(jobs, 1):
            state.update(phase="candidate", case=query["id"], mode=mode, index=index, total=len(jobs))
            write(arguments.output / "state.json", state)
            candidate.captured.clear()
            filters = query.get("filters", {})
            status, response = LARGE["phase"](
                shared,
                f"candidate:{mode}:{query['id']}",
                lambda q=query, m=mode, f=filters: candidate.search(
                    q["query"], m, f.get("book_id"), f.get("language"), 5
                ),
                arguments.query_seconds,
            )
            prior = json.loads(Path(controls[(mode, query["id"])]["path"]).read_text())
            record: dict[str, Any] = {
                "id": query["id"],
                "mode": mode,
                "control": prior["status"],
                "candidate": status,
                "equivalence": "not_compared",
                "speedup": None,
            }
            if prior["status"]["status"] == status["status"] == "completed":
                case = PROTOTYPE["Case"](query["id"], query["query"], mode=mode, **filters)
                verification = PROTOTYPE["guard"](case, prior["raw"], candidate.captured)
                before = {key: value for key, value in prior["response"].items() if key != "elapsed_ms"}
                after = {key: value for key, value in response.items() if key != "elapsed_ms"}
                if before != after:
                    raise AssertionError(f"Response/warnings mismatch: {mode}/{query['id']}")
                record.update(
                    equivalence="exact",
                    guard=verification,
                    speedup=round(prior["status"]["elapsed_ms"] / status["elapsed_ms"], 3),
                )
            records.append(record)
            write(arguments.output / "progress.json", {"validated": False, "cases": records})
        guard.ensure()
        complete = len(records) == len(jobs) and all(record["equivalence"] == "exact" for record in records)
        state.update(
            status="complete" if complete else "incomplete",
            validated=complete,
            source_guards_passed=True,
            control_seconds=round(control_spent, 3),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
    except BaseException as error:
        state.update(status="discarded", validated=False, error=f"{type(error).__name__}: {error}")
        raise
    finally:
        write(arguments.output / "state.json", state)
    summary = {
        **state,
        "cases": records,
        "limitations": [
            "Equality/latency experiment, not a new relevance score or final-test evaluation.",
            "All controls run first; candidate caches may be warmer. Timings are not isolated repeated estimates.",
            "Timeout/skipped/source-guard failure receives no equality or speedup credit.",
            "Gold records contain local source text; keep the ignored output directory private to the workspace.",
            "Lexical/baseline modes only; no model or sealed-label access.",
        ],
    }
    write(arguments.output / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/full")
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/dev-expanded.json")
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, default=ROOT / "src/book_rag/library.py")
    parser.add_argument("--output", type=Path, default=ROOT / "data/bench-native-dev54-v1")
    parser.add_argument("--expected-books", type=int, default=4001)
    parser.add_argument("--modes", nargs="+", choices=("lexical", "baseline"), default=["lexical", "baseline"])
    parser.add_argument("--query-seconds", type=float, default=20)
    parser.add_argument("--control-seconds", type=float, default=240)
    parser.add_argument("--budget-seconds", type=float, default=300)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.source.resolve()):
        parser.error("Output must stay outside the source library")
    summary = run(args)
    print(json.dumps({key: summary[key] for key in ["status", "validated", "elapsed_seconds"]}))


if __name__ == "__main__":
    main()
