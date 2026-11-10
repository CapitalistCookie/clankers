"""Claude Code's live session registry, read across every account slot.

Claude Code writes one JSON record per live REPL into <config dir>/sessions/<pid>.json
(pid, procStart, sessionId, status, cwd, `tmux` ref "<session>:@<win>.%<pane>"). Each
account slot has its own config dir, so a fleet on several slots has several
registries: a reader of one dir sees only part of the fleet (2026-10-29: five a3
sessions were invisible to the dashboard, each shown as `waiting` forever).

A record is live only while its pid runs with the same kernel start time, so a
reused pid never vouches for a dead session. Each record is annotated with
`pane_id`, `tmux_session`, `slot` (the slot whose registry holds it) and
`sessions_dir`. No function here raises; an unreadable dir is an empty one.
"""
import json
import os
import re

import accounts

_TMUX_REF_RX = re.compile(r"^(.+):@(\d+)\.%(\d+)$")


def proc_start(pid):
    """Kernel start time of pid (clock ticks since boot; /proc/<pid>/stat field
    22), or None when it is not running."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        return stat[stat.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def proc_children(pid):
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            return [int(x) for x in f.read().split()]
    except (OSError, ValueError):
        return []


def alive(rec):
    """True while the record's pid runs with the recorded start time."""
    start = proc_start(rec.get("pid"))
    return start is not None and str(rec.get("procStart", "")) == start


def read_dir(d, slot_id=None):
    """The live records of one registry dir."""
    live = []
    try:
        names = os.listdir(d)
    except OSError:
        return live
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name)) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("pid"), int):
            continue
        if not alive(rec):
            continue
        m = _TMUX_REF_RX.match(rec.get("tmux") or "")
        rec["pane_id"] = f"%{m.group(3)}" if m else None
        rec["tmux_session"] = m.group(1) if m else None
        rec["slot"] = slot_id
        rec["sessions_dir"] = d
        live.append(rec)
    return live


def read_all(sl=None):
    """The live records of every slot's registry, one per pid."""
    if sl is None:
        sl, _err = accounts.slots_or_home()
    out, seen = [], set()
    for slot_id, d in accounts.sessions_dirs(sl):
        for rec in read_dir(d, slot_id):
            if rec["pid"] in seen:
                continue
            seen.add(rec["pid"])
            out.append(rec)
    return out


def record_for_pane(pane, registry):
    """The record for a tmux pane ({pane_id, pane_pid}): by pane id first, then by
    process tree (the pane's shell or its direct child is the REPL)."""
    pane_id = pane.get("pane_id")
    if pane_id:
        for rec in registry:
            if rec.get("pane_id") == pane_id:
                return rec
    ppid = pane.get("pane_pid")
    if ppid:
        candidates = {ppid, *proc_children(ppid)}
        for rec in registry:
            if rec.get("pid") in candidates:
                return rec
    return None


def records_for_session(name, registry):
    """The records whose tmux ref names session `name` exactly."""
    return [r for r in registry if r.get("tmux_session") == name]
