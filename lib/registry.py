"""Project registry — reads ~/projects/.clanker.yaml.

write_entry/remove_entry below are THE single registry mutators (2026-07-19):
the codebase previously had three writers with two serializers and divergent
schemas (adopt vs onboard init vs onboard remove) — every mutation now goes
through one atomic, one-schema path.
"""

import os
import shutil
import subprocess
import yaml

DEFAULT_REGISTRY = os.path.expanduser("~/projects/.clanker.yaml")

# Resolve git once at import so each subprocess call skips a PATH lookup (S8).
GIT = shutil.which("git") or "/usr/bin/git"


def _registry_path():
    return os.environ.get("CLANKER_REGISTRY", DEFAULT_REGISTRY)


def _load_raw(path):
    if os.path.exists(path):
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _atomic_write(path, data):
    # tmp + os.replace: a crash mid-write can never truncate the registry.
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True,
                       default_flow_style=False)
    os.replace(tmp, path)


def write_entry(name, entry, create_only=False):
    """Merge `entry` (dict) into projects[name]. create_only=True refuses to
    touch an existing entry (returns False — adopt's idempotence contract).
    Returns True when the file was written. Only non-None values are stored
    (no more `remote: None` rows)."""
    path = _registry_path()
    data = _load_raw(path)
    projects = data.setdefault("projects", {})
    if create_only and name in projects:
        return False
    clean = {k: v for k, v in (entry or {}).items() if v is not None}
    projects[name] = {**(projects.get(name) or {}), **clean}
    _atomic_write(path, data)
    return True


def remove_entry(name):
    """Delete projects[name]. Returns True if it existed."""
    path = _registry_path()
    data = _load_raw(path)
    projects = data.get("projects", {}) or {}
    if name not in projects:
        return False
    del projects[name]
    data["projects"] = projects
    _atomic_write(path, data)
    return True


class Registry:
    def __init__(self, path=None):
        self.path = path or os.environ.get("CLANKER_REGISTRY", DEFAULT_REGISTRY)
        self.data = {}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.data = yaml.safe_load(f) or {}
        self._merged = None

    @property
    def projects(self):
        """Registry-file projects unioned with auto-discovered repos under the
        project roots. Registry entries are authoritative (they keep their declared
        archetype/metadata); a discovered-only repo is added with a detected
        archetype, its real path, and auto=True so new repos appear without anyone
        editing the registry by hand."""
        if self._merged is not None:
            return self._merged
        merged = dict(self.data.get("projects", {}) or {})
        try:
            from projects import scan_projects
            discovered = scan_projects()
        except Exception:
            discovered = {}
        for name, path in discovered.items():
            if name in merged:
                if isinstance(merged[name], dict):
                    merged[name].setdefault("path", path)
                continue
            arch = "unknown"
            try:
                from onboard import detect_archetype
                arch = detect_archetype(path)[0]
            except Exception:
                pass
            merged[name] = {"archetype": arch, "path": path, "auto": True}
        self._merged = merged
        return merged

    @property
    def archetypes(self):
        return self.data.get("archetypes", {})

    @property
    def defaults(self):
        return self.data.get("defaults", {})

    def get_archetype(self, name):
        p = self.projects.get(name, {})
        return p.get("archetype", "unknown")

    def get_hooks(self, name):
        arch = self.get_archetype(name)
        arch_def = self.archetypes.get(arch, {})
        return arch_def.get("hooks", [])

    def get_path(self, name):
        p = self.projects.get(name, {})
        if isinstance(p, dict) and p.get("path"):
            return p["path"]
        return os.path.expanduser(f"~/projects/{name}")

    def list_projects(self):
        print(f"{'Project':<20} {'Archetype':<12} {'Branch':<10} {'Last Commit'}")
        print("-" * 72)
        for name in sorted(self.projects.keys()):
            arch = self.get_archetype(name)
            path = self.get_path(name)
            branch = ""
            commit = ""
            if os.path.isdir(os.path.join(path, ".git")):
                try:
                    branch = subprocess.check_output(
                        [GIT, "-C", path, "branch", "--show-current"],
                        stderr=subprocess.DEVNULL, text=True
                    ).strip()
                    commit = subprocess.check_output(
                        [GIT, "-C", path, "log", "--oneline", "-1"],
                        stderr=subprocess.DEVNULL, text=True
                    ).strip()[:50]
                except:
                    pass
            elif not os.path.isdir(path):
                commit = "(not found)"
            else:
                commit = "(no git)"
            print(f"{name:<20} {arch:<12} {branch:<10} {commit}")
