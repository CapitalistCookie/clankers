"""`clanker new` / `clanker open` — spawn and attach throwaway Claude Code sessions.

`clanker new [name]` opens a detached tmux session anchored at the current directory
and launches Claude with the sandbox disabled and permission prompts skipped
(`CLAUDE_CODE_DISABLE_SANDBOX=1 claude --dangerously-skip-permissions`). `clanker open
[name]` (aliases: `attach`, `test`) attaches to it — defaulting to the most recent one
`new` created. Works from anywhere; nothing here is project-specific.

Account slots (2026-10-29, docs/SESSION-ACCOUNTS.md): every Claude launch names the
slot the user selected (lib/accounts.py). The launch line pins CLAUDE_CONFIG_DIR
explicitly, the session holds a user lease bound to its claude pid, and a launch
counts as done only when Claude's own registry shows the pane's process on that slot.
"""
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import time

import accounts
import claude_registry

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")
_LAST_FILE = os.path.join(DATA_DIR, ".last_new_session")

LAUNCH = "CLAUDE_CODE_DISABLE_SANDBOX=1 claude --dangerously-skip-permissions"


def launch_cmd(cwd, resume=None, *, slot, session=None):
    """The one true launch line: explicit cd first, so a pane/shell that was
    re-homed by anything (rc files, restores, keepalives) still lands Claude in
    the project dir — the 2026-07-05 fleet regression was every session's
    Claude silently running at ~. resume: True → `--resume` picker (lists THIS
    project's sessions); a session-id string → resume that session directly.
    slot: the account slot (lib/accounts.py) — its env(1) prefix sets or unsets
    CLAUDE_CONFIG_DIR on every launch, so an inherited value never decides the
    account. session: the CLANKER_SESSION marker (a session clanker started)."""
    parts = [f"cd {shlex.quote(os.path.abspath(cwd))} &&"]
    env = accounts.launch_env(slot)
    if env:
        parts.append(env)
    if session:
        parts.append(f"CLANKER_SESSION={shlex.quote(session)}")
    cmd = " ".join(parts + [LAUNCH])
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


# A tmux server copies the environment it is born in into every pane it ever
# makes. Born from inside a Claude session (`clanker new` in a Bash tool), it
# would hand that session's account and identity to the whole fleet.
_SESSION_ENV = ("CLAUDE_CONFIG_DIR", "CLANKER_SESSION", "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT",
                "CLAUDE_ENV_FILE", "CLAUDE_PROJECT_DIR", "CLAUDE_SESSION_ID")
_SESSION_ENV_PARTS = ("SESSION", "MESSAGING", "ENTRYPOINT", "EXECPATH", "CHILD")


def server_env():
    """os.environ without a parent Claude session's account and identity."""
    return {k: v for k, v in os.environ.items()
            if k not in _SESSION_ENV
            and not (k.startswith("CLAUDE_CODE_") and any(x in k for x in _SESSION_ENV_PARTS))}


def tmux_new_session(name, cwd, cols=220, rows=50, timeout=10):
    """`tmux new-session -d` that lands in the login shell whichever way the
    server came to be: a running server has its default-shell healed first,
    and a server this call starts inherits SHELL pinned to the login shell (a
    clanker run from a polluted pane must not breed a polluted server).
    Returns (CompletedProcess, healed_from_or_None)."""
    shell = login_shell()
    healed = ensure_default_shell(shell, timeout=timeout)
    env = {**server_env(), "SHELL": shell}
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
    """True iff a session named EXACTLY `name` exists. The `=` target never
    prefix-matches: a bare `-t clanker` resolved to `clanker-41` while no
    `clanker` ran, so `clanker work clanker` reported the session as present
    and `clanker open` attached to the other one (2026-09-24)."""
    return subprocess.run(["tmux", "has-session", "-t", f"={name}"],
                          capture_output=True).returncode == 0


SHELLS = ("bash", "zsh", "sh", "dash", "fish")


def _pane_command(name):
    """Foreground command of the active pane of the session named EXACTLY
    `name` ('' when there is no such session). The `=` target never
    prefix-matches, so `web` can never resolve to `web-2`."""
    p = subprocess.run(["tmux", "display-message", "-p", "-t", f"={name}:",
                        "#{pane_current_command}"], capture_output=True, text=True)
    return _norm_command((p.stdout or "").strip()) if p.returncode == 0 else ""


def _norm_command(cmd):
    """`claude.exe` (the npm package's real binary) is Claude too (serve.py
    CLAUDE_PANE_COMMANDS: the .exe form once hid a pane for days)."""
    return "claude" if cmd in ("claude", "claude.exe") else cmd


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
    """The home slot's registry dir (kept for callers that name one dir)."""
    override = os.environ.get("CLANKER_CLAUDE_SESSIONS_DIR")
    if override:
        return override
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(cfg, "sessions")


def _repl_registered(name):
    """True when a Claude REPL already claims tmux session `name` in Claude
    Code's session registry (<config dir>/sessions/<pid>.json, `tmux` field) of
    ANY account slot — it got past every startup dialog, so there is nothing
    left to answer."""
    sl, _err = accounts.slots_or_home()
    for _slot, d in accounts.sessions_dirs(sl):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fn)) as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and (rec.get("tmux") or "").startswith(name + ":"):
                return True
    # the record may name the dashboard's grouped web-* session: match the pane id
    return pane_record(pane_info(name)) is not None


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


def pane_info(name):
    """{pane_id, pane_pid, command, path} of the active pane of session `name`
    (exact target), or None when there is no such session."""
    p = subprocess.run(["tmux", "display-message", "-p", "-t", f"={name}:",
                        "#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    parts = (p.stdout or "").rstrip("\n").split("\t")
    if len(parts) < 4:
        return None
    try:
        pid = int(parts[1])
    except ValueError:
        pid = None
    return {"pane_id": parts[0], "pane_pid": pid, "command": _norm_command(parts[2]), "path": parts[3]}


def would_launch(name):
    """True when `work`/`new` with this session name would start Claude: no such
    session, or one whose active pane is a bare shell."""
    return not name or not _session_exists(_slug(name)) or _pane_command(_slug(name)) in SHELLS


LAUNCH_GRACE = 3.0       # s: a launch still at a shell prompt after this never started Claude
_NOT_FOUND_RX = re.compile(r"No such file or directory|command not found|not found", re.I)


def _pane_tail(name, lines=4):
    p = subprocess.run(["tmux", "capture-pane", "-p", "-t", f"={name}:"], capture_output=True, text=True)
    rows = [ln.rstrip() for ln in (p.stdout or "").split("\n") if ln.strip()]
    return "\n".join(rows[-lines:])


def wait_registered(name, timeout=60.0, exclude_pids=(), interval=0.5, info=None):
    """Wait for a live registry record (any slot) whose tmux ref names session
    `name`, answering the workspace-trust dialog on the way (it appears per
    slot: each slot keeps its own trust list). Returns the record, or None at
    the timeout, when the pane is gone, or as soon as the launch has ended —
    the pane is back at a shell and Claude never ran (2026-10-29: a launch
    during Claude Code's npm relink got `env: 'claude': No such file or
    directory`), or Claude ran and quit. `info["exited"]` then holds the
    pane's last lines."""
    t0 = time.time()
    deadline = t0 + timeout
    seen = False
    pane = pane_info(name)
    while time.time() < deadline:
        p = subprocess.run(["tmux", "capture-pane", "-p", "-t", f"={name}:"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return None
        where = _trust_cursor(p.stdout)
        if where:
            _answer_trust(f"={name}:", where)
        rec = pane_record(pane, exclude_pids)
        if rec:
            return rec
        cmd = _pane_command(name)
        if cmd and cmd not in SHELLS:
            seen = True
        elif cmd in SHELLS and (seen or time.time() - t0 > LAUNCH_GRACE) and not where:
            if info is not None:
                info["exited"] = _pane_tail(name)
            return None
        time.sleep(0.3 if where in ("down", "up") else interval)
    return None


def pane_record(pane, exclude_pids=()):
    """The live registry record (any slot) of the Claude in this pane. Matched by
    PANE ID, never by session name: Claude writes the name of the session its
    client came through, and while the dashboard's terminal is open that is the
    grouped `web-*` bridge session (2026-10-29 walk: `web-9e96f80f:@360.%391`
    for pane %391 of `walk-a3`)."""
    if not pane:
        return None
    recs = [r for r in claude_registry.read_all() if r["pid"] not in exclude_pids]
    return claude_registry.record_for_pane(pane, recs)


def _send_line(name, line):
    """Type one line literally (-l: nothing is a flag) and submit it."""
    subprocess.run(["tmux", "send-keys", "-t", f"={name}:", "-l", "--", line], capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", f"={name}:", "Enter"], capture_output=True)


def launch_timeout():
    """Seconds a launch may take to register (CLANKER_LAUNCH_TIMEOUT, default 60)."""
    try:
        return float(os.environ.get("CLANKER_LAUNCH_TIMEOUT") or 60)
    except ValueError:
        return 60.0


def start_in_pane(name, cwd, slot, resume=None, exclude_pids=(), timeout=None):
    """Launch Claude on `slot` in the existing pane of `name` (a shell at its
    prompt) and verify it: (record, None) or (None, error). The record's process
    must run on `slot` — read from its own environment, not assumed."""
    timeout = launch_timeout() if timeout is None else timeout
    line = launch_cmd(cwd, resume=resume, slot=slot, session=name)
    for attempt in (1, 2):
        # A launch line is typed ONLY at a shell. A prompt-looking last line is
        # not enough: Claude's own `❯` prompt looks like one, and a launch line
        # typed into Claude is sent to the model as a message (2026-10-29 walk).
        cmd = _pane_command(name)
        if cmd not in SHELLS:
            running = pane_record(pane_info(name), exclude_pids)
            return None, (f"session '{name}' runs {cmd or 'nothing'}, not a shell — nothing typed"
                          + (f" (Claude pid {running['pid']}, session {running.get('sessionId')})"
                             if running else ""))
        _wait_shell_prompt(name)
        _send_line(name, line)
        info = {}
        rec = wait_registered(name, timeout=timeout, exclude_pids=exclude_pids, info=info)
        if rec is not None:
            break
        tail = info.get("exited")
        if attempt == 1 and tail and _NOT_FOUND_RX.search(tail):
            time.sleep(LAUNCH_GRACE)          # the claude binary was being replaced: once more
            continue
        if tail:
            return None, (f"claude did not start in session '{name}' — the pane shows:\n{tail}\n"
                          f"(the pane is left as it is: clanker open {name})")
        return None, (f"claude did not register in session '{name}' within {int(timeout)} s "
                      f"(the pane is left as it is: clanker open {name})")
    sl, _err = accounts.slots_or_home()
    got = accounts.slot_of_pid(rec["pid"], sl)
    if got is None or got["id"] != slot["id"]:
        return None, (f"session '{name}' started on account {got['id'] if got else '?'}, "
                      f"not {slot['id']} (pid {rec['pid']}) — stop it and start again")
    if resume and resume is not True and rec.get("sessionId") != resume:
        return None, (f"session '{name}' resumed as {rec.get('sessionId')}, not {resume}")
    return rec, None


def _lease(slot, name):
    """A user lease for `name` on `slot`, bound to THIS process until the claude
    pid is known: (lease_id or None, warnings, error). When the accounts command
    fails, the home slot still launches (warning, no lease); any other slot stops."""
    try:
        r = accounts.lease_user(slot, name, os.getpid())
    except accounts.AccountsError as e:
        if slot.get("home"):
            return None, [f"no lease (accounts command failed: {e})"], None
        return None, [], f"account {slot['id']}: cannot take a lease: {e}"
    if r.get("off"):
        return None, [], None
    if r["refused"]:
        return None, [], f"account {slot['id']} cannot run: " + "; ".join(r["refused"])
    return (r["lease"] or {}).get("id"), r["warnings"], None


def _start(name, cwd, slot, resume, new_session):
    """Lease → (create the tmux session) → launch → verify → move the lease to
    the claude pid. Returns (name, message) or (None, error)."""
    lease_id, warnings, err = _lease(slot, name)
    if err:
        return None, err
    healed = None
    if new_session:
        r, healed = tmux_new_session(name, cwd)
        if r.returncode != 0:
            accounts.release(lease_id)
            return None, "tmux new-session failed: " + (r.stderr or "").strip()[:200]
    rec, err = start_in_pane(name, cwd, slot, resume=resume)
    if err:
        accounts.release(lease_id)
        return None, err
    if lease_id and not accounts.renew(lease_id, pid=rec["pid"]):
        warnings.append(f"lease {lease_id} could not move to claude pid {rec['pid']} "
                        "(the dashboard reconciler retries)")
    _record_last(name)
    verb = "started claude" if new_session else "started claude in existing session"
    msg = (f"{verb} '{name}' in {cwd}\n"
           f"account: {accounts.label(slot)}" + (f"  lease {lease_id}" if lease_id else "") + "\n"
           f"open it with:  clanker open {name}")
    if not new_session:
        msg = msg.replace(f"{verb} '{name}'", f"{verb} '{name}' (was a plain shell)")
    for w in warnings:
        msg += f"\nwarning: {w}"
    if healed:
        msg += (f"\nhealed tmux: default-shell was {healed} (server born in a bare "
                f"env, e.g. cron) — now {login_shell()} for every new window")
    return name, msg


def spawn(name=None, cwd=None, shell=False, resume=None, start_claude_in_shell=False,
          account=None):
    """Create a detached tmux session and launch Claude (or a bare shell) in it.
    account: the slot the user selected (lib/accounts.py). Required whenever
    Claude starts — the user always selects; nothing here picks one.
    start_claude_in_shell (the `clanker work` path): when the named session
    already exists and its active pane is a bare shell, start Claude there
    instead of only pointing at it. Boot and fleet-heal recreate mapped
    sessions as plain shells (Claude is on demand only, 2026-09-24), so this is
    how a mapped session gets its Claude. A pane running anything else is
    never typed into.
    Returns (name, message); name is None on failure. Never raises for the
    expected failure modes."""
    if not _tmux():
        return None, "tmux is not installed (required for `clanker new`)."
    cwd = os.path.abspath(cwd or os.getcwd())

    requested = bool(name)
    base = _slug(name) if name else "claude"
    exists = requested and _session_exists(base)
    if exists and not (not shell and start_claude_in_shell and _pane_command(base) in SHELLS):
        return base, (f"session '{base}' already exists — attach with: clanker open {base}")

    slot = None
    if not shell:
        slot, err = accounts.resolve(account)
        if err:
            return None, err
        # Seed the project's Claude memory namespace in the slot that runs it,
        # so the session isn't memory-blind. Best-effort.
        try:
            from memoryns import ensure_memory_stub
            ensure_memory_stub(cwd, config_dir=slot.get("real"))
        except Exception:
            pass

    if exists:
        return _start(base, cwd, slot, resume, new_session=False)

    name = base if requested else _unique_name(base)
    if not shell:
        return _start(name, cwd, slot, resume, new_session=True)

    r, healed = tmux_new_session(name, cwd)
    if r.returncode != 0:
        return None, "tmux new-session failed: " + (r.stderr or "").strip()[:200]
    _record_last(name)
    msg = (f"started shell session '{name}' in {cwd}\n"
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
    # Only a person at a terminal is attached or switched. A script or a Claude
    # tool call that runs `clanker open` inside tmux would otherwise move the
    # owner's own client (2026-10-29 walk: it did).
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return True, (f"run:  tmux switch-client -t {name}" if inside_tmux
                      else f"run:  tmux attach -t {name}")
    if inside_tmux:
        subprocess.run(argv)
        return True, f"switched to '{name}'"
    os.execvp("tmux", argv)   # replace this process with the interactive client
    return True, f"run:  tmux attach -t {name}"
