#!/usr/bin/env python3
"""Run a bounded read-only audit of canonical paragraph overlap."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from book_rag.content_overlap import audit_near_duplicates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/full/library.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("data/identity/content-overlap-v1"))
    parser.add_argument("--sample-size", type=int, default=256)
    parser.add_argument("--max-pairs", type=int, default=200)
    args = parser.parse_args()
    report = audit_near_duplicates(args.database, args.output, sample_size=args.sample_size, max_pairs=args.max_pairs)
    print(json.dumps({k: v for k, v in report.items() if k != "pairs"}, ensure_ascii=False, indent=2))
    return 0 if report["source_guards_still_current"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
