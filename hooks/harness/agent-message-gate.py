#!/usr/bin/env python3
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
"""agent-message-gate.py: PreToolUse on SendMessage, subagents and teammates
only (global rule 30). A message to a stopped or finished agent revives it and
re-sends its whole context (11-18 10:21Z: look4 -> perf revived a 452k-token
context stopped 4 h earlier).

Acts only when the payload carries agent_id; the lead's own sends are never
gated. Sends to "main", "team-lead" or the team's lead always pass.
  a. Always: block a send whose `to` names a teammate of the caller's team
     that is no longer listed in <CLAUDE_CONFIG_DIR or ~/.claude>/teams/<team>/
     config.json members (TaskStop removes the member). A teammate is a name
     with a sidecar <lead transcript minus .jsonl>/subagents/agent-a<name>-*.meta.json
     (also under the team's first lead session) or an idle-gate record
     $CLANKER_DATA/raw/idle-gate/<team>/<name>.json. Other names (another
     session, a typo) pass: they revive nothing.
  b. CLANKER_AGENT_MESSAGES=lead-only (a project's .claude/settings.json env):
     block every send except to main / team-lead / the lead.
The team: teamName in the caller's own sidecar, else the config whose
leadSessionId is the payload session_id, else session-<first 8 of it>.
Blocks with exit 2 and the reason on stderr. Fails open on every error
(hook-error log)."""
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
import glob
import json
import os
import re
import sys

LEADS = ("main", "team-lead")
AGENT_ID = re.compile(r"^a[\w.-]*-[0-9a-f]{16}$")


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def target_of(ti):
    """The addressed name: `to` (or `recipient`) without a ' [ref]' or '@team' suffix."""
    to = ti.get("to")
    if not isinstance(to, str) or not to.strip():
        to = ti.get("recipient")
    if not isinstance(to, str):
        return ""
    to = re.sub(r"\s*\[[^\]]*\]\s*$", "", to.strip())
    return to.split("@", 1)[0].strip()


def subagent_dirs(transcript, cfg):
    """The lead's sidecar dirs: this session's and the team's first lead session's."""
    out = []
    if transcript:
        base = transcript[:-6] if transcript.endswith(".jsonl") else transcript
        out.append(os.path.join(base, "subagents"))
        first = (cfg or {}).get("leadSessionId")
        if isinstance(first, str) and first:
            d = os.path.join(os.path.dirname(base), first, "subagents")
            if d not in out:
                out.append(d)
    return [d for d in out if os.path.isdir(d)]


def find_team(sid, transcript, agent_id):
    """(team, config) of the caller, or (None, None)."""
    teams = os.path.join(config_dir(), "teams")
    if not os.path.isdir(teams):
        return None, None
    names = []
    if transcript and agent_id:                               # 1. caller's own sidecar
        base = transcript[:-6] if transcript.endswith(".jsonl") else transcript
        meta = os.path.join(base, "subagents", "agent-" + agent_id + ".meta.json")
        if os.path.isfile(meta):
            try:
                tn = load(meta).get("teamName")
                if isinstance(tn, str) and tn:
                    names.append(tn)
            except Exception as e:
                hook_err(1, "read own sidecar", e)
    if sid:
        for t in sorted(os.listdir(teams)):                   # 2. leadSessionId
            p = os.path.join(teams, t, "config.json")
            try:
                if os.path.isfile(p) and load(p).get("leadSessionId") == sid:
                    names.append(t)
                    break
            except Exception:
                continue
        names.append("session-" + sid[:8])                    # 3. first life
    for t in names:
        p = os.path.join(teams, t, "config.json")
        if os.path.isfile(p):
            try:
                cfg = load(p)
            except Exception as e:
                hook_err(1, "read team config " + t, e)
                continue
            if isinstance(cfg, dict):
                return t, cfg
    return None, None


def lead_names(cfg):
    out = set(LEADS)
    la = (cfg or {}).get("leadAgentId")
    if isinstance(la, str) and la:
        out.add(la.split("@", 1)[0])
    for m in (cfg or {}).get("members") or []:
        if isinstance(m, dict) and m.get("agentType") == "team-lead" and m.get("name"):
            out.add(str(m["name"]))
    return out


def resolve_id(name, dirs):
    """An agent-id target (aperf-3e90...) -> its teammate name, else the input."""
    if not AGENT_ID.match(name):
        return name
    for d in dirs:
        p = os.path.join(d, "agent-" + name + ".meta.json")
        if os.path.isfile(p):
            try:
                n = load(p).get("name")
                if isinstance(n, str) and n:
                    return n
            except Exception as e:
                hook_err(1, "read target sidecar", e)
    return name


def was_teammate(name, team, dirs):
    for d in dirs:
        for p in glob.glob(os.path.join(d, "agent-a" + glob.escape(name) + "-*.meta.json")):
            try:
                m = load(p)
            except Exception:
                continue
            if m.get("name") == name and m.get("teamName") in (None, team):
                return True
    rec = os.path.join(os.environ.get("CLANKER_DATA") or "/data/clanker", "raw", "idle-gate",
                       team, name + ".json")
    return os.path.isfile(rec)


def verdict(p):
    """None when the send passes, else the block reason."""
    if not p.get("agent_id") or p.get("tool_name") != "SendMessage":
        return None
    ti = p.get("tool_input")
    if not isinstance(ti, dict):
        return None
    name = target_of(ti)
    if not name or name in LEADS:
        return None
    transcript = str(p.get("transcript_path") or "")
    sid = str(p.get("session_id") or "")
    lead_only = (os.environ.get("CLANKER_AGENT_MESSAGES") or "").strip().lower() == "lead-only"
    team, cfg = find_team(sid, transcript, str(p.get("agent_id")))
    if cfg is not None and name in lead_names(cfg):
        return None
    if lead_only:
        return ("Agent messages are lead-only in this project (CLANKER_AGENT_MESSAGES="
                "lead-only): send only to \"main\". Put the request for " + name +
                " in your status log or report; the lead routes it (global rule 30).")
    if cfg is None:
        return None
    dirs = subagent_dirs(transcript, cfg)
    name = resolve_id(name, dirs)
    members = {str(m.get("name")) for m in cfg.get("members") or [] if isinstance(m, dict)}
    if name in members or name in lead_names(cfg):
        return None
    if not was_teammate(name, team, dirs):
        return None
    return (name + " is stopped; a message revives it with its whole context. Put the request "
            "in your status log or report; the lead routes it (global rule 30).")


def main():
    try:
        p = json.loads(sys.stdin.read())
        if not isinstance(p, dict):
            raise ValueError("payload is not a JSON object")
    except Exception as e:
        hook_err(1, "parse hook input", e)
        return 0
    HOOK_ERR["session_id"] = str(p.get("session_id") or "")
    HOOK_ERR["cwd"] = str(p.get("cwd") or "")
    try:
        msg = verdict(p)
    except Exception as e:
        hook_err(1, "check", e)
        return 0
    if msg:
        sys.stderr.write("agent-message-gate: " + msg + "\n")
        return 2
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:
        hook_err(1, "main", e)
        rc = 0
    sys.exit(rc)
