"""Explicitly bind an owned ready RU pilot; no source migrations or model load."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from book_rag.semantic_overlay import InstallationTarget, prepare_overlay


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    source = InstallationTarget(args.library)
    receipt = prepare_overlay(source, args.pilot, args.destination)
    print(
        json.dumps(
            {
                "installation": str((args.destination / "installation.json").resolve()),
                "covered_editions": receipt["coverage"]["books"],
                "windows": receipt["rows"],
                "ready_sha256": receipt["ready_sha256"],
                "target_database": receipt["target_db_path"],
                "enabled_automatically": False,
                "global_index_changed": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
