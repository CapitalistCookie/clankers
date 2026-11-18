#!/usr/bin/env python3
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
"""TeammateIdle hook (global rule 30): record that a teammate went idle.

Claude Code fires TeammateIdle in the LEAD's process each time a teammate is
about to go idle. An idle teammate wakes on every message sent to it and sends
its whole context again, so a finished teammate must not stay alive unnoticed.
This hook only records; idle-agent-gate.py (Stop, main conversation) reads the
records and asks the lead to TaskStop.

Record: $CLANKER_DATA/raw/idle-gate/<team_name>/<teammate_name>.json
  {idle_at, idle_ts, done, asked: false, agent_id, lead_session_id, transcript}
done is true when the final non-empty line of the teammate's last assistant
text starts with AGENT-DONE or AGENT-HANDED-OFF. The text is read with
last-assistant-msg.py (same directory) from the teammate's own transcript,
<transcript_path minus .jsonl>/subagents/agent-<agent_id>.jsonl (the payload's
transcript_path is the lead's).

A new record replaces the old one, so asked goes back to false for each new
idle period. The hook always exits 0, prints nothing and never blocks a
teammate (exit 2 would send stderr to the teammate and keep it working)."""
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
import os
import re
import subprocess
import sys
import time

DONE_RE = re.compile(r"^\s*(AGENT-DONE|AGENT-HANDED-OFF)\b")
SAFE = re.compile(r"[^A-Za-z0-9_.@-]")


def safe_name(s):
    return SAFE.sub("_", str(s or ""))[:120]


def data_root():
    return os.path.join(os.environ.get("CLANKER_DATA") or "/data/clanker", "raw", "idle-gate")


def teammate_transcript(lead_transcript, agent_id):
    if not lead_transcript or not agent_id:
        return ""
    base = lead_transcript[:-6] if lead_transcript.endswith(".jsonl") else lead_transcript
    return os.path.join(base, "subagents", "agent-" + str(agent_id) + ".jsonl")


def last_text(path):
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last-assistant-msg.py")
    r = subprocess.run([sys.executable, helper, path], capture_output=True, text=True, timeout=8)
    return r.stdout


def is_done(text):
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return bool(lines) and bool(DONE_RE.match(lines[-1]))


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-" + str(os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def main():
    raw = sys.stdin.read()
    try:
        p = json.loads(raw)
    except Exception as e:
        hook_err(1, "parse payload", e)
        return 0
    if not isinstance(p, dict):
        hook_err(1, "parse payload", "payload is not an object")
        return 0
    HOOK_ERR["session_id"] = str(p.get("session_id") or "")
    HOOK_ERR["cwd"] = str(p.get("cwd") or "")
    team, name = safe_name(p.get("team_name")), safe_name(p.get("teammate_name"))
    if not team or not name:
        hook_err(1, "payload", "no team_name or teammate_name")
        return 0
    tpath = teammate_transcript(str(p.get("transcript_path") or ""), p.get("agent_id"))
    done = False
    if tpath and os.path.isfile(tpath):
        try:
            done = is_done(last_text(tpath))
        except Exception as e:
            hook_err(1, "read last message", e)
    else:
        hook_err(1, "teammate transcript", "not found: " + tpath)
    now = time.time()
    rec = {"idle_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "idle_ts": now,
           "done": done, "asked": False, "agent_id": str(p.get("agent_id") or ""),
           "lead_session_id": HOOK_ERR["session_id"], "transcript": tpath}
    try:
        write_json(os.path.join(data_root(), team, name + ".json"), rec)
    except Exception as e:
        hook_err(1, "write record", e)
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        hook_err(1, "main", e)
    sys.exit(0)
