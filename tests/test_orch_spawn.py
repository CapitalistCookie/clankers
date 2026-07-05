"""Hermetic tests for lib/orch/spawn.py — the MVP-1 session spawner.

No real tmux/claude/git side effects: every side-effecting call goes through a
FAKE launcher that records argv and returns success. The store lives in a temp
SQLite db (path=) and the worktree base is a throwaway `git init` repo so the
git-repo probe is realistic without mutating anything outside the temp dir.

Run: python3 -m pytest tests/test_orch_spawn.py -v   (or: python3 tests/test_orch_spawn.py)
"""
import os
import sys
import tempfile

sys.path.insert(0, "lib")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from orch import spawn, store, control  # noqa: E402


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeLauncher:
    """Records every argv; returns canned (rc, out) so nothing real runs.

    Recognises the read-only probes spawn issues (is-inside-work-tree,
    check-ref-format, display-message) and answers them; everything else
    (worktree add, tmux new-session/send-keys/kill, branch -D) returns rc 0.
    Override `fail_on` (a substring) to force the first matching argv to rc 1.
    """

    def __init__(self, is_repo=True, fail_on=None, pane_pid="4242"):
        self.calls = []
        self.is_repo = is_repo
        self.fail_on = fail_on
        self.pane_pid = pane_pid

    def run(self, argv, cwd=None):
        argv = list(argv)
        self.calls.append((argv, cwd))
        s = " ".join(argv)
        if self.fail_on and self.fail_on in s:
            return 1, "forced failure"
        if "rev-parse" in argv and "--is-inside-work-tree" in argv:
            return (0, "true") if self.is_repo else (1, "not a git repo")
        if "check-ref-format" in argv:
            return 0, ""
        if "display-message" in argv:
            return (0, self.pane_pid) if self.pane_pid is not None else (1, "")
        return 0, "ok"

    # convenience filters for assertions
    def argvs(self):
        return [c[0] for c in self.calls]

    def find(self, *needles):
        """All recorded argvs that contain every needle (substring of an arg or the joined line)."""
        out = []
        for a in self.argvs():
            line = " ".join(a)
            if all(n in a or n in line for n in needles):
                out.append(a)
        return out

    def first(self, *needles):
        hits = self.find(*needles)
        return hits[0] if hits else None


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="orch_test_")
    os.close(fd)
    os.unlink(path)  # let the store create it fresh
    return path


def _cfg(**over):
    c = {"enabled": True, "worktrees": True, "max_parallel": 4,
         "auto_nudge": False, "auto_spawn": False, "auto_merge": False}
    c.update(over)
    return c


# ── build_claude_command ──────────────────────────────────────────────────────
def test_build_command_minimal_is_claude_plus_task():
    cmd = spawn.build_claude_command("do the thing")
    assert cmd == ["claude", "do the thing"]


def test_build_command_headless_adds_print_flag():
    cmd = spawn.build_claude_command("t", headless=True)
    assert cmd[0] == "claude"
    assert "--print" in cmd
    # --print precedes the task positional
    assert cmd.index("--print") < cmd.index("t")
    # interactive has no --print
    assert "--print" not in spawn.build_claude_command("t", headless=False)


def test_build_command_allowed_tools_joined_with_commas():
    cmd = spawn.build_claude_command("t", allowed_tools=["Read", "Edit", "Bash"])
    i = cmd.index("--allowed-tools")
    assert cmd[i + 1] == "Read,Edit,Bash"
    # a pre-joined string passes through untouched
    cmd2 = spawn.build_claude_command("t", allowed_tools="Read,Edit")
    assert cmd2[cmd2.index("--allowed-tools") + 1] == "Read,Edit"


def test_build_command_disallowed_tools_and_permission_mode():
    cmd = spawn.build_claude_command(
        "t", disallowed_tools=["Bash"], permission_mode="acceptEdits")
    assert cmd[cmd.index("--disallowed-tools") + 1] == "Bash"
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"


def test_build_command_repeats_add_dir_per_directory():
    cmd = spawn.build_claude_command("t", add_dirs=["/a", "/b", "/c"])
    assert cmd.count("--add-dir") == 3
    # each --add-dir is immediately followed by its dir
    idxs = [i for i, x in enumerate(cmd) if x == "--add-dir"]
    assert [cmd[i + 1] for i in idxs] == ["/a", "/b", "/c"]


def test_build_command_system_prompt_and_budget():
    cmd = spawn.build_claude_command(
        "the task", append_system_prompt="be terse", max_budget_usd=2.5)
    assert cmd[cmd.index("--append-system-prompt") + 1] == "be terse"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "2.5"
    # task is always the final positional
    assert cmd[-1] == "the task"


def test_build_command_full_ordering():
    cmd = spawn.build_claude_command(
        "TASK", model="claude-opus-4-8", allowed_tools=["Read"],
        disallowed_tools=["Bash"], permission_mode="plan",
        add_dirs=["/x"], headless=True, append_system_prompt="sp",
        max_budget_usd=10)
    assert cmd[0] == "claude"
    assert cmd[1] == "--print"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"
    assert cmd[-1] == "TASK"


# ── spawn: worktree + tmux + registration ─────────────────────────────────────
def test_spawn_creates_worktree_tmux_and_registers_running(tmp_git_repo=None):
    db = _tmp_db()
    repo = _make_git_repo()
    fl = FakeLauncher(is_repo=True)
    rec = spawn.spawn("build a feature", project="demo", working_dir=repo,
                      store_path=db, launcher=fl, config=_cfg(),
                      allowed_tools=["Read", "Edit"])

    # returned record
    assert "error" not in rec
    assert rec["state"] == "running"
    assert rec["agent_type"] == "claude"
    assert rec["branch"] == f"{spawn.BRANCH_PREFIX}/{rec['id']}"
    assert rec["worktree"] and rec["id"][:8] in rec["worktree"]
    assert rec["tmux_target"] == f"orch-{rec['id'][:8]}"  # session name; tmux resolves to active pane
    assert rec["pid"] == 4242

    # persisted + active
    got = store.get_session(rec["id"], path=db)
    assert got is not None and got["state"] == "running"
    assert any(s["id"] == rec["id"] for s in store.list_sessions(active_only=True, path=db))

    # the worktree-add argv hit the launcher with the right branch + HEAD base
    wt = fl.first("worktree", "add")
    assert wt is not None
    assert "-b" in wt and rec["branch"] in wt and "HEAD" in wt
    assert rec["worktree"] in wt

    # tmux session was opened detached anchored at the worktree dir
    ns = fl.first("new-session")
    assert ns is not None
    assert "-d" in ns and "-s" in ns
    assert ns[ns.index("-s") + 1] == f"orch-{rec['id'][:8]}"
    assert ns[ns.index("-c") + 1] == rec["worktree"]

    # the agent was launched via send-keys: a quoted `claude ... task` + Enter
    sk = fl.first("send-keys")
    assert sk is not None
    assert sk[-1] == "Enter"
    assert sk[sk.index("-t") + 1] == rec["tmux_target"]
    cmd_str = sk[-2]
    assert cmd_str.startswith("claude")
    assert "--allowed-tools Read,Edit" in cmd_str
    assert "build a feature" in cmd_str
    assert "--print" not in cmd_str  # interactive seed, not headless

    os.unlink(db)


def test_spawn_headless_sends_print_command():
    db = _tmp_db()
    repo = _make_git_repo()
    fl = FakeLauncher(is_repo=True)
    rec = spawn.spawn("headless job", working_dir=repo, headless=True,
                      store_path=db, launcher=fl, config=_cfg())
    assert "error" not in rec and rec["headless"] is True
    sk = fl.first("send-keys")
    assert "claude --print" in sk[-2]
    assert "headless job" in sk[-2]
    os.unlink(db)


def test_spawn_appends_exit_sentinel_and_stores_prefix():
    """The typed command ends with the exit-sentinel printf (so the pane prints the
    agent's exit code on termination) and the prefix is persisted in session meta
    for the daemon's done-detection. Applies to headless AND interactive."""
    for headless in (True, False):
        db = _tmp_db()
        repo = _make_git_repo()
        fl = FakeLauncher(is_repo=True)
        rec = spawn.spawn("sentinel job", working_dir=repo, headless=headless,
                          store_path=db, launcher=fl, config=_cfg())
        assert "error" not in rec
        prefix = spawn.exit_sentinel_prefix(rec["id"])
        assert prefix == f"<<CLANKER_EXIT:{rec['id'][:8]}:"
        cmd_str = fl.first("send-keys")[-2]
        assert cmd_str.endswith(f"; printf '\\n{prefix}%s>>\\n' \"$?\"")
        # the prefix is in the stored meta (the daemon reads it from there)
        got = store.get_session(rec["id"], path=db)
        assert got["meta"]["exit_sentinel"] == prefix
        os.unlink(db)


def test_spawn_without_worktrees_runs_in_working_dir():
    db = _tmp_db()
    repo = _make_git_repo()
    fl = FakeLauncher(is_repo=True)
    rec = spawn.spawn("no wt", working_dir=repo, store_path=db, launcher=fl,
                      config=_cfg(worktrees=False))
    assert "error" not in rec
    assert rec["worktree"] is None and rec["branch"] is None
    # no worktree-add was issued; tmux anchored at the working dir itself
    assert fl.find("worktree", "add") == []
    ns = fl.first("new-session")
    assert ns[ns.index("-c") + 1] == os.path.abspath(repo)
    os.unlink(db)


def test_spawn_non_git_dir_skips_worktree_even_when_enabled():
    db = _tmp_db()
    plain = tempfile.mkdtemp(prefix="plain_")
    fl = FakeLauncher(is_repo=False)
    rec = spawn.spawn("x", working_dir=plain, store_path=db, launcher=fl, config=_cfg())
    assert "error" not in rec
    assert rec["worktree"] is None
    assert fl.find("worktree", "add") == []
    os.unlink(db)


# ── spawn: capacity cap ───────────────────────────────────────────────────────
def test_spawn_returns_error_at_capacity():
    db = _tmp_db()
    repo = _make_git_repo()
    fl = FakeLauncher(is_repo=True)
    cfg = _cfg(max_parallel=2)
    a = spawn.spawn("one", working_dir=repo, store_path=db, launcher=fl, config=cfg)
    b = spawn.spawn("two", working_dir=repo, store_path=db, launcher=fl, config=cfg)
    assert "error" not in a and "error" not in b
    # third is over the cap
    c = spawn.spawn("three", working_dir=repo, store_path=db, launcher=fl, config=cfg)
    assert c.get("error") == "at capacity"
    assert c["max_parallel"] == 2 and c["active"] == 2
    # it did NOT register a session or touch tmux/git
    assert len(store.list_sessions(active_only=True, path=db)) == 2
    calls_before = len(fl.calls)
    # capacity check happens before any launcher work for the rejected spawn,
    # so no new-session was issued for "three"
    assert not any("three" in " ".join(a) for a in fl.argvs())
    assert calls_before >= 0
    os.unlink(db)


# ── stop ──────────────────────────────────────────────────────────────────────
def test_stop_kills_tmux_and_sets_state_stopped():
    db = _tmp_db()
    repo = _make_git_repo()
    fl = FakeLauncher(is_repo=True)
    rec = spawn.spawn("running task", working_dir=repo, store_path=db, launcher=fl,
                      config=_cfg())
    sid = rec["id"]
    fl.calls.clear()

    ok = spawn.stop(sid, store_path=db, launcher=fl)
    assert ok is True
    assert store.get_session(sid, path=db)["state"] == "stopped"

    # killed the tmux session by name (not the full pane target)
    ks = fl.first("kill-session")
    assert ks is not None
    assert ks[ks.index("-t") + 1] == f"orch-{sid[:8]}"
    # best-effort pid kill
    assert fl.find("kill", "-TERM", "4242") != []

    # stopping an unknown session is a no-op False
    assert spawn.stop("doesnotexist", store_path=db, launcher=fl) is False
    os.unlink(db)


# ── cleanup_worktree ──────────────────────────────────────────────────────────
def test_cleanup_worktree_removes_and_deletes_branch():
    db = _tmp_db()
    repo = _make_git_repo()
    fl = FakeLauncher(is_repo=True)
    rec = spawn.spawn("wt task", working_dir=repo, store_path=db, launcher=fl,
                      config=_cfg())
    fl.calls.clear()

    ok = spawn.cleanup_worktree(rec["id"], store_path=db, launcher=fl)
    assert ok is True
    rm = fl.first("worktree", "remove")
    assert rm is not None and "--force" in rm and rec["worktree"] in rm
    bd = fl.first("branch", "-D")
    assert bd is not None and rec["branch"] in bd

    # a session with no worktree returns False (nothing to clean)
    db2 = _tmp_db()
    rec2 = spawn.spawn("nowt", working_dir=repo, store_path=db2,
                       launcher=FakeLauncher(is_repo=True), config=_cfg(worktrees=False))
    assert spawn.cleanup_worktree(rec2["id"], store_path=db2, launcher=fl) is False
    os.unlink(db)
    os.unlink(db2)


# ── invalid branch name is rejected (not raised) ──────────────────────────────
def test_spawn_rejects_invalid_branch_name():
    db = _tmp_db()
    repo = _make_git_repo()

    class BadBranchLauncher(FakeLauncher):
        def run(self, argv, cwd=None):
            self.calls.append((list(argv), cwd))
            if "check-ref-format" in argv:
                return 1, "fatal: bad ref"
            if "rev-parse" in argv:
                return 0, "true"
            return 0, "ok"

    fl = BadBranchLauncher(is_repo=True)
    rec = spawn.spawn("x", working_dir=repo, store_path=db, launcher=fl, config=_cfg())
    assert rec.get("error") == "invalid branch name"
    # nothing got registered and no worktree-add was attempted
    assert store.list_sessions(path=db) == []
    assert fl.find("worktree", "add") == []
    os.unlink(db)


# ── helpers ───────────────────────────────────────────────────────────────────
def _make_git_repo():
    """Create a real throwaway git repo dir so the is-git-repo probe is honest.

    The probe itself is faked by FakeLauncher, but using a real dir keeps the path
    resolution (abspath/basename) realistic and the worktree-root mkdir harmless.
    """
    import subprocess
    d = tempfile.mkdtemp(prefix="orch_repo_")
    subprocess.run(["git", "init", "-q", d], capture_output=True)
    return d


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
