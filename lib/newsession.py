"""`clanker new` / `clanker open` — spawn and attach throwaway Claude Code sessions.

`clanker new [name]` opens a detached tmux session anchored at the current directory
and launches Claude with the sandbox disabled and permission prompts skipped
(`CLAUDE_CODE_DISABLE_SANDBOX=1 claude --dangerously-skip-permissions`). `clanker open
[name]` (aliases: `attach`, `test`) attaches to it — defaulting to the most recent one
`new` created. Works from anywhere; nothing here is project-specific.
"""
import json
import os
import pwd
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


def _usable_shell(path):
    return bool(path) and os.path.isabs(path) and os.access(path, os.X_OK)


def login_shell():
    """The user's login shell — passwd first (what tmux itself picks from a sane
    environment), then $SHELL, then /bin/sh."""
    candidates = []
    try:
        candidates.append(pwd.getpwuid(os.getuid()).pw_shell)
    except (KeyError, OSError):
        pass
    candidates.append(os.environ.get("SHELL"))
    for c in candidates:
        if _usable_shell(c):
            return c
    return "/bin/sh"


def ensure_default_shell(shell=None, timeout=5):
    """Make the running tmux server hand out the login shell. tmux copies $SHELL
    into its global `default-shell` ONCE, at server start, and every later
    window inherits it — so a server (re)born from cron (SHELL=/bin/sh, bare
    PATH; the */3 keepalive did exactly that on 2026-09-03) spawned login dash
    fleet-wide: `clanker new` landed at a bare `$` with `claude: not found`,
    because dash never reads .bashrc, where PATH gains the claude install, and
    the operator's workaround was `su user` to get a bash back. Returns the
    wrong value it replaced, else None (no server / already right / set failed)."""
    shell = shell or login_shell()
    p = subprocess.run(["tmux", "show-options", "-gv", "default-shell"],
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        return None                      # no server: nothing to heal yet
    current = (p.stdout or "").strip()
    if not current or current == shell:
        return None
    r = subprocess.run(["tmux", "set-option", "-g", "default-shell", shell],
                       capture_output=True, text=True, timeout=timeout)
    return current if r.returncode == 0 else None


def tmux_new_session(name, cwd, cols=220, rows=50, timeout=10):
    """`tmux new-session -d` that lands in the login shell whichever way the
    server came to be: a running server has its default-shell healed first,
    and a server this call starts inherits SHELL pinned to the login shell (a
    clanker run from a polluted pane must not breed a polluted server).
    Returns (CompletedProcess, healed_from_or_None)."""
    shell = login_shell()
    healed = ensure_default_shell(shell, timeout=timeout)
    env = {**os.environ, "SHELL": shell}
    r = subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", cwd,
                        "-x", str(cols), "-y", str(rows)],
                       capture_output=True, text=True, timeout=timeout, env=env)
    return r, healed


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


TRUST_DIALOG_RX = re.compile(r"trust this folder|trust the files|quick safety check", re.I)


def _trust_cursor(text):
    """Where the workspace-trust dialog's ❯ cursor sits relative to its Yes line:
    'yes' (on it), 'down' / 'up' (Yes is below / above the cursor), 'unknown'
    (dialog visible, layout unreadable), or None (no dialog on screen).
    Claude Code 2.1.263 lists `❯ No, exit` FIRST and `Yes, I trust this folder`
    second — a bare Enter EXITS the session (older builds pre-highlighted Yes).
    Enter is therefore only ever sent while the cursor is on Yes."""
    if not text or not TRUST_DIALOG_RX.search(text):
        return None
    lines = text.split("\n")
    cursor = next((i for i, ln in enumerate(lines) if "❯" in ln), None)
    yes = next((i for i, ln in enumerate(lines) if re.search(r"\byes\b", ln, re.I)), None)
    if cursor is None or yes is None:
        return "unknown"
    if cursor == yes:
        return "yes"
    return "down" if yes > cursor else "up"


def _answer_trust(name, where):
    """One keystroke toward Yes; True when the dialog was confirmed."""
    if where == "yes":
        subprocess.run(["tmux", "send-keys", "-t", name, "Enter"], capture_output=True)
        return True
    if where in ("down", "up"):
        subprocess.run(["tmux", "send-keys", "-t", name, "Down" if where == "down" else "Up"],
                       capture_output=True)
    return False


def accept_trust_prompt(name, timeout=7.0, interval=0.5):
    """Answer Claude's workspace-trust dialog with YES in pane `name` (best
    effort — the dialog may never appear). Returns True iff it was answered."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = subprocess.run(["tmux", "capture-pane", "-p", "-t", name],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return False
        where = _trust_cursor(p.stdout)
        if _answer_trust(name, where):
            return True
        time.sleep(0.3 if where in ("down", "up") else interval)
    return False


def _sessions_dir():
    override = os.environ.get("CLANKER_CLAUDE_SESSIONS_DIR")
    if override:
        return override
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(cfg, "sessions")


def _repl_registered(name):
    """True when a live Claude REPL already claims tmux session `name` in Claude
    Code's session registry (~/.claude/sessions/<pid>.json, `tmux` field) — it
    got past every startup dialog, so there is nothing left to answer."""
    try:
        for fn in os.listdir(_sessions_dir()):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(_sessions_dir(), fn)) as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if (rec.get("tmux") or "").startswith(name + ":"):
                return True
    except OSError:
        pass
    return False


def accept_trust_prompts(names, timeout=120.0, interval=2.0):
    """Boot-path variant (`clanker tmux accept-trust`): watch MANY panes for up
    to `timeout` s — a fleet launched at boot takes a while to reach the dialog
    — and answer each once. Panes whose REPL is already registered, or that no
    longer exist, are dropped. Returns the names answered.
    Why: after the 2026-09-06 reboot 37 of 63 mapped sessions sat at
    `❯ No, exit` for a day (Claude shows the dialog even with
    --dangerously-skip-permissions and never remembers it for $HOME). The boot
    map is the operator's trust declaration for exactly these repos."""
    pending = list(dict.fromkeys(names))
    answered = []
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        for name in list(pending):
            if _repl_registered(name):
                pending.remove(name)
                continue
            p = subprocess.run(["tmux", "capture-pane", "-p", "-t", name],
                               capture_output=True, text=True)
            if p.returncode != 0:
                pending.remove(name)
                continue
            if _answer_trust(name, _trust_cursor(p.stdout)):
                answered.append(name)
                pending.remove(name)
        if pending:
            time.sleep(interval)
    return answered


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

    r, healed = tmux_new_session(name, cwd)
    if r.returncode != 0:
        return None, "tmux new-session failed: " + (r.stderr or "").strip()[:200]

    if not shell:
        _wait_shell_prompt(name)
        # Launch literally (-l) so nothing is treated as a flag; Enter submits.
        subprocess.run(["tmux", "send-keys", "-t", name, "-l", "--", launch_cmd(cwd, resume=resume)],
                       capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", name, "Enter"], capture_output=True)
        accept_trust_prompt(name)

    _record_last(name)
    what = "shell" if shell else "claude (sandbox off, permissions skipped)"
    msg = (f"started {what} session '{name}' in {cwd}\n"
           f"open it with:  clanker open {name}    (or: clanker open)")
    if healed:
        msg += (f"\nhealed tmux: default-shell was {healed} (server born in a bare "
                f"env, e.g. cron) — now {login_shell()} for every new window")
    return name, msg


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
