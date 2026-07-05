"""Project discovery + cwd→project resolution — the single source of truth.

Both the dashboard (display-time re-attribution) and the session hook use
`resolve_project()` so a session is named the same way no matter who derives it.
`scan_projects()` enumerates repos for the registry/count.

The resolver is git-aware: any path inside a repo resolves to the MAIN repo's dir
name, so a deep subdirectory, a linked worktree, and a symlink to the repo all map
to one project instead of fragmenting. `scan_projects()` dedupes by the real repo
path, so a convenience symlink (e.g. ~/polymarket_research -> ~/projects/polymarket)
is never listed as a phantom second project. Non-git paths fall back to matching
the configured roots, else `global`. Dead worktree fragments (`foo-f48`, dirs that
no longer exist) are folded to their parent in `_normalize`.

Roots come from `CLANKER_PROJECT_ROOTS` (colon-separated; default `~/projects`).
A root that is itself a repo is one project; otherwise its immediate repo
children are projects.

A final NORMALIZE step canonicalizes a resolved name so historical fragments map
to the live project — purely at the resolution layer, never mutating stored data:
  1. explicit `aliases:` from the registry (e.g. Eigenstate-V2 -> eigenstate),
  2. exact match / case-insensitive match to a live repo,
  3. a dead worktree fragment (`<repo>-<suffix>`, where the worktree dir is gone
     so git can't collapse it) folds to the live repo it prefixes.
"""
import functools
import os
import subprocess


def project_roots():
    raw = os.environ.get("CLANKER_PROJECT_ROOTS", "~/projects")
    roots = []
    for r in raw.split(":"):
        r = r.strip()
        if r:
            roots.append(os.path.abspath(os.path.expanduser(r)))
    return roots


def _main_repo_path(cwd):
    """Realpath of the MAIN repo directory for a path inside a git repo —
    collapsing linked worktrees and following symlinks — or None if not in a repo.
    `--git-common-dir` points at the main repo's `.git` for any worktree."""
    try:
        p = subprocess.run(["git", "-C", cwd, "rev-parse", "--git-common-dir"],
                           capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    common = p.stdout.strip()
    if p.returncode != 0 or not common:
        return None
    # Relative (".git") for the main worktree, absolute for a linked worktree.
    common = os.path.realpath(os.path.join(cwd, common))
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    return None


def _git_repo_name(cwd):
    """The main repo's dir name for a path inside a git repo, or None."""
    m = _main_repo_path(cwd)
    return os.path.basename(m) if m else None


def _raw_project(cwd):
    cwd = os.path.abspath(os.path.expanduser(cwd))
    name = _git_repo_name(cwd)
    if name:
        return name
    # Non-git fallback: a project folder not yet git-init'd, directly under a
    # container root. A root that is $HOME itself is NOT a container (its real
    # projects are git repos, already resolved above) — otherwise every home
    # subdir (~/.claude, ~/documents) would masquerade as a project. The container
    # dir itself, and anything else, is 'global'.
    home = os.path.abspath(os.path.expanduser("~"))
    for root in project_roots():
        if root == home or cwd == root:
            continue
        if cwd.startswith(root + os.sep):
            seg = cwd[len(root):].lstrip(os.sep).split(os.sep)[0]
            if seg:
                return seg
    return "global"


@functools.lru_cache(maxsize=1)
def _live_repos():
    return frozenset(scan_projects().keys())


@functools.lru_cache(maxsize=1)
def _aliases():
    """Explicit name→canonical map from the registry's `aliases:` section. Lets a
    legacy/variant name (e.g. Eigenstate-V2) fold to a live project."""
    path = os.environ.get("CLANKER_REGISTRY", os.path.expanduser("~/projects/.clanker.yaml"))
    try:
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        al = data.get("aliases", {})
        return {str(k): str(v) for k, v in al.items()} if isinstance(al, dict) else {}
    except Exception:
        return {}


def _normalize(name):
    """Canonicalize a resolved name to the live project (resolution layer only)."""
    if not name or name == "global":
        return name
    aliases = _aliases()
    seen = 0
    while name in aliases and seen < 5:   # follow a short alias chain, no loops
        name = aliases[name]
        seen += 1
    repos = _live_repos()
    if name in repos:
        return name
    low = name.lower()
    for r in repos:                       # case-insensitive match to a live repo
        if r.lower() == low:
            return r
    # Dead worktree fragment: `<repo>-<suffix>` whose worktree dir no longer
    # exists (so git couldn't collapse it). Fold to the LONGEST live-repo prefix.
    cand = [r for r in repos if name.startswith(r + "-")]
    if cand:
        return max(cand, key=len)
    return name


@functools.lru_cache(maxsize=8192)
def resolve_project(cwd):
    """Canonical project name for a working directory. Memoized by cwd (there are
    typically ~100 distinct cwds across all sessions, so the git calls are cheap
    and happen at most once each per process)."""
    if not cwd:
        return "global"
    return _normalize(_raw_project(cwd))


def reset_caches():
    """Drop memoized repo/alias/resolution state (tests + after registry edits)."""
    _live_repos.cache_clear()
    _aliases.cache_clear()
    resolve_project.cache_clear()


def scan_projects():
    """{name: real_path} for every distinct git repo reachable from the roots. A
    root that is itself a repo is added directly; otherwise its immediate repo
    children are. Deduped by the MAIN repo's real path, and named after it, so a
    convenience symlink (~/polymarket_research -> ~/projects/polymarket) collapses
    into the canonical project rather than appearing as a phantom second one."""
    found = {}
    seen = set()

    def add(path):
        main = _main_repo_path(path) or os.path.realpath(path)
        if main in seen:
            return
        seen.add(main)
        found.setdefault(os.path.basename(main), main)

    for root in project_roots():
        if not os.path.isdir(root):
            continue
        if os.path.isdir(os.path.join(root, ".git")):
            add(root)
            continue
        try:
            children = sorted(os.listdir(root))
        except OSError:
            continue
        for child in children:
            p = os.path.join(root, child)
            if os.path.isdir(os.path.join(p, ".git")):
                add(p)
    return found
