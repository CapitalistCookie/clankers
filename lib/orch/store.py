"""SQLite store for orchestrated sessions + backlog + events.

Concurrency-safe (WAL) so the WebUI, the supervise loop, and the CLI can all
read/write. One DB at CLANKER_DATA/orch/orch.db (override with `path=` for tests).

Session state machine (enforced by set_state):
    pending → running → {waiting ↔ idle ↔ running} → done | failed | stopped
    any active → stale (heartbeat timeout); stale → running | stopped | failed
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")
ORCH_DIR = os.path.join(DATA_DIR, "orch")
DB_PATH = os.path.join(ORCH_DIR, "orch.db")

STATES = ("pending", "running", "waiting", "idle", "stale", "done", "failed", "stopped")
ACTIVE_STATES = ("pending", "running", "waiting", "idle", "stale")
TERMINAL_STATES = ("done", "failed", "stopped")

# Allowed transitions (set_state refuses others). Terminal states are sinks.
_TRANSITIONS = {
    "pending": {"running", "failed", "stopped"},
    "running": {"waiting", "idle", "done", "failed", "stopped", "stale"},
    "waiting": {"running", "idle", "done", "failed", "stopped", "stale"},
    "idle": {"running", "waiting", "done", "failed", "stopped", "stale"},
    "stale": {"running", "stopped", "failed", "done"},
    "done": set(), "failed": set(), "stopped": set(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orch_sessions (
  id TEXT PRIMARY KEY, task TEXT, project TEXT, agent_type TEXT DEFAULT 'claude',
  working_dir TEXT, worktree TEXT, branch TEXT, tmux_target TEXT,
  headless INTEGER DEFAULT 0, pid INTEGER, state TEXT DEFAULT 'pending',
  cost_usd REAL DEFAULT 0, tokens INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT, heartbeat_at TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS orch_backlog (
  id TEXT PRIMARY KEY, task TEXT, project TEXT, priority INTEGER DEFAULT 1,
  status TEXT DEFAULT 'queued', assigned_to TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS orch_events (
  ts TEXT, session_id TEXT, kind TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_state ON orch_sessions(state);
CREATE INDEX IF NOT EXISTS idx_backlog_status ON orch_backlog(status);
"""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _conn(path=None):
    target = path or DB_PATH
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    c = sqlite3.connect(target, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db(path=None):
    c = _conn(path)
    try:
        c.executescript(_SCHEMA)
        c.commit()
    finally:
        c.close()


def _row(r):
    if r is None:
        return None
    d = dict(r)
    d["headless"] = bool(d.get("headless"))
    if d.get("meta"):
        try:
            d["meta"] = json.loads(d["meta"])
        except (TypeError, json.JSONDecodeError):
            d["meta"] = {}
    else:
        d["meta"] = {}
    return d


# ── sessions ────────────────────────────────────────────────────────────────
def insert_session(rec, path=None):
    init_db(path)
    now = _now()
    r = dict(rec)
    r.setdefault("state", "pending")
    r.setdefault("created_at", now)
    r["updated_at"] = now
    r.setdefault("heartbeat_at", now)
    meta = r.get("meta")
    cols = ("id", "task", "project", "agent_type", "working_dir", "worktree", "branch",
            "tmux_target", "headless", "pid", "state", "cost_usd", "tokens",
            "created_at", "updated_at", "heartbeat_at", "meta")
    vals = [r.get("id"), r.get("task"), r.get("project"), r.get("agent_type", "claude"),
            r.get("working_dir"), r.get("worktree"), r.get("branch"), r.get("tmux_target"),
            1 if r.get("headless") else 0, r.get("pid"), r.get("state"),
            r.get("cost_usd", 0), r.get("tokens", 0), r.get("created_at"),
            r.get("updated_at"), r.get("heartbeat_at"),
            json.dumps(meta) if isinstance(meta, (dict, list)) else meta]
    c = _conn(path)
    try:
        c.execute(f"INSERT INTO orch_sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        c.commit()
    finally:
        c.close()
    log_event(r.get("id"), "spawned", r.get("task", "")[:200], path=path)
    return r.get("id")


def get_session(session_id, path=None):
    init_db(path)
    c = _conn(path)
    try:
        return _row(c.execute("SELECT * FROM orch_sessions WHERE id=?", (session_id,)).fetchone())
    finally:
        c.close()


def list_sessions(state=None, active_only=False, path=None):
    init_db(path)
    c = _conn(path)
    try:
        if state:
            rows = c.execute("SELECT * FROM orch_sessions WHERE state=? ORDER BY created_at DESC", (state,)).fetchall()
        elif active_only:
            q = "SELECT * FROM orch_sessions WHERE state IN (%s) ORDER BY created_at DESC" % ",".join("?" * len(ACTIVE_STATES))
            rows = c.execute(q, ACTIVE_STATES).fetchall()
        else:
            rows = c.execute("SELECT * FROM orch_sessions ORDER BY created_at DESC").fetchall()
        return [_row(r) for r in rows]
    finally:
        c.close()


def update_session(session_id, path=None, **fields):
    if not fields:
        return
    init_db(path)
    fields["updated_at"] = _now()
    if "meta" in fields and isinstance(fields["meta"], (dict, list)):
        fields["meta"] = json.dumps(fields["meta"])
    if "headless" in fields:
        fields["headless"] = 1 if fields["headless"] else 0
    sets = ", ".join(f"{k}=?" for k in fields)
    c = _conn(path)
    try:
        c.execute(f"UPDATE orch_sessions SET {sets} WHERE id=?", list(fields.values()) + [session_id])
        c.commit()
    finally:
        c.close()


def set_state(session_id, state, path=None, force=False):
    """Guarded state transition. Returns True if applied, False if illegal."""
    if state not in STATES:
        return False
    cur = get_session(session_id, path=path)
    if cur is None:
        return False
    if not force and state != cur["state"] and state not in _TRANSITIONS.get(cur["state"], set()):
        return False
    update_session(session_id, path=path, state=state)
    log_event(session_id, "state", f"{cur['state']}→{state}", path=path)
    return True


def touch_heartbeat(session_id, path=None):
    update_session(session_id, path=path, heartbeat_at=_now())


def delete_session(session_id, path=None):
    init_db(path)
    c = _conn(path)
    try:
        c.execute("DELETE FROM orch_sessions WHERE id=?", (session_id,))
        c.commit()
    finally:
        c.close()


# ── events ──────────────────────────────────────────────────────────────────
def log_event(session_id, kind, detail="", path=None):
    init_db(path)
    c = _conn(path)
    try:
        c.execute("INSERT INTO orch_events (ts, session_id, kind, detail) VALUES (?,?,?,?)",
                  (_now(), session_id, kind, detail))
        c.commit()
    finally:
        c.close()


def recent_events(limit=50, path=None):
    init_db(path)
    c = _conn(path)
    try:
        rows = c.execute("SELECT * FROM orch_events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


# ── backlog ─────────────────────────────────────────────────────────────────
def enqueue(task, project=None, priority=1, path=None, id=None):
    import uuid
    init_db(path)
    bid = id or uuid.uuid4().hex[:12]
    c = _conn(path)
    try:
        c.execute("INSERT INTO orch_backlog (id, task, project, priority, status, created_at) VALUES (?,?,?,?, 'queued', ?)",
                  (bid, task, project, priority, _now()))
        c.commit()
    finally:
        c.close()
    return bid


def list_backlog(status="queued", path=None):
    init_db(path)
    c = _conn(path)
    try:
        if status:
            rows = c.execute("SELECT * FROM orch_backlog WHERE status=? ORDER BY priority DESC, created_at ASC", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM orch_backlog ORDER BY priority DESC, created_at ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def update_backlog(backlog_id, path=None, **fields):
    if not fields:
        return
    init_db(path)
    sets = ", ".join(f"{k}=?" for k in fields)
    c = _conn(path)
    try:
        c.execute(f"UPDATE orch_backlog SET {sets} WHERE id=?", list(fields.values()) + [backlog_id])
        c.commit()
    finally:
        c.close()
