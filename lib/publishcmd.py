"""`clanker publish` — sanitized sync to the public sister repo.

Before this, publishing meant hand-copying files into the public repo; the two
trees drifted BOTH directions (public still carried adapters/ after the main
repo deleted it, main fixes never landed publicly). This makes it one gated
command:

  publish --check   dry-run: what would copy / delete / be blocked
  publish           copy + delete + gates + commit in the public repo
  publish --push    ... and push origin

Model:
- MANAGED = files tracked in the main repo minus EXCLUDE. Copied verbatim
  (main is publint-clean by CI, so copies are sanitized by construction).
- PUBLIC-ONLY files (LICENSE, SETUP.md, AGENTS.md, screenshots, the public
  README/CLAUDE) are never touched: anything outside the managed set is
  preserved.
- STALE cleanup: the public repo's .publish-manifest.json records what the
  pipeline manages; files that leave the managed set get deleted publicly.
  First run bootstraps the manifest as (paths ever tracked in main) ∩ (paths
  tracked in public) — which sweeps out old hand-copies like adapters/.
- GATES (fail-closed, run in the PUBLIC tree after staging): publint pattern
  scan + gitleaks (if installed). Any hit rolls the public tree back.
"""

import json
import os
import shutil
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXCLUDE = {
    "STATUS.md",              # operator state — never public
    "CLAUDE.md",              # public repo keeps its own
    "README.md",              # public repo keeps its own
    "bin/secret",             # operator secret-store tool (historical exclusion)
    "docs/SELF_IMPROVING_LOOP.md",   # operator-specific narrative (historical)
}

MANIFEST = ".publish-manifest.json"


def _public_repo():
    p = os.environ.get("CLANKER_PUBLIC_REPO",
                       os.path.expanduser("~/projects/clankers-public"))
    return p


def _git(repo, *args, check=True, capture=True):
    r = subprocess.run(["git", "-C", repo, *args],
                       capture_output=capture, text=True, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {r.stderr.strip()[:300]}")
    return r.stdout if capture else ""


def _tracked(repo):
    return set(_git(repo, "ls-files").splitlines())


def _managed_now(repo_root):
    return {f for f in _tracked(repo_root)
            if f not in EXCLUDE and not f.startswith(".pytest_cache")}


def _managed_head(repo_root):
    head = set(_git(repo_root, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
    return {f for f in head if f not in EXCLUDE and not f.startswith(".pytest_cache")}


def _extract_head(repo_root, dest):
    """git archive HEAD -> dest (modes preserved by tar)."""
    import tarfile
    import io
    r = subprocess.run(["git", "-C", repo_root, "archive", "HEAD"],
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"git archive failed: {r.stderr.decode()[:200]}")
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
        tf.extractall(dest, filter="data")   # 3.14 default; explicit now (audit L5)


def _manifest_prev(public, repo_root):
    path = os.path.join(public, MANIFEST)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return set(json.load(f).get("managed", []))
        except (OSError, json.JSONDecodeError):
            pass
    # Bootstrap: every path EVER tracked in main ∩ tracked in public now —
    # catches stale hand-copies (adapters/, skills/, lib/orchestrate.py ...).
    ever = set(_git(repo_root, "log", "--all", "--diff-filter=A",
                    "--name-only", "--format=").splitlines()) - {""}
    return ever & _tracked(public)


def _publint_scan(public, files):
    """Main's publint patterns over the staged code files in the public tree.
    Identity patterns come from the operator-side ~/.claude/publint-ids.txt
    (same source as ci/publint.sh) — never hardcoded here."""
    import re
    home_lit = "/home/" + "user"
    slug_lit = "-home-" + "user"
    parts = [home_lit, slug_lit, r"192\.168\.[0-9]"]
    ids_file = os.path.expanduser("~/.claude/publint-ids.txt")
    if os.path.exists(ids_file):
        with open(ids_file) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts.append(line)
    pat = re.compile("|".join(parts))
    hits = []
    for f in files:
        if not f.endswith((".py", ".sh", ".js")):
            continue
        p = os.path.join(public, f)
        if not os.path.exists(p):
            continue
        try:
            with open(p, errors="ignore") as fh:
                if pat.search(fh.read()):
                    hits.append(f)
        except OSError:
            pass
    return hits


def publish(check_only=False, push=False):
    public = _public_repo()
    if not os.path.isdir(os.path.join(public, ".git")):
        print(f"publish: public repo not found at {public} "
              f"(set CLANKER_PUBLIC_REPO)", file=sys.stderr)
        return 1
    if _git(public, "status", "--porcelain").strip():
        print(f"publish: public repo is dirty — commit/stash it first ({public})",
              file=sys.stderr)
        return 1

    # Publish COMMITTED state only: managed names and contents come from HEAD
    # (git archive), never the working tree — an uncommitted edit can neither
    # ship nor trip the gates (found when concurrent worktree churn flaked the
    # publish test through the publint gate, 2026-07-19).
    managed = _managed_head(REPO_ROOT)
    prev = _manifest_prev(public, REPO_ROOT)
    # EXCLUDE is immune to the stale sweep: those paths are public-owned
    # (their public versions are deliberate), not main-origin leftovers —
    # without this, bootstrap deleted the public README/CLAUDE.md.
    deletions = sorted(f for f in (prev - managed - EXCLUDE)
                       if os.path.exists(os.path.join(public, f)))
    copies = sorted(managed)

    import tempfile
    with tempfile.TemporaryDirectory(prefix="clanker-publish-") as headtree:
        _extract_head(REPO_ROOT, headtree)

        if check_only:
            changed = []
            for f in copies:
                src, dst = os.path.join(headtree, f), os.path.join(public, f)
                if not os.path.exists(src):
                    continue
                if not os.path.exists(dst):
                    changed.append(("NEW", f))
                else:
                    with open(src, "rb") as a, open(dst, "rb") as b:
                        if a.read() != b.read():
                            changed.append(("CHANGED", f))
            for kind, f in changed[:40]:
                print(f"  {kind:8} {f}")
            for f in deletions[:40]:
                print(f"  DELETE   {f}")
            print(f"publish --check: {len(changed)} to copy, {len(deletions)} stale to delete, "
                  f"{len(copies) - len(changed)} identical")
            return 0

        for f in copies:
            src, dst = os.path.join(headtree, f), os.path.join(public, f)
            if not os.path.exists(src):
                continue
            os.makedirs(os.path.dirname(dst) or public, exist_ok=True)
            shutil.copy2(src, dst)
    for f in deletions:
        _git(public, "rm", "-q", "--ignore-unmatch", "-f", f)
        # git rm leaves empty dirs on disk sometimes; harmless.

    head = _git(REPO_ROOT, "rev-parse", "--short", "HEAD").strip()
    with open(os.path.join(public, MANIFEST), "w") as f:
        json.dump({"managed": sorted(managed), "source_commit": head}, f, indent=1)
    _git(public, "add", "-A")

    # ── gates (fail-closed) ──
    staged = _git(public, "diff", "--cached", "--name-only").splitlines()
    hits = _publint_scan(public, [f for f in managed])
    if hits:
        _git(public, "reset", "--hard", "HEAD")
        _git(public, "clean", "-fdq")
        print("publish: BLOCKED — operator-specific patterns in the staged tree:",
              file=sys.stderr)
        for h in hits[:20]:
            print(f"  {h}", file=sys.stderr)
        return 1
    if shutil.which("gitleaks"):
        cfgargs = []
        if os.path.exists(os.path.join(public, ".gitleaks.toml")):
            cfgargs = ["--config", os.path.join(public, ".gitleaks.toml")]
        r = subprocess.run(
            ["gitleaks", "detect", "--no-git", "--source", public, *cfgargs],
            capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            _git(public, "reset", "--hard", "HEAD")
            _git(public, "clean", "-fdq")
            print("publish: BLOCKED — gitleaks findings in the staged tree:",
                  file=sys.stderr)
            print((r.stdout + r.stderr)[-1000:], file=sys.stderr)
            return 1

    if not staged:
        print("publish: nothing to publish — public already in sync")
        return 0
    _git(public, "commit", "-q", "-m",
         f"publish: sync from clanker@{head}\n\n"
         f"{len(staged)} file(s) staged; {len(deletions)} stale removed. "
         f"Gates: publint clean" + (", gitleaks clean" if shutil.which("gitleaks") else "") + ".")
    print(f"publish: committed {len(staged)} file(s) "
          f"({len(deletions)} stale removed) in {public} [source clanker@{head}]")
    if push:
        r = subprocess.run(["git", "-C", public, "push", "origin", "HEAD"],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            print(f"publish: PUSH FAILED: {r.stderr.strip()[:300]}", file=sys.stderr)
            return 1
        print("publish: pushed to origin")
    return 0
