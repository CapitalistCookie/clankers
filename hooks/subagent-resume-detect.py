#!/usr/bin/env python3
"""SubagentStop hook (GLOBAL — all projects) — auto-resume subagents killed by a
usage/rate limit.

When a subagent terminates because of a rate-limit (HTTP 429 / "you've hit your
session limit") rather than finishing its task, this records it to
~/.claude/agent_resume_queue.jsonl with its ORIGINAL PROMPT + worktree branch +
reset-time hint. The parent (main loop) re-dispatches every pending entry —
STAGED together in one batch — once the limit clears (surfaced by
agent-resume-surface.sh on the next SessionStart). This turns a silent loss of
parallel work into an automatic retry.

Never blocks the agent flow — exits 0 on anything unexpected (a hook that breaks
the run is worse than a missed resume).

CAVEAT — run_in_background agents: SubagentStop fires only for SYNCHRONOUS
subagents. A `run_in_background: true` agent completes via a task-notification
(NOT SubagentStop), so this hook never runs for it. For those the PARENT (main
loop) must detect the limit signature in the notification's <result> and either
(a) re-dispatch immediately if capacity is back, or (b) call this script with
`--queue-background --result-file <f> --prompt-file <f> --cwd <d> --branch <b>`
to persist it for the next session. EITHER WAY the re-dispatch must be
CONTEXT-AWARE — read the current repo/state and COMPLETE the agent's partial work
(committed on its branch), never restart from scratch.

Usage:
  (SubagentStop hook)  echo '<event-json>' | subagent-resume-detect.py
  (parent, background) subagent-resume-detect.py --queue-background \
                         --result-file <notif-output> --prompt-file <orig-prompt> \
                         --cwd <agent-cwd> --branch <worktree-branch>
"""
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

QUEUE = Path(os.path.expanduser("~/.claude/agent_resume_queue.jsonl"))
# rate-limit / session-limit signatures (transcript JSON + the rendered text)
LIMIT_SIGNS = ('"error":"rate_limit"', '"apiErrorStatus":429', '"status":429',
               "hit your session limit", "usage limit", "rate_limit_error",
               "Overloaded", "overloaded_error",
               # server-transient signature (added 2026-06-14): "temporarily limiting requests
               # (not your usage limit) Rate limited" was silently dropping subagents — NOT matched by
               # the usage-limit signs above; clears in seconds-to-minutes, so the resume/queue should fire.
               "temporarily limiting", "not your usage limit", "Rate limited",
               '"apiErrorStatus":529', '"status":529', '"apiErrorStatus":503', '"status":503',
               "overloaded", "service_unavailable")


def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return None


_CTX_BLOCK_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>"
    r"|<local-command-caveat>.*?</local-command-caveat>"
    r"|<command-name>.*?</command-name>"
    r"|<command-message>.*?</command-message>"
    r"|<command-args>.*?</command-args>"
    r"|<local-command-stdout>.*?</local-command-stdout>",
    re.DOTALL,
)


def _extract_prompt(transcript_text):
    """The agent's task = the FIRST user message that has SUBSTANTIVE content once injected
    session-context is stripped. <system-reminder> / slash-command wrappers / local-command
    caveats are session context, NOT the task, and are removed before the substantive-check;
    a turn that is ONLY context is skipped. (Bugs fixed 2026-06-14: (1) the 22-entry
    <local-command-caveat> flood; (2) the runaway-rescue degenerate entries that captured a bare
    <system-reminder> "session named X" block AS the task — the OLD SKIP list matched
    <command-*>/caveat wrappers but NOT <system-reminder>, and only skipped a turn if it CONTAINED
    a marker rather than stripping the marker and re-checking for real content.)"""
    for line in transcript_text.splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") == "user" or rec.get("role") == "user":
            msg = rec.get("message", rec)
            content = msg.get("content") if isinstance(msg, dict) else None
            text = None
            if isinstance(content, str) and content.strip():
                text = content
            elif isinstance(content, list):
                parts = [c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text"]
                joined = "\n".join(p for p in parts if p)
                if joined.strip():
                    text = joined
            if not text:
                continue
            cleaned = _CTX_BLOCK_RE.sub("", text)
            if "Caveat: The messages below" in cleaned:
                cleaned = ""
            cleaned = cleaned.strip()
            if len(cleaned) < 20:
                continue  # pure session-context (reminder / caveat / command) — keep looking
            return cleaned
    return ""


def _find_reset(blob):
    m = re.search(r"resets\s+([0-9]{1,2}:[0-9]{2}\s*[apAP]?[mM]?\s*\(?UTC\)?)", blob)
    return m.group(1).strip() if m else ""


def _branch_from_cwd(cwd):
    bm = re.search(r"agent-([a-z0-9]{6,})", cwd or "")
    return f"worktree-agent-{bm.group(1)}" if bm else ""


def _queue_entry(prompt, branch, cwd, reset, tp, source):
    """Dedupe-check + append under an exclusive flock on QUEUE.lock — the same
    lock agent-resume-surface.sh --clear takes for its scoped rewrite. Without
    it a mass resume (the 2026-07-22 host-OOM killed ~49 sessions at once) can
    interleave a clear's read-filter-replace with this read-dedupe-append and
    drop or duplicate entries."""
    entry = {
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "pending", "reason": "usage/rate limit",
        "reset_hint": reset or "unknown", "branch": branch, "cwd": cwd or "",
        "transcript_path": tp or "", "source": source,
        "prompt": (prompt or "")[:8000],
    }
    try:
        QUEUE.parent.mkdir(parents=True, exist_ok=True)
        with open(str(QUEUE) + ".lock", "a") as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX)
            except Exception:
                pass  # locking is best-effort (exotic fs) — never block the write
            # DEDUPE (2026-07-05): the same limit-killed agent can trip this hook
            # repeatedly (retries, staged re-dispatches that die on the same limit)
            # — before this check one ISD task stacked NINE identical pending
            # entries. Same (cwd, prompt) already pending → don't append a copy.
            try:
                if QUEUE.exists():
                    key = (cwd or "", (prompt or "")[:8000])
                    for line in QUEUE.read_text(errors="ignore").splitlines():
                        try:
                            e = json.loads(line)
                        except Exception:
                            continue
                        if e.get("status") == "pending" and (e.get("cwd", ""), e.get("prompt", "")) == key:
                            return True  # identical pending entry exists — queued state already correct
            except Exception:
                pass  # dedupe is best-effort; never block the queue write
            with QUEUE.open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        return False
    return True


def _arg(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else ""


def _queue_background(argv):
    """Parent path for run_in_background agents (which never fire SubagentStop) —
    persist a limit-killed background subagent for the next session (or the parent
    re-dispatches now if capacity is back)."""
    result_file, prompt_file = _arg(argv, "--result-file"), _arg(argv, "--prompt-file")
    cwd, branch = _arg(argv, "--cwd"), _arg(argv, "--branch")
    rtext = Path(result_file).read_text(errors="ignore") if result_file and os.path.exists(result_file) else ""
    prompt = Path(prompt_file).read_text(errors="ignore") if prompt_file and os.path.exists(prompt_file) else ""
    blob = rtext + "\n" + " ".join(argv)
    if not any(s in blob for s in LIMIT_SIGNS):
        sys.stderr.write("[agent-resume] --queue-background: no limit signature — not queued\n")
        return 0
    reset = _find_reset(blob)
    if _queue_entry(prompt, branch or _branch_from_cwd(cwd), cwd, reset, result_file, "background-parent"):
        sys.stderr.write(f"[agent-resume] background subagent limit QUEUED (reset {reset or '?'}, "
                         f"branch={branch or 'n/a'}). Re-dispatch CONTEXT-AWARE (complete, don't restart).\n")
    return 0


def main():
    # run_in_background agents don't fire SubagentStop — the parent calls this
    # path instead (see the module docstring CAVEAT).
    if "--queue-background" in sys.argv:
        return _queue_background(sys.argv)
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tp = _first(data, "transcript_path", "transcriptPath")
    cwd = _first(data, "cwd") or ""
    # WORKFLOW EXCLUSION (2026-06-20): a Workflow-tool subagent is already handled by the
    # workflow's OWN limit-resilience (withRetry: 6 escalating nudges) and the workflow re-runs
    # as a unit (resumeFromRunId) — so queuing it for PARENT re-dispatch is wrong (the parent
    # re-runs the workflow, not an individual workflow agent) and was the sole source of a 27-entry
    # false "killed subagent" pile-up during heavy multi-agent research waves. Skip any agent whose transcript
    # lives under a workflow run dir.
    if tp and "/subagents/workflows/" in tp:
        return 0
    text = ""
    if tp and os.path.exists(tp):
        try:
            text = Path(tp).read_text(errors="ignore")
        except Exception:
            text = ""
    blob = text[-16000:] + "\n" + json.dumps(data)
    if not any(s in blob for s in LIMIT_SIGNS):
        return 0  # normal completion — nothing to resume

    reset = _find_reset(blob)
    branch = _branch_from_cwd(cwd)
    _queue_entry(_extract_prompt(text), branch, cwd, reset, tp, "subagent-stop")

    # SURFACE the rescue VISIBLY (2026-06-15). Previously this hook only wrote stderr, so a subagent
    # that paused on a TRANSIENT throttle showed in the workflow display as a bare 'failed' with no sign
    # the throttle was caught + queued — indistinguishable from a real death. Emit a systemMessage that
    # DISTINGUISHES a transient throttle (auto-recovers in seconds–minutes) from a HARD usage/session
    # limit (waits for reset), so the display reflects "it tried / it's recovering", not "it failed".
    hard = ("hit your session limit" in blob
            or ("usage limit" in blob and "not your usage limit" not in blob)
            or "quota" in blob)
    if hard:
        msg = (f"⛔ usage/session limit — a subagent was queued for staged re-dispatch "
               f"(resets {reset or '?'}); the run continues with the rest.")
        ctx = ("A subagent hit a HARD usage/session limit and was queued for re-dispatch after reset. "
               "Informational only — the run continues.")
    else:
        msg = ("⏳ transient API throttle — a subagent paused and was queued for auto re-dispatch "
               "(recovers in seconds–minutes; NOT a usage-limit failure). A 'failed' marker here is a "
               "throttle blip, not a death.")
        ctx = ("A subagent hit a TRANSIENT throttle (temporarily-limiting / overloaded / 5xx) and was "
               "queued; it auto-recovers. Informational only — not a real failure, take no action.")
    try:
        print(json.dumps({"systemMessage": msg,
                          "hookSpecificOutput": {"hookEventName": "SubagentStop",
                                                 "additionalContext": ctx}}))
    except Exception:
        pass
    sys.stderr.write(f"[agent-resume] subagent limit ({'hard' if hard else 'transient'}, resets "
                     f"{reset or '?'}) queued (branch={branch or 'n/a'}). See {QUEUE}.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
