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
"""
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
    except Exception:
        return 0
    if (payload.get('tool_name') or '') not in ('Bash', ''):
        return 0
    cmd = (payload.get('tool_input') or {}).get('command') or ''
    if 'ssh' not in cmd or not re.search(r'-[LD]\s*\S', cmd):
        return 0
    want = local_forward_ports(cmd)
    if not want:
        return 0
    try:
        out = subprocess.run(['ss', '-ltnpH'], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return 0  # never block work on a probe failure
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
    sys.exit(main())
