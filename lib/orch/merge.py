"""Worktree merge + prune — completes MVP-3 (auto_merge + worktree GC).

Ported from ECC's `worktree::{merge_readiness, merge_into_base}` and
`manager::{merge_ready_worktrees, prune_inactive_worktrees}`. When a session finishes
(state "done") its worktree branch can be merged back into the base repo; terminal
sessions' worktrees get pruned. Merge is gated on `config.auto_merge`; prune acts only
on terminal sessions. Conflict prediction uses `git merge-tree --write-tree` (no actual
merge), so a conflicting branch is never half-merged.

Side effects route through an injectable launcher (like spawn.py) so tests stay hermetic.

Public API:
    merge_readiness(base_dir, branch, base_ref="HEAD", launcher=None) -> dict
    merge_into_base(session, base_ref=None, launcher=None, store_path=None) -> dict
    merge_ready_worktrees(config=None, store_path=None, launcher=None) -> list
    prune_worktrees(store_path=None, launcher=None) -> list
"""
import os
import subprocess

from . import control, store


class _RealLauncher:
    def run(self, argv, cwd=None):
        try:
            p = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True)
        except (OSError, ValueError) as e:
            return 127, str(e)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def _launcher(launcher):
    return launcher if launcher is not None else _RealLauncher()


def _base_branch(base_dir, lr):
    rc, out = lr.run(["git", "-C", base_dir, "rev-parse", "--abbrev-ref", "HEAD"], cwd=base_dir)
    out = (out or "").strip()
    return out if (rc == 0 and out and out != "HEAD") else "HEAD"


def merge_readiness(base_dir, branch, base_ref="HEAD", launcher=None):
    """Predict whether `branch` merges cleanly into `base_ref` WITHOUT merging, via
    `git merge-tree --write-tree`. Exit 0 = clean, 1 = conflicts, >1 = error."""
    lr = _launcher(launcher)
    rc, out = lr.run(["git", "-C", base_dir, "merge-tree", "--write-tree", base_ref, branch], cwd=base_dir)
    if rc == 0:
        return {"ready": True, "conflicts": [], "reason": "clean"}
    if rc == 1:
        conflicts = [l for l in out.splitlines() if l.startswith("CONFLICT") or l.startswith("Auto-merging") or "\t" in l]
        return {"ready": False, "conflicts": conflicts[:20], "reason": "conflicts"}
    return {"ready": False, "conflicts": [], "reason": "merge-tree error: " + (out or "")[:200]}


def _memo_merge_state(session, state, store_path):
    """Persist a terminal merge outcome on the session so merge_ready_worktrees
    never revisits it (a done session stays 'done' forever — without the memo it
    would be re-merge-attempted every supervise pass)."""
    sid = session.get("id")
    if not sid:
        return
    meta = dict(session.get("meta") or {})
    meta["merge_state"] = state
    store.update_session(sid, path=store_path, meta=meta)


def merge_into_base(session, base_ref=None, launcher=None, store_path=None):
    """Merge a finished session's worktree branch into the base repo's branch.
    Guards: branch still exists, base working tree clean AND merge predicted
    clean. Never half-merges (aborts on failure). Returns a classified result
    dict with a `status`. Terminal outcomes (merged / branch already gone) are
    memoized on the session as meta.merge_state; retryable ones (dirty base,
    conflicts, merge failure) are not."""
    lr = _launcher(launcher)
    base, branch = session.get("working_dir"), session.get("branch")
    sid = session.get("id")
    if not base or not branch:
        return {"session": sid, "status": "skipped", "reason": "no worktree/branch"}
    rc, _ = lr.run(["git", "-C", base, "rev-parse", "--verify", "--quiet", branch], cwd=base)
    if rc != 0:
        _memo_merge_state(session, "branch_missing", store_path)
        return {"session": sid, "status": "skipped", "reason": "branch missing (already pruned?)"}
    rc, out = lr.run(["git", "-C", base, "status", "--porcelain"], cwd=base)
    if rc != 0:
        return {"session": sid, "status": "error", "reason": "base status failed"}
    if (out or "").strip():
        return {"session": sid, "status": "dirty", "reason": "base has uncommitted changes"}
    target = base_ref or _base_branch(base, lr)
    ready = merge_readiness(base, branch, target, launcher=lr)
    if not ready["ready"]:
        store.log_event(sid, "merge_conflict", branch, path=store_path)
        return {"session": sid, "status": "conflicted", "reason": ready["reason"], "conflicts": ready["conflicts"]}
    rc, out = lr.run(["git", "-C", base, "merge", "--no-ff", "-m", f"orch: merge {branch}", branch], cwd=base)
    if rc != 0:
        lr.run(["git", "-C", base, "merge", "--abort"], cwd=base)
        return {"session": sid, "status": "failed", "reason": (out or "")[:200]}
    store.log_event(sid, "merged", branch, path=store_path)
    _memo_merge_state(session, "merged", store_path)
    return {"session": sid, "status": "merged", "branch": branch}


def merge_ready_worktrees(config=None, store_path=None, launcher=None):
    """Merge every 'done' session's worktree branch. Only runs when config.auto_merge.
    Sessions with a terminal meta.merge_state memo are skipped (already handled)."""
    cfg = config if config is not None else control.get_config()
    if not cfg.get("auto_merge"):
        return []
    results = []
    for s in store.list_sessions(state="done", path=store_path):
        if (s.get("meta") or {}).get("merge_state"):
            continue
        if s.get("branch") and s.get("worktree"):
            results.append(merge_into_base(s, launcher=launcher, store_path=store_path))
    return results


def _worktree_clean(wt, lr):
    """True iff the worktree has no uncommitted changes (status --porcelain empty)."""
    rc, out = lr.run(["git", "-C", wt, "status", "--porcelain"], cwd=wt)
    return rc == 0 and not (out or "").strip()


def _pane_has_live_children(pid, lr):
    """True iff the session's pane shell (pid) has live child processes — i.e.
    the agent may still be running there. pgrep -P exits 0 when children exist.
    No pid recorded -> False (nothing to protect)."""
    if not pid:
        return False
    rc, _ = lr.run(["pgrep", "-P", str(pid)])
    return rc == 0


def _branch_merged(base, branch, lr):
    """True iff `branch` is an ancestor of the base repo's HEAD (nothing unmerged)."""
    rc, _ = lr.run(["git", "-C", base, "merge-base", "--is-ancestor", branch, "HEAD"], cwd=base)
    return rc == 0


def prune_worktrees(store_path=None, launcher=None):
    """Remove worktrees + branches of TERMINAL sessions (done/failed/stopped) that
    still exist on disk, then `git worktree prune` their base repos. Returns the
    removed records. Safe to call every pass — only touches terminal sessions.

    A `done` session's worktree is GC-able only once (a) its pane has no live
    child processes — an agent may still be running there (interactive sessions
    stay alive after idle-done; a force-removal would yank the workspace out
    from under it, as a live trial demonstrated) — and (b) its branch is fully
    merged into the base HEAD AND the worktree holds nothing uncommitted —
    force-removal would otherwise destroy finished-but-unmerged work (e.g.
    done-detection firing while auto_merge is off). Such worktrees are skipped
    until the agent exits / auto_merge (or the operator) lands them.
    failed/stopped worktrees are reclaimed unconditionally, as before (stop
    kills the tmux session, and a failed session's pane is dead by definition)."""
    lr = _launcher(launcher)
    removed = []
    seen_bases = set()
    for s in store.list_sessions(path=store_path):
        if s.get("state") in store.TERMINAL_STATES and s.get("worktree") and s.get("working_dir"):
            wt, base, branch = s["worktree"], s["working_dir"], s.get("branch")
            seen_bases.add(base)
            if os.path.isdir(wt):
                if s["state"] == "done" and (
                        _pane_has_live_children(s.get("pid"), lr)
                        or not (_worktree_clean(wt, lr) and branch
                                and _branch_merged(base, branch, lr))):
                    continue
                rc, _ = lr.run(["git", "-C", base, "worktree", "remove", "--force", wt], cwd=base)
                if branch:
                    lr.run(["git", "-C", base, "branch", "-D", branch], cwd=base)
                removed.append({"session": s["id"], "worktree": wt, "removed": rc == 0})
                store.log_event(s["id"], "pruned", wt, path=store_path)
                if rc == 0:
                    # Clear the dead pointer so neither prune nor auto-merge
                    # revisits this session on later passes.
                    store.update_session(s["id"], path=store_path, worktree=None)
    for base in seen_bases:
        lr.run(["git", "-C", base, "worktree", "prune"], cwd=base)
    return removed
