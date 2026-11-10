"""Where sessions live, and how they start and end: `clanker open`, `new`, `close`
(docs/SESSION-ACCOUNTS.md 4c).

Every Claude session belongs to a registered project (owner, 2026-10-29: "no
session should be projectless"). It runs in the project's directory, or in a git
worktree of it, so the project's CLAUDE.md, skills, hooks and conversation history
load. Work that crosses projects is a project of its own; the global rules load in
every session anyway (~/.claude/CLAUDE.md). Plain shells may run anywhere.

Every session is kept across reboots (the boot map, lib/tmux_manager.py) until
`close` ends it. A reboot brings a kept session back as a plain shell; `open`
starts Claude in it again.
"""
import fcntl
import os
import subprocess
import time

import accounts
import claude_registry
import fleet
import newsession

EXIT_WAIT = 20.0          # s: Claude exits ~1 s after /exit (measured, session_switch)


# ─── projects ──────────────────────────────────────────────────────────────

PROJECTS_TTL = 300.0      # s: the project list is re-read at least this often


def _projects_key():
    """What the project list depends on: the registry file and the project roots
    (a new repo under a root changes the root's mtime)."""
    from registry import _registry_path
    from projects import project_roots
    key = []
    for p in [_registry_path()] + project_roots():
        try:
            st = os.stat(p)
            key.append([p, st.st_mtime_ns, st.st_size])
        except OSError:
            key.append([p, None, None])
    return key


def projects():
    """{name: real path} of every registered project whose directory exists. The
    registry's merge with the repos found under the project roots takes ~0.15 s
    (git per repo); the result is cached in a file until the registry or a root
    changes, at most PROJECTS_TTL."""
    import json
    path = os.path.join(os.environ.get("CLANKER_DATA", "/data/clanker"), "cache", "projects.json")
    key = _projects_key()
    try:
        with open(path) as f:
            d = json.load(f)
        if d.get("key") == key and 0 <= time.time() - d["at"] < PROJECTS_TTL:
            return {n: p for n, p in d["projects"].items() if os.path.isdir(p)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    from registry import Registry
    reg = Registry()
    out = {}
    for n in reg.projects:
        p = reg.get_path(n)
        if p and os.path.isdir(os.path.expanduser(p)):
            out[n] = os.path.realpath(os.path.expanduser(p))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"key": key, "at": time.time(), "projects": out}, f)
        os.replace(tmp, path)
    except OSError:
        pass
    return out


def _under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def project_of(path, paths=None):
    """(name, root) of the project that holds `path` — the deepest registered root
    that contains it, or contains the main checkout of the git worktree it is in —
    or (None, None)."""
    if not path:
        return None, None
    paths = projects() if paths is None else paths
    real = os.path.realpath(os.path.expanduser(path))
    cands = [real]
    try:
        from projects import _main_repo_path
        main = _main_repo_path(real) if os.path.isdir(real) else None
    except Exception:
        main = None
    if main and main not in cands:
        cands.append(main)
    best = (None, None)
    for c in cands:
        for n, root in paths.items():
            if _under(c, root) and (best[1] is None or len(root) > len(best[1])):
                best = (n, root)
        if best[0]:
            return best
    return best


def place_here(cwd, root, paths=None):
    """The directory a session started "here" runs in: the top of the git checkout
    `cwd` is in when that checkout belongs to the project (a worktree stays a
    worktree), else the project root."""
    try:
        p = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=3)
        top = os.path.realpath(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        top = None
    return top if top and project_of(top, paths)[1] == root else root


def keep(name, path, say=None):
    """Keep the session across reboots (the boot map) until `close`. say(text) gets
    the note (default: stderr — stdout carries a command's result)."""
    import contextlib
    import io
    import sys
    from tmux_manager import ensure_boot_entry
    say = say or (lambda t: print(t, file=sys.stderr))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ensure_boot_entry(name, path)
    except OSError as e:
        print(f"warning: the boot map was not updated ({e}): '{name}' will not come back "
              "after a reboot", file=sys.stderr)
    if buf.getvalue().strip():
        say(buf.getvalue().strip())


def forget(name):
    """Take the session out of the boot map. True when it was there."""
    from tmux_manager import startup_entries, write_startup
    entries = startup_entries()
    if name not in entries:
        return False
    del entries[name]
    write_startup(entries)
    return True


# ─── what a command names ──────────────────────────────────────────────────

def resolve_open(words, sessions, projs):
    """({name, session, project, path}, suggestions): the session or project that
    `clanker open <words>` names. A project's session has the project's name, so
    one name covers both."""
    name, how, sugg = fleet.resolve(words, sorted(set(sessions) | set(projs)))
    if not name:
        return None, sugg
    return {"name": name, "how": how, "session": name in sessions,
            "project": name if name in projs else None, "path": projs.get(name)}, []


def resolve_new(words, cwd, projs):
    """Where `clanker new [project] [name...]` starts a session:
    {project, root, place, label} or {error, word, suggestions}.
    The first word is a project when it is a project's name (case aside); else the
    words name the session, in the project of the current directory."""
    if words:
        low = {n.lower(): n for n in projs}
        p = words[0] if words[0] in projs else low.get(words[0].lower())
        if p:
            return {"project": p, "root": projs[p], "place": projs[p], "label": words[1:]}
    here, root = project_of(cwd, projs)
    if here:
        return {"project": here, "root": root, "place": place_here(cwd, root, projs), "label": list(words)}
    word = words[0] if words else None
    sugg = []
    if word:
        name, how, sugg = fleet.resolve([word], sorted(projs))
        sugg = [name] if name else sugg
    return {"error": "no-project", "word": word, "suggestions": sugg}


def session_name(project, label):
    """The new session's name: the words given, else the project's name (project-2,
    project-3 … when that is taken)."""
    if label:
        return newsession._slug(" ".join(label))
    return newsession._unique_name(newsession._slug(project))


# ─── close ─────────────────────────────────────────────────────────────────

def _tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def _lock(name):
    """The per-session lock that account switches take too: a close never runs
    during a switch of the same session."""
    from session_switch import data_dir
    d = os.path.join(data_dir(), "locks")
    os.makedirs(d, exist_ok=True)
    f = open(os.path.join(d, f"switch-{name}.lock"), "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def inspect(name, sl=None):
    """What runs in session `name`: {claude: [...], others: [...], shells, attached}
    or None when there is no such session."""
    from session_switch import prompt_box_text, conversation_files
    ps = [p for p in fleet.panes() if p["session"] == name]
    if not ps:
        return None
    if sl is None:
        sl, _err = accounts.slots_or_home()
    recs = claude_registry.read_all(sl)
    out = {"claude": [], "others": [], "shells": 0, "attached": any(p["attached"] for p in ps)}
    now = time.time()
    for p in ps:
        rec = claude_registry.record_for_pane(p, recs)
        if rec:
            s = accounts.slot_of_pid(rec["pid"], sl)
            changed = (rec.get("statusUpdatedAt") or rec.get("updatedAt") or 0) / 1000
            cwd = rec.get("cwd") or p["path"]
            # a transcript exists only once Claude got a message: nothing to resume before
            talked = bool(s and s.get("real") and rec.get("sessionId")
                          and conversation_files(s["real"], rec["sessionId"], cwd))
            out["claude"].append({
                "pane": p["pane_id"], "rec": rec, "slot": s["id"] if s else "?",
                "state": fleet.STATE.get(rec.get("status"), "starting"),
                "since_s": max(0, now - changed) if changed else None,
                "sid": rec.get("sessionId"), "cwd": cwd, "talked": talked,
                "draft": prompt_box_text(name, pane=p["pane_id"])})
        elif p["command"] == "claude":
            out["others"].append(f"claude without a registry record (pane {p['pane_id']})")
        elif p["command"] in newsession.SHELLS:
            out["shells"] += 1
        else:
            out["others"].append(f"{p['command']} (pane {p['pane_id']})")
    return out


def describe(name, info):
    lines = [f"close '{name}':"]
    for c in info["claude"]:
        what = {"working": "WORKING", "decision": "waiting for your decision",
                "waiting": "waiting for you"}.get(c["state"], c["state"])
        lines.append(f"  Claude on {c['slot']}, {what} ({fleet.ago(c['since_s'])}), {c['cwd']}")
        if c["draft"]:
            lines.append(f"  unsent text in its prompt box: {c['draft'][:120]}")
    for o in info["others"]:
        lines.append(f"  also running: {o} — it is killed")
    if info["shells"]:
        lines.append(f"  {info['shells']} shell pane(s)")
    if info["attached"]:
        lines.append("  a terminal is attached: it is detached")
    lines.append("  Claude exits cleanly; the conversation stays on disk. The session leaves the boot map.")
    return "\n".join(lines)


def close(name, *, yes=False, force=False, confirm=None, exit_wait=EXIT_WAIT, progress=None):
    """End session `name`: every Claude in it exits cleanly (/exit, so the
    conversation is saved and can be resumed), its user leases are released, the
    tmux session (and any dashboard bridge to it) is killed and it leaves the boot
    map. Returns (rc, message): 0 closed, 1 refused or failed, 2 needs -y.

    Refused without --force: a Claude that is working, unsent text in a prompt box,
    a program other than a shell or Claude. confirm(summary) -> bool asks the person
    at a terminal; None means there is none (then only -y closes). progress(text)
    hears each step. Once Claude is told to exit, Ctrl-C waits until the close ends."""
    lk = _lock(name)
    if lk is None:
        return 1, f"an account switch of '{name}' is running — try again when it ends"
    try:
        return _close_locked(name, yes, force, confirm, exit_wait, progress or (lambda _t: None))
    finally:
        lk.close()


def _close_locked(name, yes, force, confirm, exit_wait, say):
    sl, _err = accounts.slots_or_home()
    info = inspect(name, sl)
    if info is None:
        return 1, f"no session named '{name}'"
    summary = describe(name, info)
    why = []
    for c in info["claude"]:
        if c["state"] == "working":
            stuck = (c["since_s"] or 0) > fleet.STUCK_S
            why.append(f"Claude is working (no status change for {fleet.ago(c['since_s'])}"
                       + ("; probably stuck" if stuck else "") + ")")
        if c["draft"]:
            why.append("its prompt box holds unsent text")
    if info["others"]:
        why.append("other programs run in it")
    if why and not force:
        return 1, summary + "\nnot closed: " + "; ".join(why) + f"\n(close it anyway: clanker close {name} --force)"
    if not yes:
        if confirm is None:
            return 2, summary + f"\nnot a terminal: confirm with  clanker close {name} -y"
        if not confirm(summary):
            return 1, f"kept '{name}'"

    from session_switch import held_interrupts
    with held_interrupts(say):
        # ── Claude exits: C-c clears the prompt box, /exit saves and ends ──
        if info["claude"]:
            say(f"Claude in '{name}' exits…")
        for c in info["claude"]:
            _tmux("send-keys", "-t", c["pane"], "C-c")
        time.sleep(0.5)
        for c in info["claude"]:
            _tmux("send-keys", "-t", c["pane"], "-l", "--", "/exit")
            _tmux("send-keys", "-t", c["pane"], "Enter")
        deadline = time.time() + exit_wait
        while time.time() < deadline and any(claude_registry.alive(c["rec"]) for c in info["claude"]):
            time.sleep(0.25)
        left = [c for c in info["claude"] if claude_registry.alive(c["rec"])]
        if left and not force:
            return 1, (f"Claude in '{name}' did not exit within {int(exit_wait)} s — the session is "
                       f"left as it is; look at it: clanker open {name}")

        try:
            held = [l for l in accounts.leases(max_age=0) if l.get("holder") == accounts.holder(name)]
        except accounts.AccountsError:
            held = []
        for l in held:
            accounts.release(l.get("id"))

        group = {}
        for ln in _tmux("list-sessions", "-F", "#{session_name}\t#{session_group}").stdout.splitlines():
            n, _, g = ln.partition("\t")
            group[n] = g
        _tmux("kill-session", "-t", f"={name}")
        g = group.get(name)
        bridges = [n for n, gg in group.items() if g and gg == g and n.startswith("web-")]
        for b in bridges:
            _tmux("kill-session", "-t", f"={b}")
        unmapped = forget(name)
        if newsession._session_exists(name):
            return 1, f"tmux did not kill '{name}' — it still runs"

    try:
        from audit import log_change
        log_change("session-closed", name, {"claude": [{"slot": c["slot"], "session_id": c["sid"],
                                                        "cwd": c["cwd"]} for c in info["claude"]],
                                            "forced": bool(force and why), "boot_entry": unmapped})
    except Exception:
        pass
    msg = [f"closed '{name}'" + (" (and its boot entry)" if unmapped else "")]
    for c in info["claude"]:
        if c in left:
            msg.append(f"  Claude on {c['slot']} did not exit: killed with the session")
        if c["sid"] and c["talked"]:
            proj, _root = project_of(c["cwd"])
            again = (f"clanker new {c['slot']} {proj} {name} --resume {c['sid']}" if proj
                     else f"cd {c['cwd']} && claude --resume {c['sid']}  (account {c['slot']})")
            msg.append(f"  conversation {c['sid']} stays on {c['slot']} — to continue it: {again}")
    if bridges:
        msg.append(f"  closed {len(bridges)} dashboard terminal(s) on it")
    return 0, "\n".join(msg)
