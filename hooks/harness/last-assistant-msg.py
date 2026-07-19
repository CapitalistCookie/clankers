#!/usr/bin/env python3
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
"""Print the text of the LAST assistant message in a Claude Code transcript.

Shared by the Stop-hook gates (iron-law-check, scope-calibration) — Stop payloads
carry transcript_path but no assistant_message, so gates extract it themselves.
Reads only the tail (default 400 lines) for speed; prints nothing on any error
(callers treat empty as nothing-to-check — fail-open on OUR bugs)."""
import json
import sys
from collections import deque


def main():
    if len(sys.argv) < 2:
        return 0
    try:
        with open(sys.argv[1], errors="ignore") as f:
            tail = deque(f, maxlen=400)
    except Exception:
        return 0
    for line in reversed(tail):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
        role = rec.get("type") or msg.get("role", "")
        if role != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            print(content)
            return 0
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text"]
            text = "\n".join(p for p in parts if p)
            if text.strip():
                print(text)
                return 0
            continue  # assistant record with only tool_use blocks — keep looking
    return 0


if __name__ == "__main__":
    sys.exit(main())
