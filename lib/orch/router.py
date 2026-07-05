"""Task → session assignment policy (MVP-4).

Given a new task, decide whether to spawn a fresh session, reuse an idle one, or
queue it because we are at capacity. Mirrors (in spirit) ECC's
`preview_assignment_for_task`: prefer reusing an idle delegate by context affinity,
else spawn if under the parallelism cap, else defer/queue.

Pure policy + thin store glue — no spawning, no tmux, no git here. The caller
(daemon / WebUI) provides a `spawn_fn` to actually create sessions when draining
the backlog; this module only decides.

Public API:
  assign(task, project=None, config=None, store_path=None) -> dict
      {"action": "reuse"|"spawn"|"queue", "target": <session id|None>, "reason": str}
  context_affinity(session, task, project) -> float
  dispatch_backlog(config=None, store_path=None, spawn_fn=None, assign_fn=None) -> list[dict]
  enqueue_task(task, project=None, priority=1, store_path=None) -> bid
"""
import re

from . import control, store

# Only sessions sitting in "idle" are reuse targets — they are alive but between
# tasks. Active sessions (the capacity denominator) are anything not terminal.
_IDLE_STATE = "idle"
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _max_parallel(config):
    if config is not None and "max_parallel" in config:
        return int(config["max_parallel"])
    return int(control.get_config()["max_parallel"])


def _tokens(text):
    """Lowercased word tokens of length >= 3 (drops noise like 'a', 'to', 'in')."""
    if not text:
        return set()
    return {t for t in _TOKEN_RE.findall(str(text).lower()) if len(t) >= 3}


def context_affinity(session, task, project):
    """Score how well an existing session matches an incoming task.

    Higher = better reuse candidate. Components:
      +1.0  same project (the dominant signal — context affinity)
      +0..  token overlap between the task and the session's own task/working_dir
            (a small tie-breaker; each shared token is worth 0.1, capped at +0.5)

    A session with no project is treated as project-neutral: it does not earn the
    +1.0 same-project bonus but is still eligible (token overlap only) so a generic
    idle worker can pick up unrelated work rather than forcing a spawn.
    """
    if session is None:
        return 0.0
    score = 0.0
    sess_project = session.get("project")
    if project is not None and sess_project is not None and sess_project == project:
        score += 1.0
    task_tokens = _tokens(task)
    if task_tokens:
        sess_tokens = _tokens(session.get("task")) | _tokens(session.get("working_dir"))
        overlap = len(task_tokens & sess_tokens)
        if overlap:
            score += min(0.5, 0.1 * overlap)
    return score


def _best_idle(sessions, task, project, claimed=None):
    """Pick the highest-affinity idle session, or None.

    Same-project idles always rank above other-project idles (the +1.0 affinity
    bonus dominates the <=0.5 token tie-breaker). Among equal-affinity candidates
    the order is whatever the store returned (created_at DESC), which is stable.

    `claimed` is a set of session ids already handed out earlier in the same
    dispatch drain (the store snapshot lags those reuse decisions) — they are
    excluded so one idle session is never double-booked within a batch.
    """
    claimed = claimed or set()
    idle = [s for s in sessions
            if s.get("state") == _IDLE_STATE and s.get("id") not in claimed]
    if not idle:
        return None, 0.0
    scored = [(context_affinity(s, task, project), s) for s in idle]
    best_score, best = max(scored, key=lambda pair: pair[0])
    return best, best_score


def assign(task, project=None, config=None, store_path=None, claimed=None):
    """Decide where a single task goes. Read-only — never mutates the store.

    Policy (first match wins):
      1. REUSE — an idle session exists matching `project` (context affinity; if no
         project was given, any idle qualifies). Targets the best-affinity idle.
      2. SPAWN — fewer active sessions than max_parallel → make a new one.
      3. QUEUE — at capacity → defer.

    `claimed` (optional) lets a multi-task drain exclude idle sessions and active
    slots it has already committed this batch but not yet flushed to the store.
    """
    sessions = store.list_sessions(active_only=True, path=store_path)
    cap = _max_parallel(config)

    best, best_score = _best_idle(sessions, task, project, claimed=claimed)
    # An idle is only a *reuse* match when no project was requested (any idle) or
    # it earned the same-project affinity bonus. Otherwise an other-project idle
    # should not steal project-affine work — fall through to spawn/queue.
    if best is not None and (project is None or best_score >= 1.0):
        if project is None:
            reason = "reusing idle session (no project affinity required)"
        else:
            reason = f"reusing idle session with matching project '{project}'"
        return {"action": "reuse", "target": best["id"], "reason": reason}

    active = len(sessions)
    if active < cap:
        return {
            "action": "spawn",
            "target": None,
            "reason": f"under capacity ({active}/{cap} active) — spawning new session",
        }

    return {
        "action": "queue",
        "target": None,
        "reason": f"at capacity ({active}/{cap} active)",
    }


def enqueue_task(task, project=None, priority=1, store_path=None):
    """Thin wrapper over store.enqueue — push a task onto the backlog."""
    return store.enqueue(task, project=project, priority=priority, path=store_path)


def dispatch_backlog(config=None, store_path=None, spawn_fn=None, assign_fn=None):
    """Drain the queued backlog through `assign`, in priority order.

    For each queued item:
      - "spawn": if `spawn_fn` is given, call spawn_fn(task, project); mark the
        backlog item dispatched (assigned_to = the returned session id, if any).
        Without a spawn_fn we cannot actually create a session, so we stop.
      - "reuse": mark dispatched (assigned_to = the reused session id).
      - "queue": at capacity — stop draining; remaining items stay queued.

    Capacity is re-checked as we go: each spawn consumes one slot, so a single
    drain never exceeds max_parallel even though `assign` reads a stale snapshot.
    Returns a list of {"backlog_id", "action", "target"} for the items acted on.
    """
    _assign = assign_fn or assign
    cap = _max_parallel(config)
    results = []

    # Count slots already consumed by live sessions; budget the rest of the drain
    # against this running tally rather than re-querying (spawn_fn may be a fake
    # that does not insert into the store).
    active = len(store.list_sessions(active_only=True, path=store_path))

    # Idle sessions we have already handed out this batch. The store snapshot that
    # `assign` reads lags these reuse decisions, so without this an idle session
    # would be double-booked across iterations.
    claimed = set()

    for item in store.list_backlog(status="queued", path=store_path):
        bid = item["id"]
        task = item.get("task")
        project = item.get("project")

        # Re-evaluate against the live snapshot (minus what we've already claimed),
        # then veto a spawn if our running tally has already hit the cap (the store
        # snapshot can lag a fake spawn).
        decision = _assign(task, project=project, config={"max_parallel": cap},
                           store_path=store_path, claimed=claimed)
        action = decision["action"]
        if action == "spawn" and active >= cap:
            action = "queue"

        if action == "queue":
            break

        if action == "spawn":
            if spawn_fn is None:
                break  # cannot materialise a session without a spawner
            target = spawn_fn(task, project)
            active += 1
            store.update_backlog(bid, path=store_path,
                                 status="dispatched", assigned_to=target)
            results.append({"backlog_id": bid, "action": "spawn", "target": target})
        else:  # reuse
            target = decision["target"]
            claimed.add(target)  # don't reuse the same idle for a later item
            store.update_backlog(bid, path=store_path,
                                 status="dispatched", assigned_to=target)
            results.append({"backlog_id": bid, "action": "reuse", "target": target})

    return results
