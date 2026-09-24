"""`clanker sync` — repo ↔ installed-hook distribution with checksum parity.

The audit's #1 structural finding (2026-07-17): the live harness's best hooks
existed only in ~/.claude, the repo's hooks ran UNPINNED from the working tree,
and the only "sync" was luck. This module makes the repo the source of truth:

  repo hooks/          (repo-run set)      --apply--> ~/.claude/hooks/clanker-dist/
  repo hooks/harness/  (vendored generic)  --apply--> ~/.claude/hooks/
  repo hooks/context-gauge.py              --apply--> ~/.claude/hooks/context-gauge.py

  repo lib/ is NOT shipped (2026-09-24): a hook must not import it.

  sync --check   parity table (sha256), exit 1 on any drift   [doctor runs this]
  sync --apply   install repo -> ~/.claude with git snapshots either side,
                 chmod +x, and per-file --selftest where supported
  sync --pin     rewrite settings.json's repo-working-tree hook paths to the
                 clanker-dist copies, so editing the repo no longer changes
                 live behavior until a sync deploys it

Hand-edit guard (2026-09-24). That night an operator rewire edited six
installed hooks and archived nine, and an apply from the old repo state would
have silently reverted all of it. apply now records the sha256 of every file
it leaves in parity in ~/.claude/hooks/.clanker-sync-state.json. Before it
copies anything it checks every installed file: one whose sha256 matches
neither the repo copy nor that record was changed outside sync (edited by
hand, or removed after an apply). Then apply installs NOTHING, prints the
three hashes, and exits 1. Carry the wanted edits into the repo first, or
re-run with --force to overwrite them. With no record at all (first run),
any installed file that differs from the repo counts as hand-edited.

Operator-specific values NEVER live in hook bodies (publint law) — they belong
in ~/.claude/harness.env, which vendored hooks source. sync never touches it.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATE_NAME = ".clanker-sync-state.json"


def _claude_dir():
    return os.environ.get("CLANKER_CLAUDE_DIR", os.path.expanduser("~/.claude"))


# The repo-run set: installed to clanker-dist and wired from there (--pin,
# 2026-07-19). Keep in sync with settings.json's clanker entries. On 2026-09-24
# agent-resume-surface.sh, subagent-resume-detect.py, prompt-check.sh and
# status-stale-nudge.sh were unwired, then removed (git history keeps them).
REPO_RUN = [
    "session-start.sh", "session-end.sh", "skill-tracker.sh",
    "subagent-tier-gate.py",
]


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _pairs(repo_root=None, claude=None):
    """Yield (label, repo_path, installed_path) for every managed file."""
    repo_root = repo_root or REPO_ROOT
    claude = claude or _claude_dir()
    hooks = os.path.join(repo_root, "hooks")
    dist = os.path.join(claude, "hooks", "clanker-dist")
    for name in REPO_RUN:
        yield ("repo-run", os.path.join(hooks, name), os.path.join(dist, name))
    cg = os.path.join(hooks, "context-gauge.py")
    if os.path.exists(cg):
        yield ("gauge", cg, os.path.join(claude, "hooks", "context-gauge.py"))
    harness = os.path.join(hooks, "harness")
    if os.path.isdir(harness):
        for name in sorted(os.listdir(harness)):
            src = os.path.join(harness, name)
            # only top-level hook FILES are distributed; docs (MANIFEST.md)
            # and subdirs (tests/) stay repo-side
            if (name == "MANIFEST.md" or name.startswith(".")
                    or not os.path.isfile(src)):
                continue
            yield ("harness", src, os.path.join(claude, "hooks", name))
    # lib/ is not shipped (2026-09-24). A lib set was added on 2026-07-22
    # because dist hooks imported `$HOOK_DIR/../lib`, but no apply ran after
    # that, so <claude>/hooks/lib never existed and those imports always
    # failed open. The last importer, session-end.sh, now inlines what it
    # used. tests/test_synccmd.py fails if a distributed hook imports lib again.


# ── last-applied state (the hand-edit guard) ────────────────────────────────

def _state_path(claude):
    return os.path.join(claude, "hooks", STATE_NAME)


def _state_key(claude, dst):
    return os.path.relpath(dst, claude)


def _load_state(claude):
    """{installed path relative to <claude>: sha256 at the last apply}."""
    try:
        with open(_state_path(claude)) as f:
            files = json.load(f).get("files", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return files if isinstance(files, dict) else {}


def _write_state(claude, files, head):
    path = _state_path(claude)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {
        "version": 1,
        "note": ("Written by `clanker sync`. sha256 of each managed file as last "
                 "applied (or found in parity). apply refuses to overwrite an "
                 "installed file that matches neither the repo copy nor this record."),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clanker_head": head,
        "files": dict(sorted(files.items())),
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _head(repo_root):
    try:
        return subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
    except Exception:
        return "?"


def _classify(src, dst, last):
    """(state, repo_sha, installed_sha) for one managed pair.

    state: parity | new (not installed, no record) | update (installed copy is
    the last-applied one; the repo moved on) | hand-edited | hand-removed."""
    want = _sha(src)
    if not os.path.exists(dst):
        return ("hand-removed" if last else "new"), want, None
    have = _sha(dst)
    if have == want:
        return "parity", want, have
    if last and have == last:
        return "update", want, have
    return "hand-edited", want, have


def check(repo_root=None, claude=None, quiet=False):
    """Parity table. Returns (drifted, missing, total)."""
    claude = claude or _claude_dir()
    state = _load_state(claude)
    drifted, missing, total = [], [], 0
    notes = {}
    for label, src, dst in _pairs(repo_root, claude):
        total += 1
        if not os.path.exists(src):
            missing.append((label, src, "missing-in-REPO"))
            continue
        kind, _, _ = _classify(src, dst, state.get(_state_key(claude, dst)))
        if kind in ("new", "hand-removed"):
            missing.append((label, dst, "not-installed"))
            if kind == "hand-removed":
                notes[dst] = "removed after the last apply: apply will refuse"
        elif kind != "parity":
            drifted.append((label, os.path.basename(src)))
            if kind == "hand-edited":
                notes[(label, os.path.basename(src))] = "installed copy edited outside sync: apply will refuse"
    if not quiet:
        for label, name in drifted:
            note = f"  ({notes[(label, name)]})" if (label, name) in notes else ""
            print(f"  DRIFT      [{label:8}] {name}{note}")
        for label, path, why in missing:
            note = f"  ({notes[path]})" if path in notes else ""
            print(f"  {why:<10} [{label:8}] {path}{note}")
        ok = total - len(drifted) - len(missing)
        print(f"sync: {ok}/{total} in parity"
              + ("" if not (drifted or missing) else
                 f" — {len(drifted)} drifted, {len(missing)} missing (fix: clanker sync --apply)"))
    return drifted, missing, total


def _git_snapshot(claude, msg):
    """Best-effort commit of the hooks dir in the ~/.claude repo (rollback)."""
    try:
        subprocess.run(["git", "-C", claude, "add", "-A", "hooks", "settings.json"],
                       capture_output=True, timeout=10)
        r = subprocess.run(["git", "-C", claude, "diff", "--cached", "--quiet"],
                           capture_output=True, timeout=10)
        if r.returncode != 0:
            subprocess.run(["git", "-C", claude, "commit", "-q", "-m", msg],
                           capture_output=True, timeout=15)
            return True
    except Exception:
        pass
    return False


def _selftest(path):
    """Run `<hook> --selftest` when the file advertises one. None = no selftest.

    Only a .sh or .py file can have one. The harness README names the flag in
    its text, so the substring test alone ran `python3 README.md --selftest`
    and reported a false SELFTEST FAILED (2026-09-24)."""
    if not path.endswith((".sh", ".py")):
        return None
    try:
        with open(path, errors="ignore") as f:
            if "--selftest" not in f.read():
                return None
    except OSError:
        return None
    interp = "bash" if path.endswith(".sh") else "python3"
    r = subprocess.run([interp, path, "--selftest"],
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def record_baseline(repo_root=None, claude=None):
    """Record every pair that is in parity NOW as last-applied; install nothing.

    For an install that reached parity without an apply (hand copies, as on
    2026-09-24), so the next apply has a baseline. Returns the entry count."""
    repo_root = repo_root or REPO_ROOT
    claude = claude or _claude_dir()
    state = _load_state(claude)
    managed = set()
    for _label, src, dst in _pairs(repo_root, claude):
        key = _state_key(claude, dst)
        managed.add(key)
        if os.path.exists(src) and os.path.exists(dst) and _sha(src) == _sha(dst):
            state[key] = _sha(dst)
    state = {k: v for k, v in state.items() if k in managed}
    _write_state(claude, state, _head(repo_root))
    return len(state)


def apply(repo_root=None, claude=None, force=False):
    repo_root = repo_root or REPO_ROOT
    claude = claude or _claude_dir()
    state = _load_state(claude)
    managed, parity, todo, refused = set(), {}, [], []
    for label, src, dst in _pairs(repo_root, claude):
        key = _state_key(claude, dst)
        managed.add(key)
        if not os.path.exists(src):
            continue
        kind, want, have = _classify(src, dst, state.get(key))
        if kind == "parity":
            parity[key] = have
        elif kind in ("new", "update") or force:
            todo.append((label, src, dst, key, kind))
        else:
            refused.append((label, dst, kind, want, have, state.get(key)))

    new_state = {k: v for k, v in state.items() if k in managed}
    new_state.update(parity)
    head = _head(repo_root)

    # All or nothing: a hand-changed file may be part of a set of edits made
    # together (tonight: a dispatcher plus its gates), so no partial install.
    if refused:
        for label, dst, kind, want, have, last in refused:
            why = ("edited outside clanker sync" if kind == "hand-edited"
                   else "removed after the last apply")
            print(f"  REFUSED    [{label:8}] {dst}: installed copy {why}", file=sys.stderr)
            print(f"               repo          {want}", file=sys.stderr)
            print(f"               installed     {have or '(missing)'}", file=sys.stderr)
            print(f"               last applied  {last or '(no record)'}", file=sys.stderr)
        _write_state(claude, new_state, head)
        print(f"sync: installed NOTHING — {len(refused)} installed file(s) changed outside "
              f"clanker sync. Carry the wanted edits into the repo, or use --force to "
              f"overwrite them.", file=sys.stderr)
        return 1

    if todo:
        _git_snapshot(claude, "sync: pre-apply snapshot")
    installed, failures = [], []
    for label, src, dst, key, kind in todo:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        new_state[key] = _sha(dst)
        st = _selftest(dst)
        forced = "  (--force: overwrote a copy changed outside sync)" if kind not in ("new", "update") else ""
        if st is False:
            failures.append(dst)
            print(f"  INSTALLED  {os.path.basename(dst)}  — SELFTEST FAILED{forced}", file=sys.stderr)
        else:
            note = "" if st is None else "  (selftest PASS)"
            print(f"  installed  [{label:8}] {os.path.basename(dst)}{note}{forced}")
        installed.append(dst)
    _write_state(claude, new_state, head)
    if not installed:
        print("sync: nothing to install — already in parity")
    else:
        _git_snapshot(claude, f"sync: apply from clanker@{head}")
    if failures:
        print(f"sync: {len(failures)} selftest FAILURE(S) — the installed copies are "
              f"live; fix and re-apply, or git-revert in {claude}", file=sys.stderr)
        return 1
    return 0


def pin(repo_root=None, claude=None, force=False):
    """Rewrite settings.json: repo-working-tree hook paths -> clanker-dist copies."""
    repo_root = repo_root or REPO_ROOT
    claude = claude or _claude_dir()
    rc = apply(repo_root, claude, force=force)   # dist copies must exist and be current
    if rc != 0:
        print("pin: aborting — apply did not complete (see above)", file=sys.stderr)
        return rc
    settings = os.path.join(claude, "settings.json")
    with open(settings) as f:
        raw = f.read()
    json.loads(raw)                        # must be valid before we touch it
    hooks_prefix = os.path.join(repo_root, "hooks") + "/"
    dist_prefix = os.path.join(claude, "hooks", "clanker-dist") + "/"
    changed = 0
    for name in REPO_RUN:
        old, new = hooks_prefix + name, dist_prefix + name
        if old in raw:
            if not os.path.exists(new):
                print(f"pin: dist copy missing for {name} — refusing", file=sys.stderr)
                return 1
            raw = raw.replace(old, new)
            changed += 1
    if not changed:
        print("pin: settings.json already points at clanker-dist (or repo paths absent)")
        return 0
    json.loads(raw)                        # still valid after substitution
    bak = settings + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(settings, bak)
    # Keep the newest 3 backups (audit L6: they accumulated forever; the git
    # snapshots either side of every apply are the real rollback story).
    baks = sorted(f for f in os.listdir(claude) if f.startswith("settings.json.bak-"))
    for stale in baks[:-3]:
        try:
            os.remove(os.path.join(claude, stale))
        except OSError:
            pass
    tmp = settings + ".tmp"
    with open(tmp, "w") as f:
        f.write(raw)
    os.replace(tmp, settings)
    # verify every command path referenced by hooks config exists
    cfg = json.loads(raw)
    dangling = []
    for event, matchers in (cfg.get("hooks") or {}).items():
        for m in matchers:
            for h in m.get("hooks", []):
                cmd = h.get("command", "")
                for tok in cmd.split():
                    if tok.startswith("/") and ("/hooks/" in tok) and not os.path.exists(tok):
                        dangling.append(tok)
    if dangling:
        shutil.copy2(bak, settings)
        print("pin: ROLLED BACK — dangling hook paths after rewrite:", file=sys.stderr)
        for d in dangling:
            print(f"  {d}", file=sys.stderr)
        return 1
    _git_snapshot(claude, f"sync: pin — {changed} repo-run hooks now wired to clanker-dist")
    print(f"pin: {changed} hook path(s) now pinned to clanker-dist (backup: {bak})")
    print("pin: repo edits no longer change live behavior until `clanker sync --apply`")
    return 0


def run(mode="check", force=False):
    if mode == "apply":
        return apply(force=force)
    if mode == "pin":
        return pin(force=force)
    drifted, missing, _ = check()
    return 1 if (drifted or missing) else 0
