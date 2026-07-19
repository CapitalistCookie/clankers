"""Per-project Claude memory namespaces (harness overhaul round 7, 2026-07-05).

Claude Code derives the auto-memory directory from the session's LAUNCH cwd:
~/.claude/projects/<slug(cwd)>/memory/MEMORY.md is injected into the system
prompt. Repo-rooted sessions (the `clanker work` law) therefore see an EMPTY
memory unless their namespace is seeded — the global router with every
cross-project arc lives only under $HOME's own namespace (GLOBAL_MEMORY).

Fix: every project namespace gets a MEMORY.md stub that (a) points hard at the
global router, (b) carries the project's own must-read arc lines, (c) reminds
the model of the frontmatter law. Stubs are created only when missing; existing
per-project memories just get the router pointer appended if absent.

Weak-model constraint: stub lines stay <250 chars and use plain-text absolute
paths (not markdown links) so memory-lint's dead-pointer check never trips.
"""
import os
import re

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
# The global namespace slug is derived from $HOME via Claude Code's own slug
# rule (slashes/dots become dashes) — never hardcode the slugified home path.
HOME_SLUG = re.sub(r"[/.]", "-", os.path.expanduser("~"))
GLOBAL_MEMORY = os.path.join(CLAUDE_PROJECTS, HOME_SLUG, "memory")
ROUTER_LINE = (
    "> GLOBAL ROUTER — cross-project arcs, feedback law, active-project table: "
    f"READ `{os.path.join(GLOBAL_MEMORY, 'MEMORY.md')}` when work spans projects "
    "or mentions another system. Full inventory: INDEX_ALL.md in the same dir."
)

# Project-specific must-read arc lines (absolute plain-text paths into the global
# memory dir), keyed by registered project NAME. The CONTENT lives OUTSIDE the
# repo in ~/projects/.clanker-arcs.yaml (operator-specific project references
# don't belong in a publishable tool; 2026-07-05). _DEFAULT_ARCS below is only
# the in-repo fallback/example.
ARCS_PATH = os.path.expanduser("~/projects/.clanker-arcs.yaml")


def _load_arcs():
    try:
        import yaml
        data = yaml.safe_load(open(ARCS_PATH)) or {}
        if isinstance(data, dict):
            return {k: [str(x) for x in v] for k, v in data.items() if isinstance(v, list)}
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return dict(_DEFAULT_ARCS)


_DEFAULT_ARCS = {
    # example only — real per-project arc lines live in ~/projects/.clanker-arcs.yaml
    # "myproject": ["> BEFORE work on X READ (in the global memory dir): x-arc.md"],
}
ARCS = _load_arcs()


def slug(path):
    """Claude Code project-dir slug: '/' and '.' become '-'."""
    return re.sub(r"[/.]", "-", os.path.abspath(path))


def ns_dir(path):
    return os.path.join(CLAUDE_PROJECTS, slug(path))


def memory_root(path):
    """The dir Claude Code keys AUTO MEMORY to: the git repo root when `path`
    is inside a repo, else the path itself. (Transcript STORAGE uses the raw
    cwd slug; memory does NOT — verified against /en/memory 2026-07-05:
    'The <project> path is derived from the git repository, so all worktrees
    and subdirectories within the same repo share one auto memory directory.')"""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return os.path.abspath(path)


def canonical_name(project_path, fallback=None):
    """Registry-first name for a path (several tmux sessions can share one repo;
    ARCS must key off the PROJECT, not whichever session seeded first)."""
    project_path = os.path.abspath(os.path.expanduser(project_path))
    try:
        import yaml
        reg_path = os.environ.get("CLANKER_REGISTRY",
                                  os.path.expanduser("~/projects/.clanker.yaml"))
        data = yaml.safe_load(open(reg_path)) or {}
        for name, v in (data.get("projects") or {}).items():
            if not isinstance(v, dict):
                continue
            p = v.get("path") or os.path.expanduser(f"~/projects/{name}")
            if os.path.realpath(p) == os.path.realpath(project_path):
                return name
    except Exception:
        pass
    return fallback or os.path.basename(project_path.rstrip("/"))


def ensure_memory_stub(project_path, project_name=None):
    """Seed <ns>/memory/MEMORY.md for a project path. Idempotent: creates the
    stub when missing, appends the router pointer and any missing ARCS lines
    when present. Returns 'created' | 'updated' | 'ok' | 'skipped:<why>'."""
    try:
        project_path = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(project_path):
            return f"skipped:no-such-dir {project_path}"
        if project_path == os.path.expanduser("~"):
            return "ok"  # the global namespace IS the router
        name = canonical_name(project_path, fallback=project_name)
        arcs = ARCS.get(name, [])
        # Auto memory loads from the GIT ROOT's namespace, not the cwd's.
        # Sub-projects of one repo (several sub-project dirs of one repo) share it; each
        # caller appends its own missing arc lines, so the shared stub converges
        # to the union.
        root = memory_root(project_path)
        if os.path.realpath(root) == os.path.realpath(os.path.expanduser("~")):
            return "ok"
        mem_dir = os.path.join(ns_dir(root), "memory")
        os.makedirs(mem_dir, exist_ok=True)
        mm = os.path.join(mem_dir, "MEMORY.md")
        if os.path.exists(mm):
            txt = open(mm, errors="ignore").read()
            missing = []
            if os.path.join(GLOBAL_MEMORY, "MEMORY.md") not in txt:
                missing.append(ROUTER_LINE)
            missing += [a for a in arcs if a not in txt]
            if not missing:
                return "ok"
            with open(mm, "a") as f:
                f.write("\n" + "\n".join(missing) + "\n")
            return "updated"
        lines = [
            f"# Memory — {name} (namespace seeded by clanker, 2026-07-05)",
            "",
            ROUTER_LINE,
            "> Repo state: STATUS.md in the repo (injected at session start). Repo rules: CLAUDE.md. Do NOT duplicate them here.",
            "> New memories: one fact per file with frontmatter (name/description/metadata.type) + one pointer line here — memory-lint enforces both.",
        ]
        lines += arcs
        with open(mm, "w") as f:
            f.write("\n".join(lines) + "\n")
        return "created"
    except Exception as e:  # a seeding failure must never block a launch
        return f"skipped:{e.__class__.__name__}"


def backfill(paths_by_name):
    """Seed stubs for {name: path}. Returns list of (name, result)."""
    out = []
    for name, path in sorted(paths_by_name.items()):
        out.append((name, ensure_memory_stub(path, name)))
    return out
