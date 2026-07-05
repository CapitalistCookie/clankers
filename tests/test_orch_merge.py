"""Hermetic tests for orch.merge — real git in throwaway /tmp repos (git ops can't be
meaningfully faked). Covers merge-readiness (clean/conflict), merge_into_base, and prune."""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from orch import merge, store  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def _setup_repo():
    d = tempfile.mkdtemp(prefix="orch-merge-")
    repo = os.path.join(d, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.c")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "f.txt"), "w") as fh:
        fh.write("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return d, repo


def _make_worktree(repo, branch, fname, content):
    wt = os.path.join(os.path.dirname(repo), ".wt", branch.replace("/", "_"))
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    _git(repo, "worktree", "add", "-b", branch, wt, "HEAD")
    with open(os.path.join(wt, fname), "w") as fh:
        fh.write(content)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "change")
    return wt


def test_merge_readiness_clean():
    d, repo = _setup_repo()
    _make_worktree(repo, "clanker/orch/a", "new.txt", "hello\n")
    assert merge.merge_readiness(repo, "clanker/orch/a")["ready"] is True


def test_merge_readiness_conflict():
    d, repo = _setup_repo()
    _make_worktree(repo, "clanker/orch/b", "f.txt", "branch-change\n")
    with open(os.path.join(repo, "f.txt"), "w") as fh:
        fh.write("base-change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "basechange")
    assert merge.merge_readiness(repo, "clanker/orch/b")["ready"] is False


def test_merge_into_base_merges_clean():
    d, repo = _setup_repo()
    wt = _make_worktree(repo, "clanker/orch/c", "feature.txt", "feat\n")
    sess = {"id": "c1", "working_dir": repo, "branch": "clanker/orch/c", "worktree": wt}
    res = merge.merge_into_base(sess, store_path=os.path.join(d, "s.db"))
    assert res["status"] == "merged", res
    assert os.path.isfile(os.path.join(repo, "feature.txt"))  # change landed in base


def test_merge_dirty_base_skipped():
    d, repo = _setup_repo()
    wt = _make_worktree(repo, "clanker/orch/d", "x.txt", "x\n")
    with open(os.path.join(repo, "dirty.txt"), "w") as fh:
        fh.write("uncommitted\n")
    res = merge.merge_into_base({"id": "d1", "working_dir": repo, "branch": "clanker/orch/d", "worktree": wt},
                                store_path=os.path.join(d, "s.db"))
    assert res["status"] == "dirty", res


def test_prune_removes_terminal_worktree():
    d, repo = _setup_repo()
    wt = _make_worktree(repo, "clanker/orch/e", "y.txt", "y\n")
    db = os.path.join(d, "s.db")
    store.insert_session({"id": "e1", "working_dir": repo, "branch": "clanker/orch/e",
                          "worktree": wt, "state": "stopped"}, path=db)
    assert os.path.isdir(wt)
    removed = merge.prune_worktrees(store_path=db)
    assert any(r["session"] == "e1" and r["removed"] for r in removed), removed
    assert not os.path.isdir(wt)


def test_prune_skips_active():
    d, repo = _setup_repo()
    wt = _make_worktree(repo, "clanker/orch/f", "z.txt", "z\n")
    db = os.path.join(d, "s.db")
    store.insert_session({"id": "f1", "working_dir": repo, "branch": "clanker/orch/f",
                          "worktree": wt, "state": "running"}, path=db)
    removed = merge.prune_worktrees(store_path=db)
    assert not any(r["session"] == "f1" for r in removed)
    assert os.path.isdir(wt)  # active worktree untouched


def test_auto_merge_gate_off():
    d, repo = _setup_repo()
    wt = _make_worktree(repo, "clanker/orch/g", "g.txt", "g\n")
    db = os.path.join(d, "s.db")
    store.insert_session({"id": "g1", "working_dir": repo, "branch": "clanker/orch/g",
                          "worktree": wt, "state": "done"}, path=db)
    assert merge.merge_ready_worktrees(config={"auto_merge": False}, store_path=db) == []
    res = merge.merge_ready_worktrees(config={"auto_merge": True}, store_path=db)
    assert len(res) == 1 and res[0]["status"] == "merged", res


def test_merge_ready_runs_once_per_session():
    """LIVE-TRIAL regression: a merged done session must not be re-merge-attempted
    on every later pass (after prune deletes its branch, the retry surfaced as a
    bogus 'conflicted' + a merge_conflict event per supervise tick)."""
    d, repo = _setup_repo()
    wt = _make_worktree(repo, "clanker/orch/k", "k.txt", "k\n")
    db = os.path.join(d, "s.db")
    store.insert_session({"id": "k1", "working_dir": repo, "branch": "clanker/orch/k",
                          "worktree": wt, "state": "done"}, path=db)
    first = merge.merge_ready_worktrees(config={"auto_merge": True}, store_path=db)
    assert len(first) == 1 and first[0]["status"] == "merged", first
    assert store.get_session("k1", path=db)["meta"]["merge_state"] == "merged"
    # second pass: memo skips it entirely
    assert merge.merge_ready_worktrees(config={"auto_merge": True}, store_path=db) == []


def test_merge_missing_branch_skips_and_memoizes():
    """A done session whose branch no longer exists (e.g. pruned by an older
    version, or deleted manually) is skipped once and memoized — not classified
    'conflicted' forever."""
    d, repo = _setup_repo()
    db = os.path.join(d, "s.db")
    store.insert_session({"id": "m1", "working_dir": repo, "branch": "clanker/orch/gone",
                          "worktree": os.path.join(d, "nowhere"), "state": "done"}, path=db)
    first = merge.merge_ready_worktrees(config={"auto_merge": True}, store_path=db)
    assert len(first) == 1 and first[0]["status"] == "skipped", first
    assert store.get_session("m1", path=db)["meta"]["merge_state"] == "branch_missing"
    assert merge.merge_ready_worktrees(config={"auto_merge": True}, store_path=db) == []


def test_prune_clears_worktree_pointer():
    d, repo = _setup_repo()
    wt = _make_worktree(repo, "clanker/orch/n", "n.txt", "n\n")
    db = os.path.join(d, "s.db")
    store.insert_session({"id": "n1", "working_dir": repo, "branch": "clanker/orch/n",
                          "worktree": wt, "state": "stopped"}, path=db)
    removed = merge.prune_worktrees(store_path=db)
    assert any(r["session"] == "n1" and r["removed"] for r in removed)
    assert store.get_session("n1", path=db)["worktree"] is None
    # later passes have nothing to do
    assert merge.prune_worktrees(store_path=db) == []


def test_prune_keeps_done_unmerged_worktree():
    """A done session whose branch is NOT merged must keep its worktree+branch —
    force-pruning it would destroy the finished work (auto_merge off / not yet run)."""
    d, repo = _setup_repo()
    wt = _make_worktree(repo, "clanker/orch/h", "h.txt", "h\n")  # 1 unmerged commit
    db = os.path.join(d, "s.db")
    store.insert_session({"id": "h1", "working_dir": repo, "branch": "clanker/orch/h",
                          "worktree": wt, "state": "done"}, path=db)
    removed = merge.prune_worktrees(store_path=db)
    assert not any(r["session"] == "h1" for r in removed)
    assert os.path.isdir(wt)
    assert _git(repo, "rev-parse", "--verify", "clanker/orch/h").returncode == 0


def test_prune_keeps_done_dirty_worktree():
    """A done session that finished WITHOUT committing (branch == HEAD but the
    worktree is dirty) must keep its worktree — its only copy of the work."""
    d, repo = _setup_repo()
    wt = os.path.join(d, ".wt", "dirty")
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    _git(repo, "worktree", "add", "-b", "clanker/orch/i", wt, "HEAD")
    with open(os.path.join(wt, "uncommitted.txt"), "w") as fh:
        fh.write("not yet committed\n")
    db = os.path.join(d, "s.db")
    store.insert_session({"id": "i1", "working_dir": repo, "branch": "clanker/orch/i",
                          "worktree": wt, "state": "done"}, path=db)
    removed = merge.prune_worktrees(store_path=db)
    assert not any(r["session"] == "i1" for r in removed)
    assert os.path.isfile(os.path.join(wt, "uncommitted.txt"))


def test_prune_skips_done_with_live_agent():
    """LIVE-TRIAL regression: prune force-removed a worktree from under a still-
    running agent. A done session whose pane shell (pid) has live children is
    never pruned; a live but child-less pane (agent exited) is fair game."""
    d, repo = _setup_repo()
    wt = _make_worktree(repo, "clanker/orch/p", "p.txt", "p\n")
    db = os.path.join(d, "s.db")
    sess = {"id": "p1", "working_dir": repo, "branch": "clanker/orch/p",
            "worktree": wt, "state": "done", "pid": 1}  # pid 1 always has children
    store.insert_session(sess, path=db)
    assert merge.merge_into_base(sess, store_path=db)["status"] == "merged"

    removed = merge.prune_worktrees(store_path=db)
    assert not any(r["session"] == "p1" for r in removed)
    assert os.path.isdir(wt)

    # agent gone: a child-less live pid (a bare sleep) no longer blocks pruning
    proc = subprocess.Popen(["sleep", "30"])
    try:
        store.update_session("p1", path=db, pid=proc.pid)
        removed = merge.prune_worktrees(store_path=db)
        assert any(r["session"] == "p1" and r["removed"] for r in removed), removed
        assert not os.path.isdir(wt)
    finally:
        proc.kill()
        proc.wait()


def test_prune_removes_done_merged_clean_worktree():
    """Once a done session's branch is merged into base HEAD and its worktree is
    clean, prune reclaims both (the auto_merge -> prune lifecycle)."""
    d, repo = _setup_repo()
    wt = _make_worktree(repo, "clanker/orch/j", "j.txt", "j\n")
    db = os.path.join(d, "s.db")
    sess = {"id": "j1", "working_dir": repo, "branch": "clanker/orch/j",
            "worktree": wt, "state": "done"}
    store.insert_session(sess, path=db)
    assert merge.merge_into_base(sess, store_path=db)["status"] == "merged"
    removed = merge.prune_worktrees(store_path=db)
    assert any(r["session"] == "j1" and r["removed"] for r in removed), removed
    assert not os.path.isdir(wt)
    # branch deleted too
    assert _git(repo, "rev-parse", "--verify", "clanker/orch/j").returncode != 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
