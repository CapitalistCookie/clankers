"""`clanker new` / `clanker open` — spawn and attach throwaway Claude Code sessions.

`clanker new [name]` opens a detached tmux session anchored at the current directory
and launches Claude with the sandbox disabled and permission prompts skipped
(`CLAUDE_CODE_DISABLE_SANDBOX=1 claude --dangerously-skip-permissions`). `clanker open
[name]` (aliases: `attach`, `test`) attaches to it — defaulting to the most recent one
`new` created. Works from anywhere; nothing here is project-specific.
"""
import os
import re
import shlex
import shutil
import subprocess
import time

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")
_LAST_FILE = os.path.join(DATA_DIR, ".last_new_session")

LAUNCH = "CLAUDE_CODE_DISABLE_SANDBOX=1 claude --dangerously-skip-permissions"


def launch_cmd(cwd, resume=None):
    """The one true launch line: explicit cd first, so a pane/shell that was
    re-homed by anything (rc files, restores, keepalives) still lands Claude in
    the project dir — the 2026-07-05 fleet regression was every session's
    Claude silently running at ~. resume: True → `--resume` picker (lists THIS
    project's sessions); a session-id string → resume that session directly."""
    cmd = f"cd {shlex.quote(os.path.abspath(cwd))} && {LAUNCH}"
    if resume is True:
        cmd += " --resume"
    elif resume:
        cmd += f" --resume {shlex.quote(str(resume))}"
    return cmd


def _slug(name):
    """A tmux-safe session name from arbitrary words (anti flag/dot injection)."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip()).strip("-")
    return s[:40] or "claude"


def _tmux():
    return shutil.which("tmux")


def _session_exists(name):
    return subprocess.run(["tmux", "has-session", "-t", name],
                          capture_output=True).returncode == 0


def _unique_name(base):
    name = base
    n = 2
    while _session_exists(name):
        name = f"{base}-{n}"
        n += 1
    return name


def _record_last(name):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_LAST_FILE, "w") as f:
            f.write(name)
    except OSError:
        pass


def read_last():
    try:
        with open(_LAST_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _wait_shell_prompt(name, timeout=3.0):
    """Wait until the pane's shell shows a prompt before typing into it. Keys sent
    during shell startup can be swallowed by init (seen on a fresh container's
    first login shell); a visible prompt means the shell is reading input."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = subprocess.run(["tmux", "capture-pane", "-p", "-t", name],
                           capture_output=True, text=True)
        if p.returncode == 0:
            lines = [ln.rstrip() for ln in p.stdout.split("\n") if ln.strip()]
            if lines and re.search(r"[$#%❯>]\s*$", lines[-1]):
                return True
        time.sleep(0.15)
    return False


def _accept_trust_prompt(name, tries=14, interval=0.5):
    """Best-effort: poll the pane for Claude's 'trust this folder?' dialog and accept
    it (Enter selects the pre-highlighted Yes), so the session lands ready."""
    for _ in range(tries):
        p = subprocess.run(["tmux", "capture-pane", "-p", "-t", name],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return
        low = p.stdout.lower()
        if "trust this folder" in low or "trust the files" in low:
            subprocess.run(["tmux", "send-keys", "-t", name, "Enter"], capture_output=True)
            return
        time.sleep(interval)


def spawn(name=None, cwd=None, shell=False, resume=None):
    """Create a detached tmux session and launch Claude (or a bare shell) in it.
    Returns (name, message). Never raises for the expected failure modes."""
    if not _tmux():
        return None, "tmux is not installed (required for `clanker new`)."
    cwd = os.path.abspath(cwd or os.getcwd())

    # Seed the project's Claude memory namespace so the session isn't memory-blind
    # (repo-rooted sessions otherwise load an empty MEMORY.md). Best-effort.
    try:
        from memoryns import ensure_memory_stub
        ensure_memory_stub(cwd)
    except Exception:
        pass

    requested = bool(name)
    base = _slug(name) if name else "claude"
    if requested and _session_exists(base):
        return base, (f"session '{base}' already exists — attach with: clanker open {base}")
    name = base if requested else _unique_name(base)

    r = subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", cwd,
                        "-x", "220", "-y", "50"], capture_output=True, text=True)
    if r.returncode != 0:
        return None, "tmux new-session failed: " + (r.stderr or "").strip()[:200]

    if not shell:
        _wait_shell_prompt(name)
        # Launch literally (-l) so nothing is treated as a flag; Enter submits.
        subprocess.run(["tmux", "send-keys", "-t", name, "-l", "--", launch_cmd(cwd, resume=resume)],
                       capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", name, "Enter"], capture_output=True)
        _accept_trust_prompt(name)

    _record_last(name)
    what = "shell" if shell else "claude (sandbox off, permissions skipped)"
    return name, (f"started {what} session '{name}' in {cwd}\n"
                  f"open it with:  clanker open {name}    (or: clanker open)")


def attach(name=None):
    """Attach to a session (default: the most recent `new`). Replaces this process
    with the tmux client when on a TTY; otherwise prints the command to run.
    Returns (ok, message)."""
    if not _tmux():
        return False, "tmux is not installed."
    name = _slug(name) if name else read_last()
    if not name:
        return False, "no session to open — start one with: clanker new [name]"
    if not _session_exists(name):
        return False, f"no session named '{name}' — start one with: clanker new {name}"

    inside_tmux = bool(os.environ.get("TMUX"))
    # From inside tmux, switch the client (attach would nest); else attach.
    argv = (["tmux", "switch-client", "-t", name] if inside_tmux
            else ["tmux", "attach-session", "-t", name])

    import sys
    if inside_tmux:
        subprocess.run(argv)
        return True, f"switched to '{name}'"
    if sys.stdout.isatty():
        os.execvp("tmux", argv)   # replace this process with the interactive client
    return True, f"run:  tmux attach -t {name}"
