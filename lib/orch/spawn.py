"""Spawn a coding-agent session — MVP-1 of the orchestration subsystem.

Ported from ECC's `session::manager::build_agent_command` (the Claude branch) and
`worktree::{create_for_session, remove}`. Creates an optional git worktree, opens a
detached tmux session, and launches `claude` inside it (interactive seed, or `--print`
headless). The session is registered in the orch store as `running`.

ALL side-effecting calls (git worktree add, tmux new-session, tmux send-keys, kill)
go through an injectable `launcher` so tests stay hermetic. The default `_RealLauncher`
shells out via subprocess. Nothing here imports outside stdlib + the orch contract.

Public API:
    build_claude_command(task, ...)            -> argv list[str]
    spawn(task, ...)                           -> session record dict (or {"error": ...})
    stop(session_id, ...)                      -> bool
    cleanup_worktree(session_id, ...)          -> bool
"""
import os
import re
import shlex
import subprocess
import time
import uuid

from . import control, store

# Branch prefix for orchestrated worktrees (ECC uses `ecc/<id>`; clanker uses this).
BRANCH_PREFIX = "clanker/orch"
# tmux session name prefix; the 8-char id slice keeps names short + unique enough.
TMUX_PREFIX = "orch"


def exit_sentinel_prefix(session_id):
    """The session-unique exit-sentinel prefix typed into the pane after the agent
    command. spawn appends `; printf '\\n<prefix>%s>>\\n' "$?"` (POSIX shells) so the
    pane prints `<prefix><exit code>>>` the moment the agent process terminates —
    the daemon's reconcile pass matches that to drive done/failed (§done-detection).
    The echoed command line only ever shows the literal `%s` (never digits), and the
    sid slice keeps one session's sentinel from matching another's pane."""
    return f"<<CLANKER_EXIT:{session_id[:8]}:"


# ── launcher ─────────────────────────────────────────────────────────────────
class _RealLauncher:
    """Default launcher: runs argv via subprocess, returns (rc, combined_output)."""

    def run(self, argv, cwd=None):
        try:
            p = subprocess.run(
                list(argv), cwd=cwd, capture_output=True, text=True
            )
        except (OSError, ValueError) as e:
            return 127, str(e)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip()


def _launcher(launcher):
    return launcher if launcher is not None else _RealLauncher()


# ── command construction (ports build_agent_command, Claude branch) ──────────
def build_claude_command(task, model=None, allowed_tools=None, disallowed_tools=None,
                         permission_mode=None, add_dirs=None, headless=False,
                         append_system_prompt=None, max_budget_usd=None,
                         skip_permissions=False):
    """Build the `claude` argv for a session, mirroring ECC's recipe.

    Order: ["claude"], then `--print` when headless and `--dangerously-skip-permissions`
    when skip_permissions (so an orchestrated session doesn't hang at the trust/permission
    prompts — clanker's own tmux_manager launches claude this way), then the optional
    profile flags (model, allowed/disallowed tools, permission mode, `--add-dir`,
    append-system-prompt, max-budget-usd), and finally the task as the trailing positional.
    """
    cmd = ["claude"]
    if headless:
        cmd.append("--print")
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if model:
        cmd += ["--model", model]
    if allowed_tools:
        cmd += ["--allowed-tools", _join_tools(allowed_tools)]
    if disallowed_tools:
        cmd += ["--disallowed-tools", _join_tools(disallowed_tools)]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    for d in (add_dirs or []):
        cmd += ["--add-dir", str(d)]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    cmd.append(str(task))
    return cmd


def _join_tools(tools):
    """Comma-join a list of tools; pass a string through untouched."""
    if isinstance(tools, str):
        return tools
    return ",".join(str(t) for t in tools)


# ── worktree helpers (port create_for_session / validate_branch_name) ────────
def _is_git_repo(path, launcher):
    rc, _ = launcher.run(
        ["git", "-C", path, "rev-parse", "--is-inside-work-tree"], cwd=path
    )
    return rc == 0


def _valid_branch_name(name, launcher):
    """git check-ref-format --branch <name> — same gate ECC uses."""
    # Cheap pre-screen so an obviously bad name never reaches the shell.
    if not name or name.startswith("/") or name.endswith("/") or ".." in name \
            or name.endswith(".lock") or any(c.isspace() for c in name) \
            or any(c in name for c in "~^:?*[\\"):
        return False
    rc, _ = launcher.run(["git", "check-ref-format", "--branch", name])
    return rc == 0


def _worktree_path(working_dir, session_id):
    """Sibling dir next to the repo: <parent>/.clanker-worktrees/<repo>-<id8>."""
    parent = os.path.dirname(os.path.abspath(working_dir)) or "."
    base = os.path.basename(os.path.abspath(working_dir)) or "repo"
    root = os.path.join(parent, ".clanker-worktrees")
    return os.path.join(root, f"{base}-{session_id[:8]}")


# ── spawn ────────────────────────────────────────────────────────────────────
def spawn(task, project=None, working_dir=None, headless=False, worktree=None,
          model=None, agent_type="claude", store_path=None, launcher=None,
          config=None, skip_permissions=None, **claude_opts):
    """Spawn a coding-agent session and register it in the store.

    Returns the inserted session record on success, or ``{"error": ...}`` (never
    raises) when at capacity or when a precondition fails. `claude_opts` are passed
    straight through to :func:`build_claude_command` (allowed_tools, permission_mode,
    add_dirs, append_system_prompt, max_budget_usd, disallowed_tools).
    """
    lr = _launcher(launcher)
    cfg = config if config is not None else control.get_config()

    # Resolve the working directory.
    working_dir = _resolve_working_dir(working_dir, project)

    # Capacity cap — refuse (don't raise) when already at max_parallel.
    max_parallel = int(cfg.get("max_parallel", 4))
    active = store.list_sessions(active_only=True, path=store_path)
    if len(active) >= max_parallel:
        return {"error": "at capacity", "active": len(active), "max_parallel": max_parallel}

    sid = uuid.uuid4().hex[:12]
    sid8 = sid[:8]

    # Decide whether to isolate in a worktree.
    want_wt = cfg.get("worktrees", True) if worktree is None else bool(worktree)
    wt_path = None
    branch = None
    run_dir = working_dir
    if want_wt and _is_git_repo(working_dir, lr):
        branch = f"{BRANCH_PREFIX}/{sid}"
        if not _valid_branch_name(branch, lr):
            return {"error": "invalid branch name", "branch": branch}
        wt_path = _worktree_path(working_dir, sid)
        os.makedirs(os.path.dirname(wt_path), exist_ok=True)
        rc, out = lr.run(
            ["git", "-C", working_dir, "worktree", "add", "-b", branch, wt_path, "HEAD"],
            cwd=working_dir,
        )
        if rc != 0:
            return {"error": "worktree add failed", "detail": out, "branch": branch}
        run_dir = wt_path

    # Open a detached tmux session anchored at the run dir. Target the session by
    # NAME (not "<name>:0.0") — tmux resolves it to the active pane regardless of the
    # server's base-index (a hardcoded :0.0 fails as "can't find window: 0" when
    # base-index is 1, which silently dropped the launch command).
    tmux_name = f"{TMUX_PREFIX}-{sid8}"
    tmux_target = tmux_name
    rc, out = lr.run(["tmux", "new-session", "-d", "-s", tmux_name, "-c", run_dir])
    if rc != 0:
        # Roll back the worktree we just created so we don't leak it.
        if wt_path is not None:
            lr.run(["git", "-C", working_dir, "worktree", "remove", "--force", wt_path],
                   cwd=working_dir)
            if branch:
                lr.run(["git", "-C", working_dir, "branch", "-D", branch], cwd=working_dir)
        return {"error": "tmux new-session failed", "detail": out}

    # Launch the agent inside the pane via send-keys (one shell-quoted command + Enter).
    if skip_permissions is None:
        skip_permissions = bool(cfg.get("skip_permissions", True))
    claude_cmd = build_claude_command(
        task, model=model, headless=headless, skip_permissions=skip_permissions, **claude_opts
    )
    cmd_str = " ".join(shlex.quote(a) for a in claude_cmd)
    # Exit sentinel: when the agent process terminates the pane shell prints the
    # exit code, which the supervise loop turns into done (rc 0) / failed (rc != 0).
    sentinel = exit_sentinel_prefix(sid)
    cmd_str += f"; printf '\\n{sentinel}%s>>\\n' \"$?\""
    # Brief readiness delay: send-keys fired immediately after new-session races the
    # shell prompt and the command gets dropped. Skipped when a launcher is injected (tests).
    if launcher is None:
        time.sleep(0.5)
    lr.run(["tmux", "send-keys", "-t", tmux_target, cmd_str, "Enter"])

    # Interactive sessions land on Claude's "trust this folder?" dialog. The operator
    # chose to spawn here, so auto-accept it (best-effort, polled) — otherwise the
    # session hangs there. Headless (--print) never shows it.
    if launcher is None and not headless and cfg.get("auto_trust", True):
        _accept_trust_prompt(tmux_target, lr)

    # Capture the pane PID if tmux can tell us.
    pid = _pane_pid(tmux_target, lr)

    rec = {
        "id": sid,
        "task": task,
        "project": project,
        "agent_type": agent_type,
        "working_dir": working_dir,
        "worktree": wt_path,
        "branch": branch,
        "tmux_target": tmux_target,
        "headless": headless,
        "pid": pid,
        "state": "running",
        "cost_usd": 0,
        "tokens": 0,
        "meta": {"command": claude_cmd, "run_dir": run_dir, "exit_sentinel": sentinel},
    }
    store.insert_session(rec, path=store_path)
    return rec


def _resolve_working_dir(working_dir, project):
    if working_dir:
        return os.path.abspath(os.path.expanduser(working_dir))
    if project:
        return os.path.abspath(os.path.expanduser(os.path.join("~/projects", project)))
    return os.getcwd()


def _accept_trust_prompt(target, launcher, tries=14, interval=0.5):
    """Poll the pane for Claude's trust-folder dialog and accept it (Enter selects the
    pre-highlighted 'Yes, I trust this folder'). Best-effort; returns True if accepted."""
    for _ in range(tries):
        rc, out = launcher.run(["tmux", "capture-pane", "-p", "-t", target])
        if rc != 0:
            return False
        low = out.lower()
        if "trust this folder" in low or "trust the files" in low:
            launcher.run(["tmux", "send-keys", "-t", target, "Enter"])
            return True
        time.sleep(interval)
    return False


def _pane_pid(tmux_target, launcher):
    rc, out = launcher.run(
        ["tmux", "display-message", "-p", "-t", tmux_target, "#{pane_pid}"]
    )
    if rc == 0:
        m = re.search(r"\d+", out)
        if m:
            return int(m.group())
    return None


# ── stop ─────────────────────────────────────────────────────────────────────
def stop(session_id, store_path=None, launcher=None):
    """Kill the session's tmux + best-effort kill its pid, then mark it stopped."""
    lr = _launcher(launcher)
    rec = store.get_session(session_id, path=store_path)
    if rec is None:
        return False
    target = rec.get("tmux_target")
    if target:
        name = target.split(":", 1)[0]
        lr.run(["tmux", "kill-session", "-t", name])
    pid = rec.get("pid")
    if pid:
        lr.run(["kill", "-TERM", str(pid)])
    store.set_state(session_id, "stopped", path=store_path)
    return True


# ── cleanup_worktree ─────────────────────────────────────────────────────────
def cleanup_worktree(session_id, store_path=None, launcher=None):
    """Remove the session's git worktree + delete its branch (both best-effort)."""
    lr = _launcher(launcher)
    rec = store.get_session(session_id, path=store_path)
    if rec is None:
        return False
    wt = rec.get("worktree")
    branch = rec.get("branch")
    base = rec.get("working_dir")
    if not wt or not base:
        return False
    lr.run(["git", "-C", base, "worktree", "remove", "--force", wt], cwd=base)
    if branch:
        lr.run(["git", "-C", base, "branch", "-D", branch], cwd=base)
    return True
