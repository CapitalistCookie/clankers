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
import sys

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


def ensure_boot_entry(name, project_path):
    """UPSERT name→path in the boot map WITHOUT touching tmux (the session
    already exists — e.g. `clanker work` just spawned it). Crash-resilience
    law (2026-07-22 OOM postmortem, audit M1): a session missing from the
    boot map silently dies with the box, so every registered-project work
    session registers here, not only those born via `init`/`tmux add`.
    Returns True when the map was (re)written, False when already current."""
    project_path = os.path.abspath(os.path.expanduser(project_path))
    entries = startup_entries()
    old = entries.get(name)
    if old == project_path:
        return False
    entries[name] = project_path
    write_startup(entries)
    print(f"Boot entry: {name} → {project_path}" + (f" (was {old})" if old else ""))
    return True


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

    ensure_boot_entry(name, project_path)
    return True


def _alive_sessions():
    """Names of currently-running tmux sessions (empty set when the server is
    down — which is exactly the resurrect case)."""
    try:
        out = subprocess.check_output(["tmux", "list-sessions", "-F", "#{session_name}"],
                                      stderr=subprocess.DEVNULL, text=True)
        return set(out.split())
    except Exception:
        return set()


def resurrect(dry_run=False, sync_registry=False):
    """One-command fleet recovery (audit P10, born of the 07-22 host-OOM):
    refresh the boot map, then execute the map script itself, whose per-entry
    has-session guard relaunches ONLY the missing sessions. That is the exact
    code path systemd runs at boot, so resurrect can never drift from proven
    boot behavior. By DEFAULT recovery is map-scoped — the registry only
    refreshes the paths of names already mapped, it never grows the fleet
    (the live registry↔fleet naming diverges enough that a blind union would
    have spawned ~34 unrequested sessions, per the 07-22 dry-run receipt);
    --sync-registry opts into adding registered projects missing from the
    map. Watchdog-friendly: exits 0 iff every mapped session is alive after."""
    from registry import Registry
    reg = Registry()
    entries = startup_entries()
    merged = dict(entries)
    unmapped = []
    for name in sorted(reg.projects):
        if name not in merged and not sync_registry:
            unmapped.append(name)
            continue
        path = os.path.abspath(os.path.expanduser(reg.get_path(name)))
        if not os.path.isdir(path):
            print(f"  skip {name}: registry path missing ({path})")
            continue
        merged[name] = path
    changed = sorted(n for n in merged if entries.get(n) != merged[n])
    kept = sorted(set(entries) - set(reg.projects))
    alive = _alive_sessions()
    missing = sorted(n for n in merged if n not in alive)
    print(f"[resurrect] boot map: {len(merged)} entries — {len(changed)} added/updated "
          f"from registry ({', '.join(changed) if changed else 'none'}), "
          f"{len(kept)} non-registry kept")
    if unmapped:
        print(f"[resurrect] {len(unmapped)} registered project(s) have no boot entry — "
              f"left alone (add + launch them with --sync-registry): {', '.join(unmapped)}")
    print(f"[resurrect] tmux now: {len(merged) - len(missing)} of {len(merged)} mapped "
          f"sessions alive" + (f"; missing: {', '.join(missing)}" if missing else ""))
    if dry_run:
        print("[resurrect] dry-run: nothing written, nothing launched")
        return 0
    write_startup(merged)
    if missing:
        print(f"[resurrect] relaunching {len(missing)} session(s) via {STARTUP_SCRIPT} …")
        r = subprocess.run(["bash", STARTUP_SCRIPT], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[resurrect] startup script exited {r.returncode}: "
                  f"{(r.stderr or '').strip()[:300]}", file=sys.stderr)
    after = _alive_sessions()
    still = sorted(n for n in merged if n not in after)
    if still:
        print(f"[resurrect] FAILED to revive {len(still)}/{len(merged)}: "
              f"{', '.join(still)}", file=sys.stderr)
        return 1
    print(f"[resurrect] all {len(merged)} mapped sessions alive")
    return 0


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
