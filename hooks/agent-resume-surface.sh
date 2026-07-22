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
#
# CLEAR IS PROJECT-SCOPED TOO (2026-07-22): the banner used to instruct a GLOBAL
# truncate (: > queue) — display was scoped but the clear wiped every OTHER
# project's pending entries. Proven live by the 07-22 host-OOM mass restart:
# ~49 sessions resumed at once and the first session's clear erased entries
# belonging to projects that hadn't processed theirs yet. Now `--clear` removes
# only THIS project's pending entries (flock'd, atomic rewrite) and the banner
# instructs exactly that command. Both writers share the same $Q.lock flock —
# see _queue_entry in subagent-resume-detect.py.
set -u
Q="${CLANKER_RESUME_QUEUE:-$HOME/.claude/agent_resume_queue.jsonl}"
HERE="${CLAUDE_PROJECT_DIR:-$PWD}"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

# One shared shown/cleared predicate (mode = count | list | clear) so display and
# clear can never drift on which entries belong to this project. An entry is OURS
# iff pending AND (its cwd equals / lives under $HERE, or either side is unscoped).
# Path match is component-wise: /proj/ab is NOT under /proj/a.
_scan() {  # $1 = mode
python3 - "$1" "$Q" "$HERE" <<'PY'
import json, os, sys
mode, q, here = sys.argv[1], sys.argv[2], sys.argv[3]

def mine(e):
    if e.get("status") != "pending":
        return False
    cwd = e.get("cwd", "") or ""
    if not (here and cwd):
        return True          # unscoped entry or unscoped session: surface everywhere
    h = here.rstrip("/")
    return cwd == h or cwd.startswith(h + "/")

try:
    lines = open(q, errors="ignore").read().splitlines()
except Exception:
    lines = []

if mode == "clear":
    keep, dropped = [], 0
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            e = json.loads(s)
        except Exception:
            keep.append(s)   # unparseable lines are not ours to destroy
            continue
        if mine(e):
            dropped += 1
        else:
            keep.append(s)
    tmp = q + ".tmp"
    with open(tmp, "w") as f:
        f.write("".join(l + "\n" for l in keep))
    os.replace(tmp, q)
    word = "entry" if dropped == 1 else "entries"
    print(f"[agent-resume] cleared {dropped} pending {word} for {here}; "
          f"{len(keep)} line(s) kept (other projects / non-pending)")
    sys.exit(0)

n = 0
for line in lines:
    s = line.strip()
    if not s:
        continue
    try:
        e = json.loads(s)
    except Exception:
        continue
    if not mine(e):
        continue
    n += 1
    if mode == "list":
        p = (e.get("prompt", "") or "").replace("\n", " ")
        print(f"  • branch={e.get('branch') or 'n/a'} cwd={e.get('cwd') or '?'} "
              f"reason={e.get('reason') or '?'} reset={e.get('reset_hint', '?')}")
        print(f"    task: {p[:160]}{'…' if len(p) > 160 else ''}")
if mode == "count":
    print(n)
PY
}

if [ "${1:-}" = "--clear" ]; then
  [ -f "$Q" ] || { echo "[agent-resume] queue absent — nothing to clear"; exit 0; }
  exec 9>>"$Q.lock" 2>/dev/null || exit 0
  flock -w 5 9 || { echo "[agent-resume] clear: queue lock busy — re-run" >&2; exit 1; }
  _scan clear
  exit $?
fi

if [ "${1:-}" = "--selftest" ]; then
  T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
  export CLANKER_RESUME_QUEUE="$T/q.jsonl"
  {
    echo '{"status":"pending","cwd":"/proj/a/x","prompt":"task A alpha","reason":"usage/rate limit","reset_hint":"9:00 UTC"}'
    echo '{"status":"pending","cwd":"/proj/b","prompt":"task B bravo","reason":"usage/rate limit"}'
    echo '{"status":"pending","cwd":"/proj/ab","prompt":"task AB prefix-collision"}'
    echo '{"status":"pending","cwd":"","prompt":"task C no-cwd"}'
    echo '{"status":"done","cwd":"/proj/a/x","prompt":"finished thing"}'
    echo 'not json garbage line'
  } > "$CLANKER_RESUME_QUEUE"
  pass=0; fail=0
  ok(){ pass=$((pass+1)); }
  bad(){ fail=$((fail+1)); echo "  FAIL: $1"; }
  out="$(CLAUDE_PROJECT_DIR=/proj/a bash "$SELF")"
  echo "$out" | grep -q "^2 subagent" && ok || bad "display count (want 2: /proj/a/x + empty-cwd)"
  echo "$out" | grep -q "task A alpha" && ok || bad "shows this project's task"
  echo "$out" | grep -q "task C no-cwd" && ok || bad "shows unscoped (empty-cwd) task"
  echo "$out" | grep -q "task B bravo" && bad "leaks other project's task" || ok
  echo "$out" | grep -q "task AB prefix" && bad "prefix collision: /proj/ab shown under /proj/a" || ok
  echo "$out" | grep -q -- "--clear" && ok || bad "banner instructs scoped --clear"
  echo "$out" | grep -q ": > " && bad "banner still instructs global truncate" || ok
  CLAUDE_PROJECT_DIR=/proj/a bash "$SELF" --clear >/dev/null || bad "clear exited nonzero"
  grep -q "task B bravo" "$CLANKER_RESUME_QUEUE" && ok || bad "clear dropped other project's entry"
  grep -q "task AB prefix" "$CLANKER_RESUME_QUEUE" && ok || bad "clear dropped prefix-collision neighbor"
  grep -q "task A alpha" "$CLANKER_RESUME_QUEUE" && bad "clear left this project's pending" || ok
  grep -q "task C no-cwd" "$CLANKER_RESUME_QUEUE" && bad "clear left unscoped pending" || ok
  grep -q "finished thing" "$CLANKER_RESUME_QUEUE" && ok || bad "clear dropped a non-pending line"
  grep -q "not json garbage" "$CLANKER_RESUME_QUEUE" && ok || bad "clear dropped an unparseable line"
  out2="$(CLAUDE_PROJECT_DIR=/proj/b bash "$SELF")"
  echo "$out2" | grep -q "task B bravo" && ok || bad "other project no longer surfaces its entry"
  total=$((pass+fail))
  if [ "$fail" -eq 0 ]; then echo "selftest: $pass/$total PASS"; exit 0
  else echo "selftest: $pass/$total pass — $fail FAILURE(S)"; exit 1; fi
fi

[ -s "$Q" ] || exit 0
N="$(_scan count 2>/dev/null || echo 0)"
[ "${N:-0}" -gt 0 ] 2>/dev/null || exit 0

echo "════════ AGENT AUTO-RESUME QUEUE ════════"
echo "$N subagent(s) (THIS project) were interrupted MID-TASK and are queued for re-dispatch"
echo "(per-entry 'reason' says why — usually a usage/rate limit, sometimes a host-level kill)."
echo "ACTION (now that the session resumed + the limit cleared): re-dispatch them — STAGED"
echo "together in ONE batch (alongside any new agents you're launching). CONTEXT-AWARE re-dispatch:"
echo "FIRST read the CURRENT repo/state (git log, what's already on main + the entry's 'branch'),"
echo "then re-dispatch each agent told EXACTLY what its partial work already did, so it COMPLETES"
echo "from there — NEVER restart from scratch; if state shows a task ALREADY COMPLETE, skip it."
echo "Then clear THIS project's entries (other projects' entries MUST survive — never truncate the file):"
echo "  bash $SELF --clear"
echo "NOTE: run_in_background agents do NOT fire SubagentStop; entries with source=background-parent"
echo "were queued by the parent from a task-notification — treat them identically."
echo "Queued tasks:"
_scan list 2>/dev/null || true
echo "═════════════════════════════════════════"
exit 0
