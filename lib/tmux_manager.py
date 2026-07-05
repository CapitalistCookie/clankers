"""Tmux fleet registry — safe add/remove/list + the boot-startup script as the
single source of truth for session→path mapping.

Rewritten 2026-07-05 (fleet-regression postmortem):
  - CLAUDE launch goes through newsession.launch_cmd() → explicit `cd <path> &&`
    prefix (a pane re-homed by restores/keepalives still lands Claude in-repo)
    and NO bare `--resume` picker (the legacy CMD opened a picker that was empty
    in fresh namespaces and wrong everywhere else).
  - add_session UPSERTS the startup entry (the old version skipped existing
    names, so stale legacy paths survived every rebuild — root cause #1 of the
    2026-07-05 regression).
  - startup_entries()/write_startup() expose the mapping for fleet tooling.
"""

import os
import re
import subprocess

STARTUP_SCRIPT = os.path.expanduser("~/.tmux-startup.sh")

_HEADER = """#!/bin/bash
# Tmux session startup — runs on boot via systemd; also the canonical
# session→path map (managed by clanker lib/tmux_manager.py — edit via
# `clanker tmux add/remove`, not by hand).
# Resurrect+continuum restore saved state first; this ensures sessions exist
# as a fallback. Claude is launched with an explicit cd so panes always land
# in the project dir (fleet-regression law, 2026-07-05).

sleep 2

LAUNCH='CLAUDE_CODE_DISABLE_SANDBOX=1 claude --dangerously-skip-permissions'

sessions=(
"""

_FOOTER = """)

for entry in "${sessions[@]}"; do
    name="${entry%%:*}"
    dir="${entry#*:}"
    [ -d "$dir" ] || dir="$HOME"
    if ! tmux has-session -t "$name" 2>/dev/null; then
        tmux new-session -d -s "$name" -c "$dir" -x 220 -y 50
        tmux send-keys -t "$name" -l -- "cd '$dir' && $LAUNCH"
        tmux send-keys -t "$name" Enter
    fi
done
"""


def _launch_cmd(path, resume=None):
    from newsession import launch_cmd
    return launch_cmd(path, resume=resume)


def list_sessions():
    """List all tmux sessions."""
    try:
        out = subprocess.check_output(["tmux", "ls"], stderr=subprocess.DEVNULL, text=True)
        return out.strip()
    except Exception:
        return ""


def startup_entries():
    """Parse the startup script's sessions array → {name: path}."""
    entries = {}
    try:
        with open(STARTUP_SCRIPT) as f:
            for line in f:
                m = re.match(r'\s*"([^":]+):([^"]+)"', line)
                if m:
                    entries[m.group(1)] = m.group(2)
    except OSError:
        pass
    return entries


def write_startup(entries):
    """Regenerate the whole startup script from {name: path} (sorted)."""
    body = "".join(f'    "{n}:{p}"\n' for n, p in sorted(entries.items()))
    tmp = STARTUP_SCRIPT + ".tmp"
    with open(tmp, "w") as f:
        f.write(_HEADER + body + _FOOTER)
    os.chmod(tmp, 0o755)
    os.replace(tmp, STARTUP_SCRIPT)


def add_session(name, project_path=None):
    """Ensure a tmux session exists for a project and UPSERT its boot entry."""
    if not project_path:
        project_path = os.path.expanduser(f"~/projects/{name}")
    project_path = os.path.abspath(os.path.expanduser(project_path))

    exists = subprocess.run(["tmux", "has-session", "-t", name],
                            capture_output=True).returncode == 0
    if exists:
        print(f"Tmux session '{name}' already exists — leaving as-is, registering for boot")
    else:
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", project_path,
                        "-x", "220", "-y", "50"], capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", name, "-l", "--",
                        _launch_cmd(project_path)], capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", name, "Enter"], capture_output=True)
        print(f"Created tmux session: {name} → {project_path}")

    entries = startup_entries()
    old = entries.get(name)
    entries[name] = project_path
    write_startup(entries)
    print(f"Boot entry: {name} → {project_path}" + (f" (was {old})" if old and old != project_path else ""))
    return True


def remove_session(name):
    """Remove a tmux session and its startup entry."""
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
    print(f"Killed tmux session (if it existed): {name}")
    entries = startup_entries()
    if name in entries:
        del entries[name]
        write_startup(entries)
        print(f"Removed from startup script: {name}")
    else:
        print(f"Not found in startup script: {name}")
    return True
