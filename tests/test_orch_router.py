"""Hermetic tests for lib/orch/router.py — the task→session assignment policy.

Each test uses a private temp SQLite store (path= override) so nothing touches the
real CLANKER_DATA store or the real control.json. Config is passed as plain
{"max_parallel": N} dicts straight into assign/dispatch_backlog.

Run: python3 tests/test_orch_router.py            (dual-runnable; non-zero exit on fail)
 or: python3 -m pytest tests/test_orch_router.py -v
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from orch import router, store  # noqa: E402


def _tmpdb():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="orch_router_test_")
    os.close(fd)
    os.unlink(path)  # let store create it fresh (WAL)
    store.init_db(path)
    return path


def _cleanup(path):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _ins(db, sid, state, project=None, task="", working_dir=None):
    store.insert_session(
        {"id": sid, "task": task, "project": project,
         "working_dir": working_dir, "state": state},
        path=db,
    )


# ── (a) no sessions, under cap → spawn ───────────────────────────────────────
def test_no_sessions_under_cap_spawns():
    db = _tmpdb()
    try:
        out = router.assign("build the thing", project="alpha",
                            config={"max_parallel": 4}, store_path=db)
        assert out["action"] == "spawn", out
        assert out["target"] is None, out
        assert isinstance(out["reason"], str) and out["reason"]
    finally:
        _cleanup(db)


# ── (b) idle session same project → reuse that id ────────────────────────────
def test_idle_same_project_is_reused():
    db = _tmpdb()
    try:
        _ins(db, "s-idle", "idle", project="alpha", task="earlier alpha work")
        out = router.assign("more alpha work", project="alpha",
                            config={"max_parallel": 4}, store_path=db)
        assert out["action"] == "reuse", out
        assert out["target"] == "s-idle", out
    finally:
        _cleanup(db)


def test_idle_any_project_reused_when_no_project_requested():
    # With no project on the task, any idle session is a valid reuse target.
    db = _tmpdb()
    try:
        _ins(db, "s-idle", "idle", project="beta", task="beta stuff")
        out = router.assign("generic task", project=None,
                            config={"max_parallel": 4}, store_path=db)
        assert out["action"] == "reuse", out
        assert out["target"] == "s-idle", out
    finally:
        _cleanup(db)


def test_idle_other_project_not_reused_for_project_task_under_cap():
    # An idle session for a *different* project must not steal project-affine work
    # while we still have capacity to spawn a dedicated session.
    db = _tmpdb()
    try:
        _ins(db, "s-beta", "idle", project="beta", task="beta stuff")
        out = router.assign("alpha task", project="alpha",
                            config={"max_parallel": 4}, store_path=db)
        assert out["action"] == "spawn", out
        assert out["target"] is None, out
    finally:
        _cleanup(db)


def test_running_session_not_reused():
    # Only idle sessions are reuse targets; a running same-project session is not.
    db = _tmpdb()
    try:
        _ins(db, "s-run", "running", project="alpha", task="busy")
        out = router.assign("alpha task", project="alpha",
                            config={"max_parallel": 4}, store_path=db)
        assert out["action"] == "spawn", out
    finally:
        _cleanup(db)


# ── (c) active count == max_parallel → queue ─────────────────────────────────
def test_at_capacity_queues():
    db = _tmpdb()
    try:
        n = 3
        for i in range(n):
            _ins(db, f"s-run-{i}", "running", project="alpha", task=f"job {i}")
        out = router.assign("another alpha task", project="alpha",
                            config={"max_parallel": n}, store_path=db)
        assert out["action"] == "queue", out
        assert out["target"] is None, out
        assert "capacity" in out["reason"].lower(), out
    finally:
        _cleanup(db)


def test_at_capacity_but_idle_present_still_reuses():
    # Reuse beats capacity: an idle same-project session is used even when the
    # active count is at max_parallel (reusing it costs no new slot).
    db = _tmpdb()
    try:
        _ins(db, "s-run-0", "running", project="alpha", task="busy0")
        _ins(db, "s-run-1", "running", project="alpha", task="busy1")
        _ins(db, "s-idle", "idle", project="alpha", task="free")
        out = router.assign("alpha task", project="alpha",
                            config={"max_parallel": 3}, store_path=db)
        assert out["action"] == "reuse", out
        assert out["target"] == "s-idle", out
    finally:
        _cleanup(db)


# ── (d) context_affinity ranks same-project idle above other-project idle ─────
def test_context_affinity_ranks_same_project_higher():
    same = {"id": "a", "project": "alpha", "task": "x", "working_dir": None}
    other = {"id": "b", "project": "beta", "task": "x", "working_dir": None}
    s_same = router.context_affinity(same, "do alpha", project="alpha")
    s_other = router.context_affinity(other, "do alpha", project="alpha")
    assert s_same > s_other, (s_same, s_other)
    assert s_same >= 1.0, s_same


def test_context_affinity_token_overlap_is_a_tiebreaker():
    # Two other-project sessions: the one whose task text shares tokens scores higher,
    # but neither outranks a same-project session (overlap is capped below 1.0).
    overlap = {"id": "o", "project": "beta",
               "task": "refactor parser module", "working_dir": None}
    none = {"id": "n", "project": "beta", "task": "unrelated", "working_dir": None}
    s_overlap = router.context_affinity(overlap, "refactor parser bug", project="alpha")
    s_none = router.context_affinity(none, "refactor parser bug", project="alpha")
    assert s_overlap > s_none, (s_overlap, s_none)
    assert s_overlap < 1.0, s_overlap  # never beats a real same-project match


def test_best_idle_picks_same_project_among_candidates():
    # End-to-end through assign: with both an other-project and a same-project idle,
    # the same-project one is chosen.
    db = _tmpdb()
    try:
        _ins(db, "s-beta", "idle", project="beta", task="beta work")
        _ins(db, "s-alpha", "idle", project="alpha", task="alpha work")
        out = router.assign("alpha task", project="alpha",
                            config={"max_parallel": 4}, store_path=db)
        assert out["action"] == "reuse", out
        assert out["target"] == "s-alpha", out
    finally:
        _cleanup(db)


# ── (e) dispatch_backlog spawns up to cap then stops, marking dispatched ──────
def test_dispatch_backlog_spawns_up_to_cap_then_stops():
    db = _tmpdb()
    try:
        # 5 queued tasks, cap 2, no live sessions → expect 2 spawns then stop.
        bids = []
        for i in range(5):
            bids.append(router.enqueue_task(f"task {i}", project="alpha",
                                            priority=1, store_path=db))

        spawned = []

        def fake_spawn(task, project):
            sid = f"spawned-{len(spawned)}"
            spawned.append((sid, task, project))
            return sid

        results = router.dispatch_backlog(
            config={"max_parallel": 2}, store_path=db, spawn_fn=fake_spawn)

        assert len(results) == 2, results
        assert all(r["action"] == "spawn" for r in results), results
        assert len(spawned) == 2, spawned

        # The two acted-on backlog items are marked dispatched with assigned_to;
        # the remaining three are still queued.
        dispatched = store.list_backlog(status="dispatched", path=db)
        queued = store.list_backlog(status="queued", path=db)
        assert len(dispatched) == 2, dispatched
        assert len(queued) == 3, queued
        for d in dispatched:
            assert d["assigned_to"] is not None and d["assigned_to"].startswith("spawned-")
    finally:
        _cleanup(db)


def test_dispatch_backlog_honors_priority_order():
    db = _tmpdb()
    try:
        router.enqueue_task("low", project="alpha", priority=1, store_path=db)
        hi = router.enqueue_task("high", project="alpha", priority=9, store_path=db)

        seen = []

        def fake_spawn(task, project):
            seen.append(task)
            return f"s-{len(seen)}"

        results = router.dispatch_backlog(
            config={"max_parallel": 1}, store_path=db, spawn_fn=fake_spawn)

        # cap 1 → only the highest-priority item is dispatched.
        assert len(results) == 1, results
        assert seen == ["high"], seen
        assert results[0]["backlog_id"] == hi, results
    finally:
        _cleanup(db)


def test_dispatch_backlog_reuses_idle_before_spawning():
    db = _tmpdb()
    try:
        _ins(db, "s-idle", "idle", project="alpha", task="ready")
        router.enqueue_task("first alpha", project="alpha", priority=5, store_path=db)
        router.enqueue_task("second alpha", project="alpha", priority=1, store_path=db)

        spawned = []

        def fake_spawn(task, project):
            sid = f"spawned-{len(spawned)}"
            spawned.append(sid)
            return sid

        results = router.dispatch_backlog(
            config={"max_parallel": 2}, store_path=db, spawn_fn=fake_spawn)

        # First item reuses the idle session; second spawns (1 slot still free).
        assert results[0]["action"] == "reuse", results
        assert results[0]["target"] == "s-idle", results
        assert results[1]["action"] == "spawn", results
        assert len(spawned) == 1, spawned
    finally:
        _cleanup(db)


def test_dispatch_backlog_stops_when_already_at_capacity():
    db = _tmpdb()
    try:
        # Already at cap with running sessions, no idles → nothing dispatched.
        _ins(db, "s-run-0", "running", project="alpha", task="busy0")
        _ins(db, "s-run-1", "running", project="alpha", task="busy1")
        router.enqueue_task("waiting task", project="alpha", priority=1, store_path=db)

        def fake_spawn(task, project):
            raise AssertionError("should not spawn at capacity")

        results = router.dispatch_backlog(
            config={"max_parallel": 2}, store_path=db, spawn_fn=fake_spawn)

        assert results == [], results
        assert len(store.list_backlog(status="queued", path=db)) == 1
    finally:
        _cleanup(db)


def test_dispatch_backlog_no_spawn_fn_stops_at_spawn():
    db = _tmpdb()
    try:
        router.enqueue_task("needs spawn", project="alpha", priority=1, store_path=db)
        results = router.dispatch_backlog(config={"max_parallel": 4}, store_path=db,
                                          spawn_fn=None)
        assert results == [], results
        assert len(store.list_backlog(status="queued", path=db)) == 1
    finally:
        _cleanup(db)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
