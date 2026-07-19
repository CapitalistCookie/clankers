"""`clanker adopt <url|path>` + `clanker doctor <project>` — one-command repo
ingestion + the project contract check (harness overhaul M4, 2026-07-05).

The contract a repo must meet to be "in the system":
  registered · git repo · CLAUDE.md (≤150 lines) · a state file (STATUS.md et al)
  · ci/fast.sh (localci) · a remote · no plaintext secrets.

adopt() converges a repo toward the contract (clone if URL, register, scaffold
missing pieces, then doctor). It never commits and never creates remotes — it
prints what it did and what remains. Green-or-refuse philosophy: doctor's exit
code is honest; a failing contract is reported, not papered over.
"""
import os
import subprocess
import sys

from registry import Registry

import yaml

CLAUDE_STUB = """# {name}

> Seeded by `clanker adopt` — refine on first real session (keep ≤100 lines; detail lives in docs/).

- **State:** `STATUS.md` (NOW / NEXT / BLOCKED / DECISIONS) — update it as part of finishing work.
- **Skills:** `.claude/skills/` · **project hooks:** `.claude/settings.json`.
- **CI:** `ci/fast.sh` (<60s, green) gates pre-push via localci; add `ci/full.sh` for the detached post-commit suite.
- Launch sessions here via `clanker work {name}`.
"""

STATUS_STUB = """# STATUS — {name} (seeded by `clanker adopt`; refine each session)

## NOW
—

## NEXT
—

## BLOCKED
—

## DECISIONS (pending operator)
—
"""

CI_STUB = """#!/usr/bin/env bash
# fast CI stub (seeded by `clanker adopt`) — replace with real checks; keep <60s and GREEN.
set -euo pipefail
echo '[ci/fast] all green (stub)'
"""

SECRET_PATTERN = r"glpat-[A-Za-z0-9._-]{15,}|cfut_[A-Za-z0-9]{30,}|PGPASSWORD='[^$]"


def _sh(args, cwd=None, timeout=120):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _print_checks(name, checks):
    print(f"=== clanker doctor: {name} ===")
    bad = 0
    for label, ok, detail in checks:
        mark = "✓" if ok else "✗"
        bad += 0 if ok else 1
        print(f"  {mark} {label:<26} {detail}")
    print(f"=== {'CONTRACT MET' if bad == 0 else f'{bad} CHECK(S) FAILING'} ===")
    return bad


def project_checks(name, include_secrets=True):
    """Build the contract check rows for one project: [(label, ok, detail), ...].

    include_secrets=False skips the repo-wide secret grep — it dominates the
    runtime on large repos, which matters when the fleet loop runs this 40×.
    """
    reg = Registry()
    path = reg.get_path(name)
    checks = []

    def add(label, ok, detail=""):
        checks.append((label, bool(ok), detail))

    add("path exists", os.path.isdir(path), path)
    if not os.path.isdir(path):
        return checks

    add("registered", name in reg.projects, "" if name in reg.projects else "run: clanker adopt " + path)
    is_git = os.path.exists(os.path.join(path, ".git"))
    add("git repo", is_git)

    cl = os.path.join(path, "CLAUDE.md")
    n = sum(1 for _ in open(cl, errors="ignore")) if os.path.exists(cl) else 0
    add("CLAUDE.md ≤150 lines", 0 < n <= 150, f"{n} lines" if n else "missing")

    state = [s for s in ("STATUS.md", "HANDOFF.md", "docs/HANDOFF.md", "STATE.md", "RESUME.md")
             if os.path.exists(os.path.join(path, s))]
    add("state file", bool(state), ", ".join(state) or "none — seed STATUS.md")

    add("ci/fast.sh or .localci",
        os.path.exists(os.path.join(path, "ci/fast.sh")) or os.path.exists(os.path.join(path, ".localci")))

    if is_git:
        _, remotes, _ = _sh(["git", "remote"], path)
        add("remote", bool(remotes), remotes.replace("\n", ", ") or "none — DR risk")
        if include_secrets:
            rc, hits, _ = _sh(["grep", "-rlIE", SECRET_PATTERN,
                               "--include=*.md", "--include=*.json", "--include=*.sh",
                               "--exclude-dir=.git", "."], path)
            add("no plaintext secrets", rc != 0, hits.replace("\n", ", ")[:100])

    # Archetype-specific contract — each project TYPE self-improves against different
    # invariants (the loop skeleton is universal; the probes are not). See
    # clanker/docs/SELF_IMPROVING_LOOP.md §"Archetype matrix".
    for label, ok, detail in _archetype_checks(reg.get_archetype(name), path):
        add(label, ok, detail)

    return checks


def doctor_project(name):
    return 1 if _print_checks(name, project_checks(name)) else 0


def doctor_fleet(include_secrets=False):
    """Contract check across EVERY registered project — one row each.

    The per-project doctor existed since the overhaul, but there was no
    fleet-wide view of the very governance it enforces (audit 2026-07-19).
    Secret grep is off by default here (40 repos × repo-wide grep); run
    `clanker doctor <name>` for the full per-project table including secrets.
    """
    reg = Registry()
    failing = 0
    print(f"{'Project':<28} {'Archetype':<12} {'Contract':<10} Failing checks")
    print("-" * 92)
    for name in sorted(reg.projects.keys()):
        checks = project_checks(name, include_secrets=include_secrets)
        fails = [label for label, ok, _ in checks if not ok]
        if fails:
            failing += 1
        status = "MET" if not fails else f"{len(fails)}/{len(checks)} ✗"
        print(f"{name:<28} {reg.get_archetype(name) or '?':<12} {status:<10} {', '.join(fails)}")
    total = len(reg.projects)
    print("-" * 92)
    print(f"{total - failing}/{total} projects meet contract"
          + ("" if not failing else f" — {failing} drifted (fix: clanker doctor <name>)"))
    return 1 if failing else 0


def _archetype_checks(archetype, path):
    """Extra contract rows per archetype. Returns [(label, ok, detail), ...]."""
    out = []
    if archetype == "research":
        # research-governance-standard: a gated <name>_spec/ dir with a prereg
        spec = any(d.endswith("_spec") and os.path.isdir(os.path.join(path, d))
                   for d in (os.listdir(path) if os.path.isdir(path) else []))
        prereg = spec and any(
            os.path.exists(os.path.join(path, d, "prereg.yml"))
            for d in os.listdir(path) if d.endswith("_spec"))
        out.append(("research: _spec/ governance dir", spec,
                    "" if spec else "no <name>_spec/ — see research-governance-standard"))
        if spec:
            out.append(("research: prereg.yml present", prereg,
                        "" if prereg else "spec dir without prereg.yml"))
    elif archetype in ("infra",):
        # infra: STATUS.md must track a backup story (the #1 gap this overhaul found)
        st = os.path.join(path, "STATUS.md")
        has_backup = os.path.exists(st) and "backup" in open(st, errors="ignore").read().lower()
        out.append(("infra: backup tracked in STATUS", has_backup,
                    "" if has_backup else "STATUS.md should name a backup story"))
    elif archetype in ("production", "frontend"):
        # deployable: a full CI tier beyond the fast gate
        full = os.path.exists(os.path.join(path, "ci/full.sh"))
        out.append(("deployable: ci/full.sh", full,
                    "" if full else "no ci/full.sh (detached post-commit suite)"))
    return out


def doctor_summary(name):
    """One-line contract-drift summary for session briefings — the self-healing surface:
    drift shows up where the operator/model already looks, every session start.
    Returns None when the contract is met (or on any internal error — briefings must
    never break). Skips the grep-based secret check (too slow for every start)."""
    try:
        reg = Registry()
        path = reg.get_path(name)
        if not os.path.isdir(path):
            return None
        fails = []
        cl = os.path.join(path, "CLAUDE.md")
        n = sum(1 for _ in open(cl, errors="ignore")) if os.path.exists(cl) else 0
        if not 0 < n <= 150:
            fails.append("CLAUDE.md" if n == 0 else f"CLAUDE.md({n}L)")
        if not any(os.path.exists(os.path.join(path, s)) for s in
                   ("STATUS.md", "HANDOFF.md", "docs/HANDOFF.md", "STATE.md", "RESUME.md")):
            fails.append("state-file")
        if not (os.path.exists(os.path.join(path, "ci/fast.sh")) or os.path.exists(os.path.join(path, ".localci"))):
            fails.append("ci")
        if os.path.exists(os.path.join(path, ".git")):
            _, remotes, _ = _sh(["git", "remote"], path, timeout=10)
            if not remotes:
                fails.append("remote")
        if fails:
            return f"contract drift: {', '.join(fails)} — run: clanker doctor {name}"
    except Exception:
        return None
    return None


def _register(name, path, archetype):
    from registry import write_entry
    entry = {"archetype": archetype}
    if os.path.realpath(path) != os.path.realpath(os.path.expanduser(f"~/projects/{name}")):
        entry["path"] = path
    return write_entry(name, entry, create_only=True)


def adopt(target, archetype=None, new=False):
    # 1. resolve: URL -> clone into ~/projects/<name>; path -> use in place;
    #    --new -> create ~/projects/<name> (or the given path) + git init first.
    if "://" in target or target.startswith("git@"):
        name = os.path.basename(target).removesuffix(".git")
        path = os.path.expanduser(f"~/projects/{name}")
        if os.path.exists(path):
            print(f"adopt: {path} already exists — adopting in place (no clone)")
        else:
            print(f"adopt: cloning {target} -> {path} (localci templateDir hooks arrive on clone)")
            rc, _, err = _sh(["git", "clone", target, path], timeout=600)
            if rc != 0:
                print(f"adopt: clone FAILED: {err[:300]}", file=sys.stderr)
                return 1
    else:
        # a bare name (no slash) means ~/projects/<name>
        raw = target if ("/" in target or target.startswith("~")) else f"~/projects/{target}"
        path = os.path.abspath(os.path.expanduser(raw))
        name = os.path.basename(path.rstrip("/"))
        if not os.path.isdir(path):
            if not new:
                print(f"adopt: no such directory: {path}\n"
                      f"       (starting from scratch? add --new to create + git init it)",
                      file=sys.stderr)
                return 1
            os.makedirs(path)
            rc, _, err = _sh(["git", "init", "-b", "main", path])
            if rc != 0:
                print(f"adopt: git init FAILED: {err[:200]}", file=sys.stderr)
                return 1
            print(f"adopt: created {path} + git init (branch main)")
        elif new and not os.path.exists(os.path.join(path, ".git")):
            rc, _, err = _sh(["git", "init", "-b", "main", path])
            print(f"adopt: git init'd existing dir {path}" if rc == 0
                  else f"adopt: git init FAILED: {err[:200]}")

    # 2. archetype
    if not archetype:
        try:
            from onboard import detect_archetype
            archetype = detect_archetype(path)[0] or "tool"
        except Exception:
            archetype = "tool"

    # 3. register
    if _register(name, path, archetype):
        print(f"adopt: registered '{name}' (archetype={archetype})")
    else:
        print(f"adopt: '{name}' already registered")

    # 4. scaffold the contract (only what's missing; never overwrite)
    made = []
    for rel, content, mode in (("CLAUDE.md", CLAUDE_STUB, None),
                               ("STATUS.md", STATUS_STUB, None),
                               ("ci/fast.sh", CI_STUB, 0o755)):
        p = os.path.join(path, rel)
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(content.format(name=name))
            if mode:
                os.chmod(p, mode)
            made.append(rel)
    os.makedirs(os.path.join(path, ".claude", "skills"), exist_ok=True)
    if made:
        print(f"adopt: scaffolded {', '.join(made)} (uncommitted — review + commit)")

    # 5. doctor
    rc = doctor_project(name)
    print(f"next: review the stubs, commit, add a private remote if missing, then: clanker work {name}")
    return rc
