#!/usr/bin/env python3
"""Read only rate-limit events; never emit prompts or auth data.

Pass the current rollout JSONL, never infer a weekly limit from token counts.
"""

import argparse
import datetime as dt
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("rollout", type=Path)
args = parser.parse_args()
latest = None
for line in args.rollout.open():
    try:
        event = json.loads(line)
    except ValueError:
        continue
    limits = event.get("payload", {}).get("rate_limits")
    if not limits or limits.get("limit_id") != "codex":
        continue
    for window in (limits.get("primary"), limits.get("secondary")):
        if window and window.get("window_minutes") == 10080:
            latest = {
                "timestamp": event.get("timestamp"),
                "remaining_percent": 100 - window["used_percent"],
                "resets_at": dt.datetime.fromtimestamp(window["resets_at"], dt.UTC).isoformat(),
            }
print(json.dumps(latest or {"available": False}, indent=2))
