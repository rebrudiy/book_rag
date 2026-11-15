"""Explicit acquisition of the pinned MIT NLI experiment model, no hub cache."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from book_rag.nli_validation import DEFAULT_DIRECTORY, acquire_nli


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    args = parser.parse_args()
    print(json.dumps(acquire_nli(args.directory), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
