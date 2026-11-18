#!/usr/bin/env python3
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
"""subagent-delivery-gate: PreToolUse on Agent|Task. Every dispatch prompt
must carry a report contract, chosen by whether agent teams are on.

Teams ON (CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS truthy in the hook's env, which
is the session's merged settings env): the prompt must tell the agent to
deliver its final report via SendMessage to "main" AND make it its final
message. History: on 2026-07-28 two audits' reports sent to "team-lead" were
lost despite success receipts; only "main" arrived. On 2026-08-01 a
`run_in_background: false` dispatch was backgrounded by the harness anyway,
so a synchronous dispatch is no exemption: the contract is required on every
dispatch.

Teams OFF (operator 2026-11-18, global rule 30): an ordinary background agent
ends by itself and its task notification carries its final message, so a
SendMessage copy only wakes the lead a second time. The prompt needs only a
"final message" contract line (the words "final message"); a SendMessage
contract still passes.

Truthy is what Claude Code itself accepts for the flag: 1, true, yes, on
(trimmed, any case). Anything else, unset included, is off. A project turns
teams off with {"env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0"}} in its
.claude/settings.json (project env overrides the user settings' env).

Opt-out: the literal token NO-REPORT-NEEDED (fire-and-forget).
Fails open on every error (hook-error log)."""
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
import re
import sys

TRUTHY = ("1", "true", "yes", "on")
SEND_MAIN = re.compile(r"""to\s*[:=]?\s*["']main["']|["']main["']\s*(?:conversation)?""")
FINAL = re.compile(r"final\s+(?:report\s+)?message", re.I)
TEAMS_LINE = ('Deliver your final report via SendMessage with to: "main", AND make that '
              'report your final message.')
SOLO_LINE = ("Make your final report your final message (the task notification carries "
             "it; do not also SendMessage it).")


def teams_on(env=None):
    v = (env if env is not None else _he_os.environ).get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
    return str(v or "").strip().lower() in TRUTHY


def verdict(prompt, teams):
    """None when the prompt passes, else the block message."""
    if "NO-REPORT-NEEDED" in prompt:
        return None
    if "SendMessage" in prompt and SEND_MAIN.search(prompt):
        return None
    if not teams and FINAL.search(prompt):
        return None
    if teams:
        return ("subagent-delivery-gate: Agent dispatch without a delivery contract (agent "
                "teams are on: a teammate's reply to 'team-lead' was lost on 2026-07-28, and "
                "run_in_background: false is no exemption since 2026-08-01). Add to the "
                "prompt, verbatim:\n\n  " + TEAMS_LINE + "\n\nFor a true fire-and-forget "
                "agent, include the literal token NO-REPORT-NEEDED.\n")
    return ("subagent-delivery-gate: Agent dispatch without a report contract (agent teams "
            "are off in this session, so the task notification carries the agent's final "
            "message). Add to the prompt:\n\n  " + SOLO_LINE + "\n\nFor a true "
            "fire-and-forget agent, include the literal token NO-REPORT-NEEDED.\n")


def main():
    try:
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict):
            raise ValueError("payload is not a JSON object")
    except Exception as e:
        hook_err(1, "parse hook input", e)
        return 0
    HOOK_ERR["session_id"] = str(data.get("session_id") or "")
    HOOK_ERR["cwd"] = str(data.get("cwd") or "")
    if data.get("tool_name") not in ("Agent", "Task"):
        return 0
    ti = data.get("tool_input")
    if not isinstance(ti, dict) or "prompt" not in ti:
        return 0   # prompt is required by the Agent schema: a harness change, not a dispatch
    try:
        msg = verdict(str(ti.get("prompt") or ""), teams_on())
    except Exception as e:
        hook_err(1, "check", e)
        return 0
    if msg:
        sys.stderr.write(msg)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
