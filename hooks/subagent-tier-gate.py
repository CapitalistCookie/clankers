#!/usr/bin/env python3
"""PreToolUse gate on Agent dispatches (GLOBAL) — operator ruling 2026-07-05:
subagents run on an EXPLICITLY chosen model (default choice: opus; orchestration
itself may be fable or opus). An Agent call with NO model field silently inherits
whatever the parent runs — exactly the drift the ruling forbids — so this gate
fail-closes on missing `model` and tells the model how to fix the call.

Task tool calls and non-Agent tools pass through untouched. Never crashes the
session: any internal error exits 0 (fail-open on OUR bugs, fail-closed only on
the designed condition) and lands in the hook-error log."""
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


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        hook_err(1, "parse hook input", e)
        return 0
    if not isinstance(data, dict):
        hook_err(1, "parse hook input", "payload is not a JSON object")
        return 0
    HOOK_ERR.update(session_id=str(data.get("session_id") or ""), cwd=str(data.get("cwd") or ""))
    tool = data.get("tool_name", "")
    if tool != "Agent":
        return 0
    ti = data.get("tool_input") or {}
    model = (ti.get("model") or "").strip()
    if model:
        return 0  # explicit choice made — ruling satisfied (opus expected for subagents)
    sys.stderr.write(
        "subagent-tier-gate: Agent dispatch has NO explicit `model` — operator ruling "
        "(2026-07-05, memory feedback-subagent-model-tier): every subagent dispatch "
        "carries an explicit model; use `model: \"opus\"` unless this lane genuinely "
        "needs fable (harness/governance-quality work). Re-issue the Agent call with "
        "the model field set.\n")
    return 2


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:                  # our bug: fail open, visibly
        hook_err(1, "main", e)
        rc = 0
    sys.exit(rc)
