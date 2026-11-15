"""Repack a sealed window manifest into an inactive canonical-offset prototype.

Uses only the verified cached tokenizer. No models, GPU, downloads, vectors,
production pointer changes or removal of existing manifests are permitted.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/full"))
    parser.add_argument(
        "--original", type=Path, default=Path("data/semantic-batches/multilingual1000-v1/windows.sqlite3")
    )
    parser.add_argument("--output", type=Path, default=Path("data/bench/compact-windows1000-v1/compact.sqlite3"))
    arguments = parser.parse_args()
    probe_path = arguments.output.with_suffix(arguments.output.suffix + ".probe.json")
    if probe_path.exists() or probe_path.is_symlink():
        raise FileExistsError("Use a new absent nonsymlink probe destination")
    os.environ["OMP_NUM_THREADS"] = "2"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    from book_rag.compact_window_manifest import load_pinned_tokenizer, repack_window_manifest
    from book_rag.semantic_pilot import _publish_json

    tokenizer, pin = load_pinned_tokenizer(arguments.source / "models")
    receipt = repack_window_manifest(
        arguments.source, arguments.original, arguments.output, tokenizer, tokenizer_receipt=pin
    )
    forbidden = [name for name in ("torch", "faiss", "llama_cpp") if name in sys.modules]
    if forbidden:
        raise RuntimeError(f"Unexpected model/native-search runtime imported: {forbidden}")
    maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    summary = {
        "kind": receipt["kind"],
        "prototype_only": True,
        "rows": receipt["rows"],
        "books": receipt["books"],
        "prefixes": receipt["prefixes"],
        "input_chain": receipt["input_chain"],
        "database_sha256": receipt["database_sha256"],
        "original_bytes": receipt["original_bytes"],
        "compact_bytes": receipt["compact_bytes"],
        "reduction_fraction": 1 - receipt["compact_bytes"] / receipt["original_bytes"],
        "elapsed_seconds": receipt["elapsed_seconds"],
        "peak_rss_bytes": maximum if sys.platform == "darwin" else maximum * 1024,
        "forbidden_runtime_modules_loaded": forbidden,
        "tokenizer_only": True,
        "old_manifest_retained": arguments.original.exists(),
        "source_guards_verified_before_after": receipt["source_guards_verified_before_after"],
        "output": str(arguments.output),
        "production_activation": False,
        "free_bytes_after": receipt["free_bytes_after"],
    }
    _publish_json(probe_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
