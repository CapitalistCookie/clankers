#!/usr/bin/env python3
"""Bootstrap clanker with historical session data."""

import json
import os

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")
SESSIONS_DIR = os.path.join(DATA_DIR, "raw/sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

CONV_PATH = "/data/meta-analysis/conversations.json"

with open(CONV_PATH) as f:
    data = json.load(f)

conversations = data.get("conversations", [])
records_by_date = {}
total = 0

for conv in conversations:
    ts = conv.get("created_at", "")
    if not ts:
        continue
    date_key = ts[:10]
    project = conv.get("project", "global")

    record = {
        "timestamp": ts,
        "session_id": conv.get("conversation_id", ""),
        "project": project,
        "cwd": os.path.expanduser(f"~/projects/{project}") if project != "global" else os.path.expanduser("~"),
        "duration_s": int(conv.get("duration_seconds", 0)),
        "claude_version": conv.get("cli_version", "unknown"),
        "tool_uses": conv.get("tool_uses", {}),
        "errors": conv.get("errors_encountered", 0),
        "error_tools": {},
        "files_touched": conv.get("files_touched", [])[:50],
        "files_touched_count": conv.get("files_touched_count", 0),
        "user_corrections": 0,
        "subagent_count": conv.get("subagent_count", 0),
        "outcome": "unknown",
        "source": "bootstrap",
    }

    records_by_date.setdefault(date_key, []).append(record)
    total += 1

for date_key, records in sorted(records_by_date.items()):
    outfile = os.path.join(SESSIONS_DIR, f"{date_key}.jsonl")
    with open(outfile, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

print(f"Ingested {total} sessions across {len(records_by_date)} days")

# Verify
total_records = 0
for f_name in os.listdir(SESSIONS_DIR):
    if f_name.endswith(".jsonl"):
        with open(os.path.join(SESSIONS_DIR, f_name)) as fh:
            total_records += sum(1 for line in fh if line.strip())
print(f"Total records in store: {total_records}")
