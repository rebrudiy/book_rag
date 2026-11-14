"""Paired development measurements on a stable library with a ready subset index.

One owned SQLite copy is reused sequentially. Original data, vectors, bindings
and model cache metadata are never migrated or written by the evaluator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_rag.evaluation import compare_reports, evaluate, evaluation_library, load_dataset


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(source: Path, dataset: Path, output: Path, model_cache: Path | None, modes: list[str], k: int) -> None:
    source = source.resolve()
    if not 1 <= k <= 50:
        raise ValueError("k must be between 1 and 50")
    fixture = load_dataset(dataset)
    if fixture["split"] != "dev":
        raise ValueError("This iterative comparison accepts development data only; keep final checks separate")
    metadata = json.loads((source / "vectors/active.json").read_text())
    if metadata.get("coverage_kind") != "subset":
        raise ValueError("Select an explicitly installed ready subset index")
    copied_bytes = sum(
        path.stat().st_size for name in ("library.sqlite3", "library.sqlite3-wal") if (path := source / name).exists()
    )
    needed = copied_bytes + 256 * 1024**2 + 4 * 1024**3
    if shutil.disk_usage(tempfile.gettempdir()).free < needed:
        raise OSError("Evaluation copy requires its SQLite bytes, 256 MiB scratch and four GiB free reserve")
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    reports = {}
    # Publish success reports only after all original-input guards have passed
    # at context exit. A failed capture/run leaves no apparently valid results.
    with evaluation_library(source, model_cache=model_cache) as library:
        required = set(fixture.get("required_book_ids", []))
        available = {book["id"] for book in library.books() if book["status"] == "indexed"}
        if missing := required - available:
            raise ValueError("Required development targets are not searchable: " + ", ".join(sorted(missing)))
        for mode in modes:
            print(f"Evaluating {mode}: {len(fixture['queries'])} development queries", flush=True)
            reports[mode] = evaluate(library, dataset, mode=mode, limit=k)
            reports[mode]["limitations"].extend(
                [
                    "Known target labels are reused with more distractors; other relevant editions may be unlabeled.",
                    "Scoped negatives apply only to their selected book, not to every edition in the library.",
                    "Runs share one process; RSS and warm caches are not isolated mode-to-mode cost comparisons.",
                ]
            )
    snapshots = [report["corpus_snapshot"] for report in reports.values()]
    if not snapshots or any(snapshot != snapshots[0] for snapshot in snapshots[1:]):
        raise RuntimeError("Paired corpus snapshots differ")
    write_json(output / "corpus-metadata.json", snapshots[0])
    snapshot_sha = hashlib.sha256((output / "corpus-metadata.json").read_bytes()).hexdigest()
    for mode, report in reports.items():
        report.pop("corpus_snapshot")
        report["corpus_snapshot_artifact"] = {"path": "corpus-metadata.json", "sha256": snapshot_sha}
        write_json(output / f"{mode}.json", report)
    comparisons = {}
    for left, right in zip(modes, modes[1:], strict=False):
        comparisons[f"{left}_to_{right}"] = compare_reports(reports[left], reports[right])
    summary = {
        "created_at": datetime.now(UTC).isoformat(),
        "seconds": round(time.perf_counter() - started, 3),
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "index": {
            key: metadata.get(key)
            for key in ("generation", "vectors", "passages", "model", "model_revision", "installation_receipt")
        },
        "semantic_editions": metadata["coverage"]["books"],
        "metrics": {mode: report["metrics"] for mode, report in reports.items()},
        "actual_modes": {mode: report["actual_search_modes"] for mode, report in reports.items()},
        "comparisons": comparisons,
        "source_guards_passed": True,
    }
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--dataset", type=Path, default=Path("evals/dev-expanded.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=("baseline", "lexical", "hybrid", "semantic"),
        default=["baseline", "lexical", "hybrid"],
    )
    parser.add_argument("--k", type=int, default=5)
    arguments = parser.parse_args()
    if len(set(arguments.modes)) != len(arguments.modes):
        parser.error("Modes must not repeat")
    run(arguments.source, arguments.dataset, arguments.output, arguments.model_cache, arguments.modes, arguments.k)


if __name__ == "__main__":
    main()
