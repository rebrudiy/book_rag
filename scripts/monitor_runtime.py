"""Record timestamped, read-only corpus counters for later progress comparisons."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def observe(url: str, vector_dir: Path | None) -> dict:
    record = {"observed_at": datetime.now(UTC).isoformat()}
    try:
        with urlopen(url.rstrip("/") + "/api/stats", timeout=10) as response:
            payload = response.read(256 * 1024 + 1)
        if len(payload) > 256 * 1024:
            raise ValueError("Unexpectedly large stats response")
        record["stats"] = json.loads(payload)
    except (URLError, OSError, ValueError) as exc:
        record["stats_error"] = str(exc)
    result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False, timeout=5)
    record["git_head"] = result.stdout.strip() if result.returncode == 0 else None
    if vector_dir:
        checkpoints = []
        for path in sorted(vector_dir.glob("staging-*/checkpoint.json")):
            try:
                data = json.loads(path.read_text())
                checkpoints.append(
                    {
                        "staging": path.parent.name,
                        "completed_vectors": data["completed_vectors"],
                        "total_vectors": data["descriptor"]["vectors"],
                        "checkpoint_age_seconds": round(time.time() - path.stat().st_mtime, 3),
                    }
                )
            except (OSError, ValueError, KeyError) as exc:
                checkpoints.append({"checkpoint_error": str(exc)})
        record["embedding_checkpoints"] = checkpoints
        active = vector_dir / "active.json"
        record["completed_generation_published"] = active.exists()
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--output", type=Path, default=Path("data/runtime-observations.jsonl"))
    parser.add_argument("--vectors", type=Path)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 5 <= args.interval <= 3600:
        parser.error("Interval must be between 5 and 3600 seconds")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        while True:
            record = observe(args.url, args.vectors)
            with args.output.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
            if args.once:
                print(json.dumps(record, ensure_ascii=False))
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
