"""Runtime config for the optional orchestration subsystem — WebUI- and CLI-writable.

OFF by default. The persisted control file (CLANKER_DATA/orch/control.json) is the
source of truth once written; before that it falls back to env CLANKER_ORCH or the
registry `defaults.orchestration`. The WebUI/CLI flip these at runtime (no restart).

Safety model: enabling `enabled` alone only turns on READ-ONLY supervision (detect
parked / budget / overlap / surface attention). Every behaviour that ACTS on a
session is its own sub-toggle, all default False:
  auto_nudge  — auto-continue routine waiting sessions (risk-gated)
  auto_spawn  — auto-dispatch the backlog into new sessions
  auto_merge  — auto-merge finished worktrees
"""
import json
import os
import tempfile

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")
ORCH_DIR = os.path.join(DATA_DIR, "orch")
CONTROL_PATH = os.path.join(ORCH_DIR, "control.json")

# Defaults + the allowed keys (set_config ignores anything not here).
DEFAULTS = {
    "enabled": False,          # master switch (read-only supervision when on)
    "auto_nudge": False,       # auto-continue routine waiting sessions
    "auto_spawn": False,       # auto-dispatch the backlog
    "auto_merge": False,       # auto-merge finished worktrees
    "worktrees": True,         # isolate spawned sessions in a git worktree
    "skip_permissions": True,  # launch claude with --dangerously-skip-permissions (autonomy)
    "auto_trust": True,        # auto-accept Claude's "trust this folder?" dialog on spawn
    "max_parallel": 4,         # cap on concurrent orchestrated sessions
    "nudge_idle_secs": 45,     # how long a session must sit waiting before a nudge
    "nudge_risk_max": "review",  # never auto-nudge if the pending action scores >= this
    "nudge_max": 3,            # lifetime cap on auto-nudges per session (0 = uncapped)
    "done_idle_secs": 0,       # interactive: stable, question-free pane this long → done (0 = off)
    "budget_usd": None,        # global cost cap across orchestrated sessions (None = off)
}

_BOOL_KEYS = {"enabled", "auto_nudge", "auto_spawn", "auto_merge", "worktrees", "skip_permissions", "auto_trust"}


def _registry_default():
    try:
        import yaml
        reg = os.environ.get("CLANKER_REGISTRY", os.path.expanduser("~/projects/.clanker.yaml"))
        with open(reg) as f:
            return bool((yaml.safe_load(f) or {}).get("defaults", {}).get("orchestration"))
    except Exception:
        return False


def _load_raw():
    try:
        with open(CONTROL_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def get_config():
    """Effective config: persisted control file merged onto DEFAULTS. If no file has
    been written yet, the master switch defaults from env/registry (still off unless set)."""
    cfg = dict(DEFAULTS)
    raw = _load_raw()
    if raw is None:
        env = os.environ.get("CLANKER_ORCH", "").strip().lower()
        cfg["enabled"] = env in ("1", "true", "on", "yes", "all") or _registry_default()
    else:
        for k, v in raw.items():
            if k in DEFAULTS:
                cfg[k] = v
    return cfg


def set_config(**updates):
    """Persist config changes (atomic). Coerces bool keys; ignores unknown keys.
    Returns the new effective config."""
    cfg = get_config()
    for k, v in updates.items():
        if k not in DEFAULTS:
            continue
        if k in _BOOL_KEYS:
            v = _coerce_bool(v)
        cfg[k] = v
    os.makedirs(ORCH_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=ORCH_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONTROL_PATH)
    return cfg


def _coerce_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "on", "yes")


def enabled():
    """Master switch — True only if the operator opted in. Default False."""
    return bool(get_config()["enabled"])
