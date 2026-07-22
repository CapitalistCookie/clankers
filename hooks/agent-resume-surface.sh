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
#
# ARCHIVE-ON-CLEAR (2026-07-22, audit R1): a clear no longer DELETES entries —
# dropped entries append to <queue>.resolved.jsonl (June convention) with
# status=cleared + resolved_at + resolved_by, under the same flock. A mistaken
# clear is now diagnosable in seconds (the 07-17 audit-request loss took five
# days precisely because the drop left no trace). The archive is written BEFORE
# the queue rewrite lands: if the append fails, the queue is untouched.
# UNSCOPED entries (no cwd) still SURFACE in every project, but a scoped
# --clear leaves them (audit R2 — project A must not delete what project B is
# also surfacing); remove them explicitly with --clear-unscoped, which touches
# ONLY unscoped entries.
set -u
Q="${CLANKER_RESUME_QUEUE:-$HOME/.claude/agent_resume_queue.jsonl}"
HERE="${CLAUDE_PROJECT_DIR:-$PWD}"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

# One shared project-match predicate (mode = count | list | clear |
# clear-unscoped) so display and clear can never drift on which entries belong
# to this project. An entry is OURS iff pending AND: same git root as $HERE
# (worktree-aware), OR its cwd lives under $HERE while inside NO git repo
# (scratch reclaim), OR either side is unscoped. Path match is component-wise:
# /proj/ab is NOT under /proj/a. The ONE deliberate display/clear asymmetry is
# unscoped entries (R2): shown everywhere, cleared only by --clear-unscoped.
_scan() {  # $1 = mode
python3 - "$1" "$Q" "$HERE" "$SELF" <<'PY'
import json, os, subprocess, sys, time
mode, q, here, self_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
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

def _same_project(cwd, h):
    """True iff cwd belongs to the project rooted at h: same git root (linked
    worktrees -> parent repo), or under h while inside NO git repo (scratch
    reclaim — a cwd inside a DIFFERENT repo belongs to that project even when
    it lives under h; this is the $HOME-swallow fix, 07-22)."""
    h = h.rstrip("/")
    if _root_info(cwd)[0] == _root_info(h)[0]:
        return True
    return (cwd == h or cwd.startswith(h + "/")) and not _root_info(cwd)[1]

def mine(e):
    """Display predicate (count/list)."""
    if e.get("status") != "pending":
        return False
    cwd = e.get("cwd", "") or ""
    if not (here and cwd):
        return True          # unscoped entry or unscoped session: surface everywhere
    return _same_project(cwd, here)

def clearable(e):
    """Clear predicate. Same project-match as display; differs ONLY on
    unscoped entries (R2): they surface everywhere, but a scoped clear leaves
    them — project A's clear must not delete an entry project B is also
    surfacing. --clear-unscoped (or a clear with no scope at all) removes
    ONLY unscoped entries."""
    if e.get("status") != "pending":
        return False
    cwd = e.get("cwd", "") or ""
    if mode == "clear-unscoped" or not here:
        return not cwd
    if not cwd:
        return False         # R2: unscoped entries survive scoped clears
    return _same_project(cwd, here)

try:
    lines = open(q, errors="ignore").read().splitlines()
except Exception:
    lines = []

if mode in ("clear", "clear-unscoped"):
    keep, archived, left_unscoped = [], [], 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            e = json.loads(s)
        except Exception:
            keep.append(s)   # unparseable lines are not ours to destroy
            continue
        if clearable(e):
            e["status"] = "cleared"
            e["resolved_at"] = now
            e["resolved_by"] = (here or "?") + (
                " [--clear-unscoped]" if mode == "clear-unscoped" else "")
            archived.append(e)
        else:
            if mode == "clear" and e.get("status") == "pending" and not (e.get("cwd") or ""):
                left_unscoped += 1
            keep.append(s)
    tmp = q + ".tmp"
    with open(tmp, "w") as f:
        f.write("".join(l + "\n" for l in keep))
    # Archive BEFORE the queue rewrite lands (R1): if this append fails, the
    # queue is untouched — a retried clear may duplicate archive rows, but a
    # pending entry can never vanish without a trace again.
    ar = (q[:-len(".jsonl")] if q.endswith(".jsonl") else q) + ".resolved.jsonl"
    if archived:
        with open(ar, "a") as f:
            for e in archived:
                f.write(json.dumps(e) + "\n")
    os.replace(tmp, q)
    dropped = len(archived)
    word = "entry" if dropped == 1 else "entries"
    scope = "(unscoped only)" if mode == "clear-unscoped" else f"for {here}"
    print(f"[agent-resume] cleared {dropped} pending {word} {scope}; "
          f"{len(keep)} line(s) kept (other projects / unscoped / non-pending)"
          + (f"; archived to {os.path.basename(ar)}" if archived else ""))
    if left_unscoped:
        w = "entry" if left_unscoped == 1 else "entries"
        print(f"[agent-resume] {left_unscoped} UNSCOPED pending {w} left in place "
              f"(surfaces in every project) — if you re-dispatched them too, run:\n"
              f"  bash {self_path} --clear-unscoped")
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

if [ "${1:-}" = "--clear" ] || [ "${1:-}" = "--clear-unscoped" ]; then
  [ -f "$Q" ] || { echo "[agent-resume] queue absent — nothing to clear"; exit 0; }
  exec 9>>"$Q.lock" 2>/dev/null || exit 0
  flock -w 5 9 || { echo "[agent-resume] clear: queue lock busy — re-run" >&2; exit 1; }
  _scan "${1#--}"
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
  RQ="${CLANKER_RESUME_QUEUE%.jsonl}.resolved.jsonl"
  CLAUDE_PROJECT_DIR=/proj/a bash "$SELF" --clear >/dev/null || bad "clear exited nonzero"
  grep -q "task B bravo" "$CLANKER_RESUME_QUEUE" && ok || bad "clear dropped other project's entry"
  grep -q "task AB prefix" "$CLANKER_RESUME_QUEUE" && ok || bad "clear dropped prefix-collision neighbor"
  grep -q "task A alpha" "$CLANKER_RESUME_QUEUE" && bad "clear left this project's pending" || ok
  grep -q "task C no-cwd" "$CLANKER_RESUME_QUEUE" && ok || bad "R2: scoped clear DELETED the unscoped entry"
  grep -q "finished thing" "$CLANKER_RESUME_QUEUE" && ok || bad "clear dropped a non-pending line"
  grep -q "not json garbage" "$CLANKER_RESUME_QUEUE" && ok || bad "clear dropped an unparseable line"
  # R1: cleared != deleted — drops land in the resolved archive with metadata
  grep -q "task A alpha" "$RQ" 2>/dev/null && ok || bad "R1: cleared entry missing from resolved archive"
  grep -q '"status": "cleared"' "$RQ" 2>/dev/null && ok || bad "R1: archive entry lacks status=cleared"
  grep -q '"resolved_by": "/proj/a"' "$RQ" 2>/dev/null && ok || bad "R1: archive entry lacks resolved_by"
  grep -q "task B bravo" "$RQ" 2>/dev/null && bad "R1: archive holds an entry that was NOT cleared" || ok
  # R2 companion: --clear-unscoped removes ONLY unscoped entries (archived too)
  CLAUDE_PROJECT_DIR=/proj/zzz bash "$SELF" --clear-unscoped >/dev/null || bad "clear-unscoped exited nonzero"
  grep -q "task C no-cwd" "$CLANKER_RESUME_QUEUE" && bad "clear-unscoped left the unscoped entry" || ok
  grep -q "task B bravo" "$CLANKER_RESUME_QUEUE" && ok || bad "clear-unscoped deleted a SCOPED entry"
  grep -q "task C no-cwd" "$RQ" 2>/dev/null && ok || bad "clear-unscoped did not archive the unscoped entry"
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
  grep -q "task SCR" "$RQ" 2>/dev/null && ok || bad "R1: ancestor's cleared scratch entry not archived"
  grep -q "task R1 in-repo" "$RQ" 2>/dev/null && bad "R1: archive holds another project's live entry" || ok

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
