"""Register complete direct-live batches without copying or rebinding artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from book_rag.semantic_batch_overlay import install_batch_registry
from book_rag.semantic_overlay import InstallationTarget


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument(
        "--batch", type=Path, action="append", required=True, help="Repeat for each completed batch root"
    )
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--compact",
        type=Path,
        nargs=2,
        action="append",
        metavar=("BATCH", "PAYLOAD"),
        help="Explicit compact-v2 manifest for a selected batch; repeat as needed",
    )
    args = parser.parse_args()
    pairs = args.compact or []
    compact = dict(pairs)
    if len(compact) != len(pairs):
        parser.error("A batch may have only one compact payload")
    install_batch_registry(InstallationTarget(args.library), args.batch, args.destination, compact_manifests=compact)
    print(
        json.dumps(
            {
                "registry": str((args.destination / "registry.json").resolve()),
                "registered_batches": len(args.batch),
                "enabled_automatically": False,
                "global_index_changed": False,
                "existing_bindings_rewritten": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
