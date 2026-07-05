#!/usr/bin/env bash
# SessionStart hook (GLOBAL — all projects) — surface usage/rate-limit-interrupted
# subagents queued for auto-resume (by subagent-resume-detect.py), so the parent
# re-dispatches them — STAGED in one batch — now that the session has resumed and
# the limit has (presumably) cleared. Idempotent + non-fatal.
#
# PROJECT-SCOPED (2026-06-20): only surfaces entries belonging to the CURRENT project
# (cwd under $CLAUDE_PROJECT_DIR / $PWD), so a killed agent from project A never nags
# an unrelated project B's session start. (Before this fix a global pile-up of 27 stale
# one project's workflow-agent entries nagged every project. The detector now also excludes
# workflow subagents, which self-heal via the workflow's own withRetry — see
# subagent-resume-detect.py — so this queue should stay small + project-local.)
set -u
Q="$HOME/.claude/agent_resume_queue.jsonl"
[ -s "$Q" ] || exit 0
HERE="${CLAUDE_PROJECT_DIR:-$PWD}"

# count pending entries belonging to THIS project
N=$(python3 - "$Q" "$HERE" <<'PY' 2>/dev/null || echo 0
import json,sys
q,here=sys.argv[1],(sys.argv[2] if len(sys.argv)>2 else "")
n=0
for line in open(q, errors="ignore"):
    line=line.strip()
    if not line: continue
    try: e=json.loads(line)
    except Exception: continue
    if e.get("status")!="pending": continue
    cwd=e.get("cwd","") or ""
    if here and cwd and not cwd.startswith(here): continue   # project-scoped
    n+=1
print(n)
PY
)
[ "${N:-0}" -gt 0 ] || exit 0

echo "════════ AGENT AUTO-RESUME QUEUE ════════"
echo "$N subagent(s) (THIS project) were KILLED MID-TASK by a usage/rate limit and are queued for re-dispatch."
echo "ACTION (now that the session resumed + the limit cleared): re-dispatch them — STAGED"
echo "together in ONE batch (alongside any new agents you're launching). CONTEXT-AWARE re-dispatch:"
echo "FIRST read the CURRENT repo/state (git log, what's already on main + the entry's 'branch'),"
echo "then re-dispatch each agent told EXACTLY what its partial work already did, so it COMPLETES"
echo "from there — NEVER restart from scratch. Then CLEAR the queue:  : > ~/.claude/agent_resume_queue.jsonl"
echo "NOTE: run_in_background agents do NOT fire SubagentStop; entries with source=background-parent"
echo "were queued by the parent from a task-notification — treat them identically."
echo "Queued tasks:"
python3 - "$Q" "$HERE" <<'PY' 2>/dev/null || true
import json,sys
q,here=sys.argv[1],(sys.argv[2] if len(sys.argv)>2 else "")
for line in open(q, errors="ignore"):
    line=line.strip()
    if not line: continue
    try: e=json.loads(line)
    except Exception: continue
    if e.get("status")!="pending": continue
    cwd=e.get("cwd","") or ""
    if here and cwd and not cwd.startswith(here): continue   # project-scoped
    p=(e.get("prompt","") or "").replace("\n"," ")
    print(f"  • branch={e.get('branch') or 'n/a'} cwd={cwd or '?'} reset={e.get('reset_hint','?')}")
    print(f"    task: {p[:160]}{'…' if len(p)>160 else ''}")
PY
echo "═════════════════════════════════════════"
exit 0
