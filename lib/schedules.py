"""Scheduled-work inventory + decommission planning.

WHY THIS EXISTS (2026-08-12): `clanker remove` cleaned registry, per-project
settings.json, clanker.local.md, tmux session and wiki article — and nothing on
the machine. The yon repo's last commit was 2026-06-25 and it was declared dead
2026-07-05, yet /etc/cron.d/yon-ci-nightly kept running a 16-worker pytest suite
every morning at 06:00 for 48 days, into a 3.5 MB log nobody read, on a box the
operator was actively working on. Two of the eight yon cron files had been
disabled BY HAND during an unrelated cutover; the other six were simply never
looked at.

The rule this module enforces: a project cannot be retired while it still owns
scheduled work on the machine. Creation was automated; teardown was not.

DESIGN CONSTRAINTS (each one is a scar):
  * NEVER delete. Disabling renames a cron.d file to `.disabled-<reason>-<date>`
    (the operator's existing convention) or comments a crontab line. Everything
    stays recoverable. Standing rule: never delete without explicit confirmation.
  * NEVER run sudo. /etc/cron.d and systemd are root-owned; this module PLANS
    and prints exact commands for the operator to run. Silent privilege
    escalation on a live box is how you take down something that mattered.
  * NEVER guess ownership. Matching is on PATH BOUNDARIES, so `yon` does not
    sweep up `yonmusic` or `yonlaptop-setup`. A loose substring match is exactly
    the class of bug that made an earlier fix mis-attribute a live session's
    state to a dead one.
  * Degrade gracefully. A missing crontab, absent /etc/cron.d, or no systemctl
    is a normal machine, not an error.
"""
import datetime
import os
import re
import subprocess

CROND_DIR = "/etc/cron.d"
DISABLED_RE = re.compile(r"\.disabled[-.]")
# a path token: absolute, or ~-relative
PATH_RE = re.compile(r"(?:/[A-Za-z0-9_.@+-]+)+/?|~(?:/[A-Za-z0-9_.@+-]+)+")


# ── ownership ────────────────────────────────────────────────────────────────
def _norm(p):
    if not p:
        return ""
    p = os.path.expanduser(p)
    return os.path.normpath(p)


def paths_in(text):
    """Every filesystem-looking path token in a line."""
    return {_norm(m.group(0)) for m in PATH_RE.finditer(text or "")}


def owns(text, project_path):
    """Does this command reference project_path, on a PATH BOUNDARY?

    `~/yon` owns `~/yon` and `~/yon/scripts/x.py` but NOT `~/yonmusic` —
    the bug this function exists to prevent.
    """
    target = _norm(project_path)
    if not target:
        return False
    for p in paths_in(text):
        if p == target or p.startswith(target + os.sep):
            return True
    return False


# ── sources ──────────────────────────────────────────────────────────────────
def _crond_items(crond_dir=CROND_DIR):
    out = []
    try:
        names = sorted(os.listdir(crond_dir))
    except OSError:
        return out
    for name in names:
        path = os.path.join(crond_dir, name)
        if not os.path.isfile(path):
            continue
        disabled = bool(DISABLED_RE.search(name))
        try:
            with open(path, errors="replace") as fh:
                lines = fh.read().split("\n")
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append({"source": "cron.d", "unit": name, "path": path,
                        "line_no": i, "line": s, "active": not disabled,
                        "privileged": True})
    return out


def _crontab_items(text=None, user=None):
    if text is None:
        try:
            r = subprocess.run(["crontab", "-l"], capture_output=True,
                               text=True, timeout=15)
            text = r.stdout if r.returncode == 0 else ""
        except Exception:
            text = ""
    out = []
    for i, line in enumerate((text or "").split("\n"), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append({"source": "crontab", "unit": user or "user", "path": None,
                    "line_no": i, "line": s, "active": True,
                    "privileged": False})
    return out


def _systemd_items(units=None):
    """units: optional pre-supplied list of (unit_name, exec_line) for tests."""
    out = []
    if units is None:
        units = []
        try:
            r = subprocess.run(
                ["systemctl", "list-units", "--type=timer,service", "--all",
                 "--no-legend", "--no-pager"],
                capture_output=True, text=True, timeout=20)
            names = [ln.split()[0] for ln in r.stdout.split("\n")
                     if ln.strip() and not ln.startswith("*")]
            for n in names:
                if not (n.endswith(".timer") or n.endswith(".service")):
                    continue
                p = subprocess.run(
                    ["systemctl", "show", n, "-p", "ExecStart", "-p", "FragmentPath",
                     "--no-pager"], capture_output=True, text=True, timeout=10)
                units.append((n, p.stdout))
        except Exception:
            units = []
    for name, blob in units:
        if not blob:
            continue
        out.append({"source": "systemd", "unit": name, "path": None,
                    "line_no": 0, "line": blob.replace("\n", " ").strip(),
                    "active": True, "privileged": True})
    return out


def scan(crond_dir=CROND_DIR, crontab_text=None, systemd_units=None,
         include_systemd=True):
    """Every scheduled item on this machine."""
    items = _crond_items(crond_dir) + _crontab_items(crontab_text)
    if include_systemd:
        items += _systemd_items(systemd_units)
    return items


# ── classification ───────────────────────────────────────────────────────────
def for_project(items, project_path):
    """Items this project owns (path-boundary match)."""
    return [i for i in items if owns(i["line"], project_path)]


def repo_root_of(path, roots, isdir=os.path.isdir):
    """Nearest ancestor of `path` that is a git repo and lies under `roots`.

    This is what makes the audit precise rather than noisy. Flagging every path
    under $HOME that no project claims buries the real finding under
    ~/bin/clanker, ~/bin/tmux-keepalive.sh and backup rsyncs — all correctly
    project-less. Scheduled work only has an OWNER if it lives in a repo.
    """
    roots = [_norm(r) for r in roots]
    p = _norm(path)
    while p and p != os.sep:
        if any(p == r or p.startswith(r + os.sep) for r in roots):
            if isdir(os.path.join(p, ".git")):
                return p
        elif not any(r.startswith(p + os.sep) or r == p for r in roots):
            break
        nxt = os.path.dirname(p)
        if nxt == p:
            break
        p = nxt
    return None


def unowned(items, known_paths, roots=None, isdir=os.path.isdir):
    """Active items whose owning REPO is not a registered project.

    That is the yon class: a real repo, real scheduled work, no longer known to
    clanker. Reported for triage — never auto-disabled, because some of it may
    still feed live research even though the repo is retired.

    roots: where repos live; defaults to the operator home, resolved at CALL
    time (repo law 9 — no import-time path capture, no hardcoded home).
    """
    if roots is None:
        roots = (os.path.expanduser("~"),)
    known = sorted({_norm(p) for p in known_paths if p}, key=len, reverse=True)
    out = []
    for it in items:
        if not it["active"]:
            continue
        repos = set()
        for p in paths_in(it["line"]):
            r = repo_root_of(p, roots, isdir=isdir)
            if r:
                repos.add(r)
        if not repos:
            continue                      # no owning repo -> not a project's work
        rogue = sorted(r for r in repos
                       if not any(r == k or r.startswith(k + os.sep) for k in known))
        if not rogue:
            continue
        it = dict(it)
        it["paths"] = rogue
        out.append(it)
    return out


# ── disable planning (never executed here) ───────────────────────────────────
def disable_commands(item, reason="dead-project", today=None):
    """Exact, copy-pasteable commands to DISABLE (never delete) an item."""
    day = today or datetime.date.today().isoformat()
    suffix = ".disabled-{r}-{d}".format(r=reason, d=day)
    if item["source"] == "cron.d":
        return ["sudo mv {p} {p}{s}".format(p=item["path"], s=suffix)]
    if item["source"] == "crontab":
        return ["# comment out line {n} of `crontab -e`:".format(n=item["line_no"]),
                "#   {}".format(item["line"][:120])]
    if item["source"] == "systemd":
        unit = item["unit"]
        return ["sudo systemctl disable --now {}".format(unit)]
    return []


def plan(items, reason="dead-project", today=None):
    """(commands, needs_sudo) for a set of items."""
    cmds, sudo = [], False
    seen_units = set()
    for it in items:
        if it["source"] == "cron.d":
            if it["unit"] in seen_units:      # one mv per FILE, not per line
                continue
            seen_units.add(it["unit"])
        if it.get("privileged"):
            sudo = True
        cmds.extend(disable_commands(it, reason, today))
    return cmds, sudo
