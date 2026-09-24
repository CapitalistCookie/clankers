#!/usr/bin/env python3
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
"""Print the text of the LAST assistant message in a Claude Code transcript.

Shared by the Stop-hook gates (iron-law-check, scope-calibration) — Stop payloads
carry transcript_path but no assistant_message, so gates extract it themselves.
Reads only the tail (default 400 lines) for speed; prints nothing on any error
(callers treat empty as nothing-to-check — fail-open on OUR bugs) and logs the
error to the hook-error log."""
# ---- hook-error log: the same block in every clanker python hook ----------------
# A failure that the hook swallows (the hook stays fail-open for the session)
# appends one JSON line {ts, hook, session_id, cwd, rc, stderr_tail} to
# $CLANKER_DATA/raw/health/hook-errors-<UTC day>.jsonl (default /data/clanker),
# where `clanker doctor --harness` counts it. hook_err never raises, never
# blocks and never writes to stdout. Usage: hook_err(rc, step, error text or
# exception); stderr_tail is "<step>: " plus the end of the text (of the
# traceback, for an exception), 300 characters at most. Set
# HOOK_ERR["session_id"] and HOOK_ERR["cwd"] once the payload is parsed; a
# script read from stdin has no __file__ and sets HOOK_ERR["hook"] as well.
import os as _he_os
import sys as _he_sys

HOOK_ERR = {"hook": _he_os.path.basename(globals().get("__file__") or "") or "?",
            "session_id": "", "cwd": ""}


def hook_err(rc, step, err=""):
    try:
        import json
        import time
        import traceback
        if isinstance(err, BaseException):
            err = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        step, err = str(step), str(err or "").strip()
        room = 298 - len(step)
        msg = (step + ": " + err[-room:]) if err and room > 0 else step
        try:
            cwd = HOOK_ERR.get("cwd") or _he_os.getcwd()
        except OSError:
            cwd = ""
        rc = int(rc) if str(rc).lstrip("-").isdigit() else 1
        now = time.gmtime()
        d = _he_os.path.join(_he_os.environ.get("CLANKER_DATA") or "/data/clanker",
                             "raw", "health")
        _he_os.makedirs(d, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", now),
               "hook": str(HOOK_ERR.get("hook") or "?"),
               "session_id": str(HOOK_ERR.get("session_id") or ""), "cwd": str(cwd),
               "rc": rc, "stderr_tail": msg[:300]}
        path = _he_os.path.join(d, "hook-errors-" + time.strftime("%Y-%m-%d", now) + ".jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass
# ---- end of hook-error log --------------------------------------------------------

import json
import sys
from collections import deque


def main():
    if len(sys.argv) < 2:
        return 0
    try:
        with open(sys.argv[1], errors="ignore") as f:
            tail = deque(f, maxlen=400)
    except Exception as e:
        hook_err(1, "read transcript", e)
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
    try:
        rc = main()
    except Exception as e:                  # our bug: print nothing, log it
        hook_err(1, "main", e)
        rc = 0
    sys.exit(rc)
