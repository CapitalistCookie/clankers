"""`clanker sync` — repo ↔ installed-hook distribution with checksum parity.

The audit's #1 structural finding (2026-07-17): the live harness's best hooks
existed only in ~/.claude, the repo's hooks ran UNPINNED from the working tree,
and the only "sync" was luck. This module makes the repo the source of truth:

  repo hooks/          (repo-run set)      --apply--> ~/.claude/hooks/clanker-dist/
  repo hooks/harness/  (vendored generic)  --apply--> ~/.claude/hooks/
  repo hooks/context-gauge.py              --apply--> ~/.claude/hooks/context-gauge.py
  repo lib/*.py        (hook import root)  --apply--> ~/.claude/hooks/lib/

  sync --check   parity table (sha256), exit 1 on any drift   [doctor runs this]
  sync --apply   install repo -> ~/.claude with git snapshots either side,
                 chmod +x, and per-file --selftest where supported
  sync --pin     rewrite settings.json's repo-working-tree hook paths to the
                 clanker-dist copies, so editing the repo no longer changes
                 live behavior until a sync deploys it

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


def _claude_dir():
    return os.environ.get("CLANKER_CLAUDE_DIR", os.path.expanduser("~/.claude"))


# The repo-run set: wired from the repo working tree today; --pin moves the
# wiring to the dist copies. Keep in sync with settings.json's clanker entries.
REPO_RUN = [
    "session-start.sh", "session-end.sh", "prompt-check.sh", "skill-tracker.sh",
    "status-stale-nudge.sh", "agent-resume-surface.sh",
    "subagent-resume-detect.py", "subagent-tier-gate.py",
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
    # Dist hooks import repo modules via `$HOOK_DIR/../lib` — i.e.
    # <claude>/hooks/lib when running from clanker-dist. Nothing shipped that
    # dir, so after --pin (2026-07-19) every lib import in the dist set failed
    # open: briefings, handoffs, and git-aware project resolution silently
    # stopped (found 2026-07-22: newest handoff predated the pin). Ship the
    # whole top level so cross-imports never need dependency-chasing;
    # subpackages (orch/, ecc/) are CLI/dashboard-side, not hook-side.
    lib = os.path.join(repo_root, "lib")
    if os.path.isdir(lib):
        for name in sorted(os.listdir(lib)):
            src = os.path.join(lib, name)
            if not name.endswith(".py") or not os.path.isfile(src):
                continue
            yield ("lib", src, os.path.join(claude, "hooks", "lib", name))


def check(repo_root=None, claude=None, quiet=False):
    """Parity table. Returns (drifted, missing, total)."""
    drifted, missing, total = [], [], 0
    for label, src, dst in _pairs(repo_root, claude):
        total += 1
        if not os.path.exists(src):
            missing.append((label, src, "missing-in-REPO"))
            continue
        if not os.path.exists(dst):
            missing.append((label, dst, "not-installed"))
            continue
        if _sha(src) != _sha(dst):
            drifted.append((label, os.path.basename(src)))
    if not quiet:
        for label, name in drifted:
            print(f"  DRIFT      [{label:8}] {name}")
        for label, path, why in missing:
            print(f"  {why:<10} [{label:8}] {path}")
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
    """Run `<hook> --selftest` when the file advertises one. None = no selftest."""
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


def apply(repo_root=None, claude=None):
    repo_root = repo_root or REPO_ROOT
    claude = claude or _claude_dir()
    _git_snapshot(claude, "sync: pre-apply snapshot")
    installed, failures = [], []
    for label, src, dst in _pairs(repo_root, claude):
        if not os.path.exists(src):
            continue
        if os.path.exists(dst) and _sha(src) == _sha(dst):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        # lib files are MODULES, not hooks — a "--selftest" string inside one
        # is coincidental; executing `python3 <module> --selftest` proves nothing.
        st = None if label == "lib" else _selftest(dst)
        if st is False:
            failures.append(dst)
            print(f"  INSTALLED  {os.path.basename(dst)}  — SELFTEST FAILED", file=sys.stderr)
        else:
            note = "" if st is None else "  (selftest PASS)"
            print(f"  installed  [{label:8}] {os.path.basename(dst)}{note}")
        installed.append(dst)
    if not installed:
        print("sync: nothing to install — already in parity")
    else:
        try:
            head = subprocess.check_output(
                ["git", "-C", repo_root, "rev-parse", "--short", "HEAD"],
                text=True, timeout=10).strip()
        except Exception:
            head = "?"
        _git_snapshot(claude, f"sync: apply from clanker@{head}")
    if failures:
        print(f"sync: {len(failures)} selftest FAILURE(S) — the installed copies are "
              f"live; fix and re-apply, or git-revert in {claude}", file=sys.stderr)
        return 1
    return 0


def pin(repo_root=None, claude=None):
    """Rewrite settings.json: repo-working-tree hook paths -> clanker-dist copies."""
    repo_root = repo_root or REPO_ROOT
    claude = claude or _claude_dir()
    rc = apply(repo_root, claude)          # dist copies must exist and be current
    if rc != 0:
        print("pin: aborting — apply reported selftest failures", file=sys.stderr)
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


def run(mode="check"):
    if mode == "apply":
        return apply()
    if mode == "pin":
        return pin()
    drifted, missing, _ = check()
    return 1 if (drifted or missing) else 0
