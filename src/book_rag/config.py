"""Environment-only runtime configuration."""

import os
from pathlib import Path


def data_dir() -> Path:
    """Return the local corpus/index location (never committed)."""
    return Path(os.environ.get("BOOKRAG_DATA_DIR", "data")).expanduser().resolve()
