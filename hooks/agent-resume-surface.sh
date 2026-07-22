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
#
# ROOT-SCOPED v2.1 (2026-07-22, same day): the prefix rule ALSO let an ANCESTOR
# session swallow other projects — HERE=$HOME matches every cwd on the box, so
# the home session was surfaced (and with scoped clear would have DELETED) every
# project's entries. Ownership is now git-root equality (linked worktrees
# resolve to the parent repo via --git-common-dir; submodules to their own
# toplevel) — the same convention as the memory namespace (lib/memoryns.py).
# The under-$HERE prefix match survives ONLY for cwds not inside any git repo
# (scratch dirs have no scope of their own -> the nearest ancestor session owns
# them). Non-existent cwds fall back to pure path logic.
set -u
Q="${CLANKER_RESUME_QUEUE:-$HOME/.claude/agent_resume_queue.jsonl}"
HERE="${CLAUDE_PROJECT_DIR:-$PWD}"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

# One shared shown/cleared predicate (mode = count | list | clear) so display and
# clear can never drift on which entries belong to this project. An entry is OURS
# iff pending AND: same git root as $HERE (worktree-aware), OR its cwd lives
# under $HERE while inside NO git repo (scratch reclaim), OR either side is
# unscoped. Path match is component-wise: /proj/ab is NOT under /proj/a.
_scan() {  # $1 = mode
python3 - "$1" "$Q" "$HERE" <<'PY'
import json, os, subprocess, sys
mode, q, here = sys.argv[1], sys.argv[2], sys.argv[3]
_roots = {}

def _root_info(p):
    """(scope_root, is_repo) for path p. scope_root = MAIN repo toplevel (linked
    worktrees resolve to the parent repo via --git-common-dir; submodules to
    their own toplevel); the realpath itself when p is inside no git repo."""
    rp = os.path.realpath(p)
    if rp in _roots:
        return _roots[rp]
    root, is_repo = rp, False
    try:
        r = subprocess.run(
            ["git", "-C", rp, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            gd = r.stdout.strip()
            if not os.path.isabs(gd):
                gd = os.path.join(rp, gd)
            gd = os.path.realpath(gd)
            if os.path.basename(gd) == ".git":
                root, is_repo = os.path.dirname(gd), True
            else:  # submodule / detached git-dir: own toplevel
                t = subprocess.run(["git", "-C", rp, "rev-parse", "--show-toplevel"],
                                   capture_output=True, text=True, timeout=3)
                if t.returncode == 0 and t.stdout.strip():
                    root, is_repo = os.path.realpath(t.stdout.strip()), True
    except Exception:
        pass
    _roots[rp] = (root, is_repo)
    return _roots[rp]

def mine(e):
    if e.get("status") != "pending":
        return False
    cwd = e.get("cwd", "") or ""
    if not (here and cwd):
        return True          # unscoped entry or unscoped session: surface everywhere
    h = here.rstrip("/")
    if _root_info(cwd)[0] == _root_info(h)[0]:
        return True          # same project/repo (linked worktrees -> parent repo)
    # Under-$HERE reclaim, for NON-repo cwds only: a scratch dir has no scope of
    # its own, so the nearest ancestor session owns it. A cwd inside a different
    # repo belongs to THAT project even when it lives under $HERE — this is the
    # $HOME-swallow fix (07-22: a home session was handed, and scoped clear would
    # have deleted, every project's entries on the box).
    return (cwd == h or cwd.startswith(h + "/")) and not _root_info(cwd)[1]

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

  # ── v2.1 git-root cases (real dirs): an ANCESTOR session must not swallow
  # repos under it (the $HOME-swallow incident), must still reclaim non-repo
  # scratch dirs, and linked worktrees must map to the parent repo's scope.
  RT="$T/root"; mkdir -p "$RT/scratch/sub"
  git init -q "$RT/repo1" \
    && git -C "$RT/repo1" -c user.name=t -c user.email=t@t commit -q --allow-empty -m x \
    && git -C "$RT/repo1" worktree add -q "$RT/repo1-wt" >/dev/null 2>&1 \
    || bad "git fixture scaffold failed"
  {
    printf '{"status":"pending","cwd":"%s","prompt":"task R1 in-repo","reason":"oom-kill"}\n' "$RT/repo1"
    printf '{"status":"pending","cwd":"%s","prompt":"task R1WT worktree","reason":"usage/rate limit"}\n' "$RT/repo1-wt"
    printf '{"status":"pending","cwd":"%s","prompt":"task SCR scratch-under-root"}\n' "$RT/scratch/sub"
  } > "$CLANKER_RESUME_QUEUE"
  out3="$(CLAUDE_PROJECT_DIR="$RT" bash "$SELF")"   # ancestor of repo1 AND scratch
  echo "$out3" | grep -q "task SCR" && ok || bad "ancestor reclaims non-repo scratch subdir"
  echo "$out3" | grep -q "task R1 in-repo" && bad "HOME-swallow: ancestor surfaced a repo's entry" || ok
  echo "$out3" | grep -q "task R1WT" && bad "HOME-swallow: ancestor surfaced a worktree entry" || ok
  out4="$(CLAUDE_PROJECT_DIR="$RT/repo1" bash "$SELF")"
  echo "$out4" | grep -q "task R1 in-repo" && ok || bad "repo session surfaces its entry"
  echo "$out4" | grep -q "task R1WT" && ok || bad "repo session surfaces its linked-worktree entry"
  echo "$out4" | grep -q "task SCR" && bad "repo session leaked ancestor scratch entry" || ok
  out5="$(CLAUDE_PROJECT_DIR="$RT/repo1-wt" bash "$SELF")"
  echo "$out5" | grep -q "task R1 in-repo" && ok || bad "worktree session maps to parent repo scope"
  CLAUDE_PROJECT_DIR="$RT" bash "$SELF" --clear >/dev/null || bad "ancestor clear exited nonzero"
  grep -q "task R1 in-repo" "$CLANKER_RESUME_QUEUE" && ok || bad "ancestor clear DELETED a repo's pending entry"
  grep -q "task R1WT" "$CLANKER_RESUME_QUEUE" && ok || bad "ancestor clear DELETED a worktree entry"
  grep -q "task SCR" "$CLANKER_RESUME_QUEUE" && bad "ancestor clear left its own scratch entry" || ok

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
