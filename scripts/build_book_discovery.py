"""Build/resume the standalone metadata-only semantic book catalog offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from book_rag.book_discovery import ReadOnlyBookLibrary, build_book_discovery


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/full"))
    parser.add_argument("--device", choices=["cpu", "mps"], default="cpu")
    parser.add_argument("--batch-size", type=int, default=128)
    args = parser.parse_args()
    print(
        json.dumps(
            build_book_discovery(ReadOnlyBookLibrary(args.data_dir), device=args.device, batch_size=args.batch_size),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
