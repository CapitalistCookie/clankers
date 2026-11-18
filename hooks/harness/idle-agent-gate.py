#!/usr/bin/env python3
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
"""Stop hook, main conversation only (global rule 30): make the lead stop
finished or long-idle teammates.

An idle teammate wakes on every message sent to it and sends its whole context
again. A hook cannot stop a teammate (a TeammateIdle {"continue": false} leaves
it listed as idle); only the lead's TaskStop can. So this gate blocks the
lead's Stop, once per idle period, and names the teammates to stop.

Inputs:
  * team config: <CLAUDE_CONFIG_DIR or ~/.claude>/teams/<team>/config.json,
    members[].name (TaskStop removes the member; 'team-lead' is the lead).
  * records from teammate-idle-record.py:
    $CLANKER_DATA/raw/idle-gate/<team>/<name>.json {idle_at, idle_ts, done, asked}.

The lead's team, first hit wins:
  1. the team whose config leadSessionId equals the payload's session_id;
  2. teamName in the lead's teammate sidecars
     (<transcript_path minus .jsonl>/subagents/*.meta.json), if all agree.
     A resumed lead keeps its first session's team, so 1 misses it;
  3. a record whose lead_session_id equals the session_id;
  4. session-<first 8 of session_id>.

Flags each listed member with a record not yet asked about:
  * done records, at once;
  * not-done records idle longer than IDLE_GATE_MINUTES (default 30).
Skipped: a record older than the member's joinedAt (a new teammate reused the
name), and a teammate whose own transcript changed more than 15 s after
idle_at (it was woken and is working again).
If any is flagged: set asked=true on each and print {"decision": "block"}.
Never blocks when stop_hook_active is true, in a subagent (payload agent_id),
or when IDLE_GATE_OFF=1. Fails open on every error (hook-error log)."""
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
import sys
import time

ACTIVE_SLACK_S = 15


def data_root():
    return os.path.join(os.environ.get("CLANKER_DATA") or "/data/clanker", "raw", "idle-gate")


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def team_config(teams, team):
    path = os.path.join(teams, team, "config.json")
    if not os.path.isfile(path):
        return None
    try:
        cfg = load(path)
    except Exception as e:
        hook_err(1, "read team config " + team, e)
        return None
    return cfg if isinstance(cfg, dict) else None


def find_team(sid, transcript):
    """(team name, config) of the lead's team, or (None, None)."""
    teams = os.path.join(config_dir(), "teams")
    names = sorted(os.listdir(teams)) if os.path.isdir(teams) else []
    for t in names:                                            # 1. leadSessionId
        cfg = team_config(teams, t)
        if cfg and cfg.get("leadSessionId") == sid:
            return t, cfg
    if transcript:                                             # 2. sidecars
        base = transcript[:-6] if transcript.endswith(".jsonl") else transcript
        sub = os.path.join(base, "subagents")
        seen = set()
        if os.path.isdir(sub):
            for n in os.listdir(sub):
                if n.endswith(".meta.json"):
                    try:
                        tn = load(os.path.join(sub, n)).get("teamName")
                    except Exception:
                        continue
                    if tn:
                        seen.add(tn)
        if len(seen) == 1:
            t = seen.pop()
            cfg = team_config(teams, t)
            if cfg:
                return t, cfg
    root = data_root()                                         # 3. records
    if os.path.isdir(root):
        for t in sorted(os.listdir(root)):
            d = os.path.join(root, t)
            if not os.path.isdir(d):
                continue
            for n in os.listdir(d):
                if not n.endswith(".json"):
                    continue
                try:
                    if load(os.path.join(d, n)).get("lead_session_id") == sid:
                        cfg = team_config(teams, t)
                        if cfg:
                            return t, cfg
                except Exception:
                    pass
                break                                          # one record per team is enough
    t = "session-" + sid[:8]                                   # 4. first life
    cfg = team_config(teams, t)
    return (t, cfg) if cfg else (None, None)


def fmt_age(sec):
    m = int(sec // 60)
    return f"{m}m" if m < 120 else f"{m // 60}h{m % 60:02d}m"


def flagged(team, cfg, now, limit_s):
    """[(name, record path, record, state, age)] to ask about."""
    out = []
    d = os.path.join(data_root(), team)
    if not os.path.isdir(d):
        return out
    for m in cfg.get("members") or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "")
        if not name or name == "team-lead":
            continue
        path = os.path.join(d, name + ".json")
        if not os.path.isfile(path):
            continue
        try:
            rec = load(path)
            ts = float(rec["idle_ts"])
        except Exception as e:
            hook_err(1, "read record " + name, e)
            continue
        if rec.get("asked"):
            continue
        joined = m.get("joinedAt")
        if isinstance(joined, (int, float)) and joined / 1000.0 > ts:
            continue                                           # record of an earlier teammate
        tp = rec.get("transcript") or ""
        try:
            if tp and os.path.getmtime(tp) > ts + ACTIVE_SLACK_S:
                continue                                       # woken, working again
        except OSError:
            pass
        age = now - ts
        if rec.get("done"):
            out.append((name, path, rec, "done", age))
        elif age > limit_s:
            out.append((name, path, rec, "not done", age))
    return out


def mark_asked(path, rec):
    rec["asked"] = True
    tmp = path + ".tmp-" + str(os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f)
    os.replace(tmp, path)


def main():
    if os.environ.get("IDLE_GATE_OFF") == "1":
        return 0
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
    if p.get("stop_hook_active") or p.get("agent_id"):
        return 0
    sid = HOOK_ERR["session_id"]
    if not sid:
        return 0
    try:
        limit_s = float(os.environ.get("IDLE_GATE_MINUTES") or 30) * 60
    except ValueError:
        limit_s = 1800.0
    team, cfg = find_team(sid, str(p.get("transcript_path") or ""))
    if not team:
        return 0
    hits = flagged(team, cfg, time.time(), limit_s)
    if not hits:
        return 0
    for _, path, rec, _, _ in hits:
        try:
            mark_asked(path, rec)
        except Exception as e:
            hook_err(1, "mark asked", e)
    items = "; ".join(f"{n} ({state}, idle {fmt_age(age)})" for n, _, _, state, age in hits)
    reason = ("Idle teammates still alive: " + items + ". Stop each with TaskStop <name> now "
              "(global rule 30), or say in one line why it stays.")
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        hook_err(1, "main", e)
    sys.exit(0)
