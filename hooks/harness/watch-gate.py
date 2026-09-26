#!/usr/bin/env python3
"""PreToolUse gate on Monitor/CronCreate (GLOBAL) — the orchestrator's wake
economy (global rule 25 in ~/.claude/CLAUDE.md).

WHY (2026-09-26, colonizers): the lead session armed five per-job Monitor
watches plus a usage-band watch. Each one emitted its initial state and every
state change ("running", bands), and each event woke the lead: about twelve
wakes in 45 minutes. Every wake re-sends the lead's whole context (hundreds of
thousands of cached tokens per turn). The framework itself prescribed it
("keep two Monitor watches armed; re-arm on expiry").

WHAT IT DOES (exit 2 blocks; the stderr line goes to the model):
  a. UNBOUNDED  — a Monitor command with an endless construct (`while true`,
                  `while :`, `tail -f/-F/--follow`, `inotifywait -m`,
                  `journalctl -f`, `watch `, `for ((;;))`) and no `exit` or
                  `break` anywhere in its text. Such a watch cannot end on its
                  own terminal event. A plain text heuristic.
  b. SECOND     — a Monitor while another Monitor of the same session (and
                  the same subagent, when the payload has `agent_id`) is still
                  live. Each allowed Monitor leaves a stamp file
                  {armed, timeout_ms, description} in the stamp dir:
                    $CLAUDE_SCRATCHPAD_DIR/watch-gate/<key>/   when set, else
                    /var/tmp/claude-<uid>/watch-gate/<key>/
                  <key> = session id, plus "-<agent_id>" for a subagent. A stamp
                  older than its timeout_ms (capped at 1,800,000, the Monitor
                  cap) is expired and ignored. The hook cannot see a watch
                  that exits early or is stopped, so the refusal names the
                  reset: `python3 <this file> --reset <key>` (or remove the
                  stamp files), or start the session with WATCH_GATE_RESET=1.
  c. POLLING    — a recurring CronCreate (recurring is true by default) whose
                  shortest gap between two firings is under 30 minutes.
  d. EMITTER    — a Monitor command with an obvious state emitter
                  (`heartbeat`, `echo ... running|alive|waiting|still|band`).
                  A warning in additionalContext, not a refusal.
A one-shot CronCreate (recurring: false) is always allowed.
WATCH_GATE_OFF=1 in the session's environment turns the gate off.
This gate deletes nothing except the stamps a --reset names.

Selftest: python3 -u watch-gate.py --selftest  (runs tests/test_watch_gate.py
beside this file; run it after ANY edit here).
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
import sys
import time

RULE = "global rule 25 (orchestrator wake economy, ~/.claude/CLAUDE.md)"
MONITOR_DEFAULT_MS = 300000    # the Monitor tool's default timeout_ms
MONITOR_CAP_MS = 1800000       # the Monitor tool caps deadlines above this
MIN_CRON_PERIOD_MIN = 30       # a recurring cron firing more often is polling

UNBOUNDED = re.compile(
    r"\bwhile\s+(true|:|\[\s*1\s*\]|\(\(\s*1\s*\)\))(\s|;|$)"
    r"|\btail\b[^|;&\n]*\s(-[a-zA-Z]*[fF][a-zA-Z]*|--follow\S*)(\s|$)"
    r"|\binotifywait\b[^|;&\n]*\s(-[a-zA-Z]*m[a-zA-Z]*|--monitor)(\s|$)"
    r"|\bjournalctl\b[^|;&\n]*\s(-[a-zA-Z]*f[a-zA-Z]*|--follow)(\s|$)"
    r"|(^|[;&|(\s])watch\s"
    r"|\bfor\s*\(\(\s*;\s*;\s*\)\)")
TERMINATOR = re.compile(r"\b(exit|break)\b")
EMITTER = re.compile(
    r"\bheart_?beat\b"
    r"|\b(echo|printf)\b[^\n;|&]*\b(running|alive|waiting|still|in[- ]progress|bands?)\b",
    re.I)


# ── stamps (rule b) ──────────────────────────────────────────────────────────

def _safe(s):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(s or ""))[:120] or "nosession"


def stamp_key(data):
    key = _safe(data.get("session_id"))
    if data.get("agent_id"):
        key += "-" + _safe(data.get("agent_id"))
    return key


def stamp_root(env):
    scratch = env.get("CLAUDE_SCRATCHPAD_DIR")
    if scratch:
        return os.path.join(scratch, "watch-gate")
    return os.path.join("/var/tmp", "claude-%d" % os.getuid(), "watch-gate")


def _timeout_ms(ti):
    try:
        t = float(ti.get("timeout_ms") or MONITOR_DEFAULT_MS)
    except (TypeError, ValueError):
        t = MONITOR_DEFAULT_MS
    return int(min(max(t, 1000), MONITOR_CAP_MS))


def live_stamps(d, now):
    """[(path, stamp)] of the stamps in d that have not expired."""
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        p = os.path.join(d, n)
        try:
            with open(p) as f:
                st = json.load(f)
            if now < float(st["armed"]) + int(st["timeout_ms"]) / 1000.0:
                out.append((p, st))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def write_stamp(d, ti, now):
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "%d-%d.json" % (int(now * 1e6), os.getpid()))
    with open(p, "w") as f:
        json.dump({"armed": now, "timeout_ms": _timeout_ms(ti),
                   "description": str(ti.get("description") or "")[:200]}, f)
    return p


def reset(d):
    """Remove the stamp files in d; returns how many. Nothing else is touched."""
    n = 0
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for name in names:
        if name.endswith(".json"):
            try:
                os.remove(os.path.join(d, name))
                n += 1
            except OSError:
                pass
    return n


# ── cron (rule c) ────────────────────────────────────────────────────────────

MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
          "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
          "@hourly": "0 * * * *"}


def _field(spec, lo, hi):
    vals = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
            if step < 1:
                raise ValueError("step < 1")
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = int(part)
            b = hi if step > 1 else a
        if a < lo or b > hi or a > b:
            raise ValueError("out of range: %s" % part)
        vals.update(range(a, b + 1, step))
    return vals


def cron_min_gap(expr):
    """Shortest gap in minutes between two firings (minute and hour fields;
    the day fields only thin the days out). None when it cannot be parsed."""
    expr = MACROS.get(expr.strip().lower(), expr)
    f = expr.split()
    if len(f) != 5:
        return None
    try:
        mins = _field(f[0], 0, 59)
        hours = _field(f[1], 0, 23)
    except (ValueError, TypeError):
        return None
    times = sorted(h * 60 + m for h in hours for m in mins)
    if not times:
        return None
    if len(times) == 1:
        return 1440
    gaps = [b - a for a, b in zip(times, times[1:])]
    gaps.append(times[0] + 1440 - times[-1])
    return min(gaps)


# ── the check ────────────────────────────────────────────────────────────────

def check(data, env=None, now=None):
    """(exit code, stderr text, additionalContext text or None, stamp dir or None).
    Exit 0 with a stamp dir means: allow and record a stamp there."""
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    if env.get("WATCH_GATE_OFF") == "1":
        return 0, "", None, None
    tool = data.get("tool_name")
    ti = data.get("tool_input") or {}
    if not isinstance(ti, dict):
        return 0, "", None, None

    if tool == "CronCreate":
        recurring = ti.get("recurring", True)
        if recurring is False or str(recurring).lower() == "false":
            return 0, "", None, None
        gap = cron_min_gap(str(ti.get("cron") or ""))
        if gap is not None and gap < MIN_CRON_PERIOD_MIN:
            return 2, ("watch-gate: refused a recurring CronCreate firing every %d min "
                       "(< %d) — a polling cadence; %s. Use a one-shot "
                       "(recurring: false) at the moment something can change "
                       "(a reset, a deadline)." % (gap, MIN_CRON_PERIOD_MIN, RULE)), None, None
        return 0, "", None, None

    if tool != "Monitor":
        return 0, "", None, None

    cmd = ti.get("command")
    cmd = cmd if isinstance(cmd, str) else ""
    if cmd and UNBOUNDED.search(cmd) and not TERMINATOR.search(cmd):
        return 2, ("watch-gate: refused a Monitor that cannot end on its own event "
                   "(an endless loop/follow with no `exit`); %s. Arm ONE single-shot "
                   "watch that exits on the terminal event (a job exited, a report "
                   "file appeared, a hard threshold crossed)." % RULE), None, None

    d = os.path.join(stamp_root(env), stamp_key(data))
    if env.get("WATCH_GATE_RESET") == "1":
        reset(d)
    live = live_stamps(d, now)
    if live:
        st = live[-1][1]
        left = int(float(st["armed"]) + int(st["timeout_ms"]) / 1000.0 - now)
        return 2, ("watch-gate: refused a second Monitor — one is still armed "
                   "(\"%s\", expires in %ds); %s. Fold this condition into the one "
                   "watch. If that watch already exited or was stopped, clear it: "
                   "python3 %s --reset %s  (or remove %s/*.json, or start the "
                   "session with WATCH_GATE_RESET=1)."
                   % (st.get("description", "")[:60], max(left, 0), RULE,
                      os.path.abspath(__file__), stamp_key(data), d)), None, d

    warn = None
    if ti.get("ws"):
        warn = ("watch-gate: a ws Monitor streams every frame; each frame wakes the "
                "session — %s. Subscribe to terminal events only." % RULE)
    elif cmd and EMITTER.search(cmd):
        warn = ("watch-gate: this Monitor looks like it emits state (a heartbeat or "
                "a 'running'/band line); every line wakes the session — %s. Emit only "
                "the terminal event." % RULE)
    return 0, "", warn, d


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception as e:
        hook_err(1, "parse hook input", e)
        return 0
    if not isinstance(data, dict):
        hook_err(1, "parse hook input", "payload is not a JSON object")
        return 0
    HOOK_ERR.update(session_id=str(data.get("session_id") or ""), cwd=str(data.get("cwd") or ""))
    try:
        code, msg, warn, d = check(data)
        if code == 0 and d:
            write_stamp(d, data.get("tool_input") or {}, time.time())
    except Exception as exc:  # FAIL OPEN on the hook's own bugs
        sys.stderr.write("watch-gate: internal error, failing open: %s: %s\n"
                         % (type(exc).__name__, exc))
        hook_err(1, "check", exc)
        return 0
    if code:
        sys.stderr.write(msg + "\n")
        return code
    if warn:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "additionalContext": warn}}) + "\n")
    return 0


def _cli_reset(args):
    root = stamp_root(os.environ)
    if not args:
        sys.stderr.write("usage: watch-gate.py --reset <session key>|--all\n")
        return 1
    keys = sorted(os.listdir(root)) if args[0] == "--all" and os.path.isdir(root) else args
    total = sum(reset(os.path.join(root, _safe(k))) for k in keys)
    print("watch-gate: reset %d stamp(s) under %s" % (total, root))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        import subprocess
        t = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "test_watch_gate.py")
        sys.exit(subprocess.call([sys.executable, "-u", t]))
    if len(sys.argv) > 1 and sys.argv[1] == "--reset":
        sys.exit(_cli_reset(sys.argv[2:]))
    sys.exit(main())
