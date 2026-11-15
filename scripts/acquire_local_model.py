"""Explicitly acquire the reviewed pinned GGUF; inference stays offline."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from book_rag.local_generation import DEFAULT_MODEL_DIRECTORY, GIB, acquire_model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT_MODEL_DIRECTORY)
    parser.add_argument("--reserve-gib", type=int, default=3)
    arguments = parser.parse_args()
    print(json.dumps({"pid": os.getpid(), "directory": str(arguments.directory), "stage": "acquisition"}), flush=True)
    print(json.dumps(acquire_model(arguments.directory, reserve_bytes=arguments.reserve_gib * GIB), indent=2))


if __name__ == "__main__":
    main()
