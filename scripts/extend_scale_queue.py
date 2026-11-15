#!/usr/bin/env python3
"""Append affordable distinct candidates from a sealed offline planning receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path

from book_rag.corpus_scale import _save


def extend(original: Path, output: Path, data_dir: Path, target: int = 7000) -> dict:
    old_report = json.loads(original.with_suffix(".report.json").read_text())
    assert hashlib.sha256(original.read_bytes()).hexdigest() == old_report["queue_sha256"]
    entries = json.loads(original.read_text())
    seen = {e["id"] for e in entries}
    keys = {e["work_id"] for e in entries}
    if output.exists() or output.with_suffix(".report.json").exists():
        raise FileExistsError("Preserve existing queue receipt")
    free = shutil.disk_usage(data_dir).free
    reserve = int(4.5 * 1024**3)
    budget = free - reserve
    spent = sum(e["resource_forecast"]["estimated_added_bytes"] for e in entries)
    with sqlite3.connect(
        "file:" + str(Path(old_report["planning_db"]).resolve()) + "?mode=ro&immutable=1", uri=True
    ) as db:
        for identity, raw, cost in db.execute(
            "SELECT id,entry,projected_bytes FROM candidates ORDER BY projected_bytes,txt_bytes,id"
        ):
            if len(entries) >= target:
                break
            if identity in seen or spent + cost > budget:
                continue
            e = json.loads(raw)
            if e["work_id"] in keys:
                continue
            entries.append(e)
            seen.add(identity)
            keys.add(e["work_id"])
            spent += cost
    assert len(entries) == len(seen) == len(keys)
    _save(output, entries)
    report = {
        **old_report,
        "queue": str(output),
        "selected": len(entries),
        "queue_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "free_bytes_before": free,
        "reserve_bytes": reserve,
        "budget_bytes": budget,
        "projected_added_bytes": spent,
        "selected_by_language": dict(Counter(e["language"] for e in entries)),
        "selected_by_subject": dict(Counter(e["primary_subject_area"] for e in entries)),
        "selected_by_format": dict(Counter(e["format"] for e in entries)),
        "rdf_utf8_text_bytes": sum(e["resource_forecast"]["rdf_utf8_text_bytes"] for e in entries),
        "original_queue": str(original),
        "original_queue_sha256": old_report["queue_sha256"],
        "appended": len(entries) - old_report["selected"],
        "selection_tail": "lowest projected source/index cost from remaining distinct screened candidates",
        "budget_note": (
            "Counts full original queue cost, including books already indexed since baseline, conservatively."
        ),
    }
    report["records"] = {k: v for k, v in old_report["records"].items() if k != "budget_omitted"}
    report["initial_prefix_budget_omitted"] = old_report["records"].get("budget_omitted", 0)
    report["remaining_unselected_candidates"] = old_report["eligible_candidate_groups"] - len(entries)
    report["target_limit_reached"] = len(entries) >= target
    report["dedup_evidence"] = str(original.with_suffix(".dedup.jsonl"))
    _save(output.with_suffix(".report.json"), report)
    return report


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--original", type=Path, default=Path("data/catalog/scale10000-v1/queue.json"))
    p.add_argument("--output", type=Path, default=Path("data/catalog/scale10000-v2/queue.json"))
    p.add_argument("--data-dir", type=Path, default=Path("data/full"))
    p.add_argument("--target", type=int, default=7000)
    a = p.parse_args()
    print(json.dumps(extend(a.original, a.output, a.data_dir, a.target), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
