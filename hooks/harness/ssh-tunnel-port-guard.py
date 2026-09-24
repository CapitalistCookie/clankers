#!/usr/bin/env python3
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
"""PreToolUse(Bash) guard — NEVER bind an ssh local-forward onto a port already in use.

Incident 2026-06-14: a headless-render tunnel `ssh -L 8899:127.0.0.1:80 <ct117>` reused
port 8899, which is clanker's dashboard port (clanker/lib/serve.py on 127.0.0.1:8899).
cloudflared's clanker ingress (-> localhost:8899) then forwarded to the strategy SPA on
CT117, so clanker.genericnondescriptwebsite.com served the wrong dashboard until the
tunnel was killed. This makes that mechanically impossible: any `ssh -L/-D` whose LOCAL
bind port is already LISTENing on this box is DENIED with the conflicting process named.

Output: a PreToolUse deny decision (permissionDecision: deny).
Everything else passes silently. Pick a free high port, or kill the stale listener first.
An internal failure fails open and lands in the hook-error log.
"""
# ---- hook-error log: the same block in every clanker python hook ----------------
# A failure that the hook swallows (the hook stays fail-open for the session)
# appends one JSON line {ts, hook, session_id, cwd, rc, stderr_tail} to
# $CLANKER_DATA/raw/health/hook-errors-<UTC day>.jsonl (default /data/clanker),
# where `clanker doctor --harness` counts it. hook_err never raises, never
# blocks and never writes to stdout. Usage: hook_err(rc, step, error text or
# exception); stderr_tail is "<step>: " plus the end of the text (of the
# traceback, for an exception), 300 characters at most. Set
# HOOK_ERR["session_id"] and HOOK_ERR["cwd"] once the payload is parsed.
import os as _he_os
import sys as _he_sys

HOOK_ERR = {"hook": (_he_os.path.basename(_he_sys.argv[0]) if _he_sys.argv
                     and _he_sys.argv[0] not in ("", "-", "-c") else "?"),
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
import re
import subprocess
import sys


def local_forward_ports(cmd: str) -> set[int]:
    ports: set[int] = set()
    # -L [bind:]localport:host:hostport   (split by ':' -> 3 fields = port first, 4 = bind then port)
    for m in re.finditer(r'-L\s*(\S+)', cmd):
        parts = m.group(1).split(':')
        lp = parts[0] if len(parts) == 3 else (parts[1] if len(parts) == 4 else None)
        if lp and lp.isdigit():
            ports.add(int(lp))
    # -D [bind:]port  (dynamic SOCKS) — local bind too
    for m in re.finditer(r'-D\s*(\S+)', cmd):
        lp = m.group(1).split(':')[-1]
        if lp.isdigit():
            ports.add(int(lp))
    return ports


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        hook_err(1, "parse hook input", e)
        return 0
    if not isinstance(payload, dict):
        hook_err(1, "parse hook input", "payload is not a JSON object")
        return 0
    HOOK_ERR.update(session_id=str(payload.get("session_id") or ""), cwd=str(payload.get("cwd") or ""))
    if (payload.get('tool_name') or '') not in ('Bash', ''):
        return 0
    cmd = (payload.get('tool_input') or {}).get('command') or ''
    if 'ssh' not in cmd or not re.search(r'-[LD]\s*\S', cmd):
        return 0
    want = local_forward_ports(cmd)
    if not want:
        return 0
    try:
        r = subprocess.run(['ss', '-ltnpH'], capture_output=True, text=True, timeout=5)
    except Exception as e:
        hook_err(1, "ss -ltnpH (port check skipped)", e)
        return 0  # never block work on a probe failure
    if r.returncode != 0:
        hook_err(r.returncode, "ss -ltnpH (port check skipped)", r.stderr)
        return 0
    out = r.stdout
    busy: dict[int, str] = {}
    for ln in out.splitlines():
        m = re.search(r':(\d+)\s', ln)
        if m:
            busy.setdefault(int(m.group(1)), ln.strip())
    clash = sorted(p for p in want if p in busy)
    if clash:
        who = '\n'.join(f"  :{p} -> {busy[p]}" for p in clash)
        print(json.dumps({'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': (
                f"BLOCKED: ssh local-forward would bind port(s) {clash} ALREADY in LISTEN use on "
                f"this box — reusing an in-use port hijacks that service (2026-06-14: -L 8899 "
                f"hijacked clanker -> served the strategy dashboard). Pick a FREE high port "
                f"(e.g. 17000-65000), or kill the stale listener first.\nIn use:\n{who}"),
        }}))
    return 0


if __name__ == '__main__':
    try:
        rc = main()
    except Exception as e:                  # our bug: fail open, visibly
        hook_err(1, "main", e)
        rc = 0
    sys.exit(rc)
