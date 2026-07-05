"""Optional orchestration subsystem — clanker spawns & steers its own coding sessions.

OFF by default and SAFE by default (see control.py: the master switch enables only
read-only supervision; auto_nudge / auto_spawn / auto_merge are separate sub-toggles,
all off). The supervise loop runs as a background task inside the dashboard server
that no-ops unless control.enabled(). Everything is flippable from the WebUI/CLI.

Modules:
  control — runtime config (WebUI/CLI-writable toggles)
  store   — SQLite store of orchestrated sessions + backlog + events
  spawn   — create worktree + tmux + launch a session (MVP-1)
  nudge   — risk-gated auto-continue of routine waiting sessions (MVP-2)
  daemon  — the supervise loop: parked/budget/overlap/nudge/merge passes (MVP-3)
  router  — task→spawn/reuse/queue assignment policy (MVP-4)
"""
from .control import enabled, get_config, set_config  # noqa: F401

__all__ = ["enabled", "get_config", "set_config",
           "control", "store", "spawn", "nudge", "daemon", "router"]
