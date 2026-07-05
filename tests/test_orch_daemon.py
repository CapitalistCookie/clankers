"""Hermetic tests for the orchestration supervise loop (lib/orch/daemon.py).

Everything runs against a fresh temp SQLite store and fully-injected callbacks —
no real tmux, no real processes, no real `nudge` module, no production data.
`pid_alive` is monkeypatched per test so liveness is deterministic; `config` is
passed as a plain dict so the real control.json is never read or written.

Covers:
  (a) a session with a dead pid -> reaped to `failed` (+ pid cleared, event logged)
  (b) a waiting session + auto_nudge=True + routine pane -> send_keys gets nudge text
  (c) auto_nudge=False -> send_keys never called
  (d) enabled=False -> supervise_once is a pure no-op
  (e) reconcile drives state from get_pane_state (+ heartbeat bumped)
  plus: pid_alive edge cases and reap_dead leaving pid-less / live sessions alone.

Run: python3 tests/test_orch_daemon.py   (or: python3 -m pytest tests/test_orch_daemon.py -v)
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from orch import daemon, store  # noqa: E402


# ── fixtures / helpers ───────────────────────────────────────────────────────
import atexit  # noqa: E402

_TMP_DBS = []


def _tmp_db():
    """A unique temp store db path (created lazily by store.init_db). Tracked for
    end-of-run cleanup (incl. the -wal/-shm sidecars WAL mode creates)."""
    p = os.path.join(tempfile.gettempdir(), f"clanker-daemon-test-{uuid.uuid4().hex}.db")
    _TMP_DBS.append(p)
    return p


@atexit.register
def _cleanup_tmp_dbs():
    for p in _TMP_DBS:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(p + suffix)
            except OSError:
                pass


def _insert(path, sid, state="running", pid=4242, tmux_target=None,
            cost_usd=0.0, meta=None, headless=False):
    """Insert a session directly into a known state (bypassing the pending->...
    transition chain via force) so each test starts from the state it needs."""
    store.insert_session({
        "id": sid,
        "task": f"task for {sid}",
        "project": "workspace",
        "tmux_target": tmux_target or f"orch-{sid}:0.0",
        "pid": pid,
        "cost_usd": cost_usd,
        "meta": meta or {},
        "headless": headless,
    }, path=path)
    # insert_session always lands in 'pending'; force to the desired state.
    store.set_state(sid, state, path=path, force=True)


class _Recorder:
    """Records send_keys(target, text) calls for assertions."""
    def __init__(self):
        self.calls = []

    def __call__(self, target, text):
        self.calls.append((target, text))


def _cfg(**over):
    """A minimal effective-config dict (no control.json involved)."""
    base = {
        "enabled": True,
        "auto_nudge": False,
        "budget_usd": None,
        "nudge_idle_secs": 45,
        "nudge_risk_max": "review",
    }
    base.update(over)
    return base


def _const_pane(text):
    """A capture_pane callback returning the same text for any target."""
    return lambda target: text


def _const_state(state):
    """A get_pane_state callback returning a fixed state."""
    return lambda target, pane_text: state


# ── pid_alive ────────────────────────────────────────────────────────────────
def test_pid_alive_self_is_alive():
    assert daemon.pid_alive(os.getpid()) is True


def test_pid_alive_none_and_nonpositive_are_dead():
    assert daemon.pid_alive(None) is False
    assert daemon.pid_alive(0) is False
    assert daemon.pid_alive(-5) is False


def test_pid_alive_nonint_is_dead():
    assert daemon.pid_alive("not-a-pid") is False


def test_pid_alive_dead_pid_is_dead(monkeypatch):
    def _raise(_pid, _sig):
        raise ProcessLookupError()
    monkeypatch.setattr(daemon.os, "kill", _raise)
    assert daemon.pid_alive(999999) is False


def test_pid_alive_eperm_counts_as_alive(monkeypatch):
    def _raise(_pid, _sig):
        raise PermissionError()
    monkeypatch.setattr(daemon.os, "kill", _raise)
    assert daemon.pid_alive(12345) is True


# ── (a) reap dead sessions ───────────────────────────────────────────────────
def test_reap_dead_marks_dead_pid_failed(monkeypatch):
    path = _tmp_db()
    _insert(path, "deadbeef", state="running", pid=4242)
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: False)

    reaped = daemon.reap_dead(store_path=path)

    assert [r["id"] for r in reaped] == ["deadbeef"]
    sess = store.get_session("deadbeef", path=path)
    assert sess["state"] == "failed"
    assert sess["pid"] is None
    # a 'reaped' event was logged
    kinds = [e["kind"] for e in store.recent_events(path=path)]
    assert "reaped" in kinds


def test_reap_dead_keeps_live_sessions(monkeypatch):
    path = _tmp_db()
    _insert(path, "alive123", state="running", pid=7777)
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)

    reaped = daemon.reap_dead(store_path=path)

    assert reaped == []
    sess = store.get_session("alive123", path=path)
    assert sess["state"] == "running"
    assert sess["pid"] == 7777


def test_reap_dead_ignores_pidless_sessions(monkeypatch):
    path = _tmp_db()
    _insert(path, "nopid", state="pending", pid=None)
    # Even though pid_alive would say "dead", a None pid is left alone.
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: False)

    reaped = daemon.reap_dead(store_path=path)

    assert reaped == []
    assert store.get_session("nopid", path=path)["state"] == "pending"


def test_supervise_once_reaps_via_pass(monkeypatch):
    """(a) end-to-end through supervise_once: dead pid -> failed + reaped:N action."""
    path = _tmp_db()
    _insert(path, "zombie", state="running", pid=4242, tmux_target="orch-z:0.0")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: False)

    out = daemon.supervise_once(
        config=_cfg(),
        store_path=path,
        capture_pane=_const_pane("idle pane"),
        get_pane_state=_const_state("running"),
    )

    assert out["enabled"] is True
    assert out["reaped"] == 1
    assert "reaped:zombie" in out["actions"]
    assert store.get_session("zombie", path=path)["state"] == "failed"


# ── (b) auto-nudge a waiting session ─────────────────────────────────────────
def test_auto_nudge_sends_keys_for_waiting_session(monkeypatch):
    path = _tmp_db()
    _insert(path, "wait1", state="waiting", pid=7777, tmux_target="orch-w:0.0")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    rec = _Recorder()

    out = daemon.supervise_once(
        config=_cfg(auto_nudge=True),
        store_path=path,
        capture_pane=_const_pane("❯ awaiting input"),
        # keep it waiting through reconcile so it is nudge-eligible
        get_pane_state=_const_state("waiting"),
        send_keys=rec,
        should_nudge=lambda session, pane_text, config: (True, "routine"),
        nudge_text=lambda: "continue",
    )

    assert out["nudged"] == 1
    assert "nudged:wait1" in out["actions"]
    assert rec.calls == [("orch-w:0.0", "continue")]
    kinds = [e["kind"] for e in store.recent_events(path=path)]
    assert "nudged" in kinds


def test_auto_nudge_respects_should_nudge_false(monkeypatch):
    """auto_nudge on, but the gate says no -> no send_keys."""
    path = _tmp_db()
    _insert(path, "wait2", state="waiting", pid=7777, tmux_target="orch-w2:0.0")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    rec = _Recorder()

    out = daemon.supervise_once(
        config=_cfg(auto_nudge=True),
        store_path=path,
        capture_pane=_const_pane("❯ risky action pending"),
        get_pane_state=_const_state("waiting"),
        send_keys=rec,
        should_nudge=lambda session, pane_text, config: (False, "too risky"),
        nudge_text=lambda: "continue",
    )

    assert out["nudged"] == 0
    assert rec.calls == []


def test_auto_nudge_real_gate_fires_after_pane_idle(monkeypatch):
    """End-to-end through the REAL nudge.should_nudge (no injected gate): the
    daemon must supply waiting_secs from the pane-stability ledger — reconcile
    bumps heartbeat_at every pass, so deriving idleness from heartbeats means
    the nudge_idle_secs threshold could never be reached (the bug that made
    auto_nudge structurally dead via the daemon)."""
    path = _tmp_db()
    _insert(path, "realn", state="waiting", pid=7777, tmux_target="orch-rn")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    rec = _Recorder()
    pane = "Do you want to continue? (y/n)"
    cfgn = _cfg(auto_nudge=True, nudge_idle_secs=15)

    # pass 1: ledger initialized (pane just observed) -> waiting 0s -> no nudge
    out = daemon.supervise_once(config=cfgn, store_path=path, now=_T0,
                                capture_pane=_const_pane(pane),
                                get_pane_state=_const_state("waiting"),
                                send_keys=rec)
    assert out["nudged"] == 0
    assert rec.calls == []

    # pass 2: 30s later, pane unchanged -> waited 30s >= 15s -> the real gate
    # classifies a routine (y/n) wait and nudges with the real message
    out = daemon.supervise_once(config=cfgn, store_path=path,
                                now=_T0 + timedelta(seconds=30),
                                capture_pane=_const_pane(pane),
                                get_pane_state=_const_state("waiting"),
                                send_keys=rec)
    assert out["nudged"] == 1, out
    assert rec.calls == [("orch-rn", "Use your best judgment and continue.")]
    kinds = [e["kind"] for e in store.recent_events(path=path)]
    assert "nudged" in kinds


def test_auto_nudge_increments_lifetime_count(monkeypatch):
    path = _tmp_db()
    _insert(path, "cnt1", state="waiting", pid=7777, tmux_target="orch-c1")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    rec = _Recorder()

    daemon.supervise_once(
        config=_cfg(auto_nudge=True, nudge_max=3),
        store_path=path,
        capture_pane=_const_pane("❯ awaiting"),
        get_pane_state=_const_state("waiting"),
        send_keys=rec,
        should_nudge=lambda session, pane_text, config: (True, "routine"),
        nudge_text=lambda: "continue",
    )

    assert len(rec.calls) == 1
    assert store.get_session("cnt1", path=path)["meta"]["nudge_count"] == 1


def test_auto_nudge_cap_parks_session(monkeypatch):
    """At nudge_max lifetime nudges the session is never nudged again — it is
    parked for the operator (one nudge_capped event, not one per pass). This
    bounds the nudge → reply → nudge loop a finished interactive agent causes."""
    path = _tmp_db()
    _insert(path, "cap1", state="waiting", pid=7777, tmux_target="orch-cp",
            meta={"nudge_count": 3})
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    rec = _Recorder()
    cfgc = _cfg(auto_nudge=True, nudge_max=3)
    kw = dict(
        config=cfgc, store_path=path,
        capture_pane=_const_pane("❯ awaiting"),
        get_pane_state=_const_state("waiting"),
        send_keys=rec,
        should_nudge=lambda session, pane_text, config: (True, "routine"),
        nudge_text=lambda: "continue",
    )

    out = daemon.supervise_once(**kw)
    assert out["nudged"] == 0
    assert rec.calls == []
    assert "nudge_capped:cap1" in out["actions"]
    assert store.get_session("cap1", path=path)["meta"]["nudge_capped"] is True

    # a second pass stays quiet — no repeat nudge_capped event spam
    daemon.supervise_once(**kw)
    capped_events = [e for e in store.recent_events(path=path)
                     if e["kind"] == "nudge_capped"]
    assert len(capped_events) == 1


def test_auto_nudge_cap_zero_is_uncapped(monkeypatch):
    path = _tmp_db()
    _insert(path, "cap0", state="waiting", pid=7777, tmux_target="orch-c0",
            meta={"nudge_count": 99})
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    rec = _Recorder()

    out = daemon.supervise_once(
        config=_cfg(auto_nudge=True, nudge_max=0),
        store_path=path,
        capture_pane=_const_pane("❯ awaiting"),
        get_pane_state=_const_state("waiting"),
        send_keys=rec,
        should_nudge=lambda session, pane_text, config: (True, "routine"),
        nudge_text=lambda: "continue",
    )

    assert out["nudged"] == 1
    assert store.get_session("cap0", path=path)["meta"]["nudge_count"] == 100


# ── (c) auto_nudge disabled -> never nudges ──────────────────────────────────
def test_auto_nudge_off_never_sends_keys(monkeypatch):
    path = _tmp_db()
    _insert(path, "wait3", state="waiting", pid=7777, tmux_target="orch-w3:0.0")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    rec = _Recorder()
    nudged_gate = {"called": False}

    def _gate(session, pane_text, config):
        nudged_gate["called"] = True
        return (True, "routine")

    out = daemon.supervise_once(
        config=_cfg(auto_nudge=False),
        store_path=path,
        capture_pane=_const_pane("❯ awaiting input"),
        get_pane_state=_const_state("waiting"),
        send_keys=rec,
        should_nudge=_gate,
        nudge_text=lambda: "continue",
    )

    assert out["nudged"] == 0
    assert rec.calls == []
    # the gate is never even consulted when auto_nudge is off
    assert nudged_gate["called"] is False


# ── (d) disabled -> no-op ────────────────────────────────────────────────────
def test_disabled_is_noop(monkeypatch):
    path = _tmp_db()
    _insert(path, "s1", state="waiting", pid=4242, tmux_target="orch-s:0.0")
    # If anything ran, these would blow up / record — assert they don't.
    rec = _Recorder()
    touched = {"capture": False}

    def _cap(target):
        touched["capture"] = True
        return "❯"

    out = daemon.supervise_once(
        config=_cfg(enabled=False, auto_nudge=True),
        store_path=path,
        capture_pane=_cap,
        get_pane_state=_const_state("waiting"),
        send_keys=rec,
        should_nudge=lambda *a: (True, "x"),
        nudge_text=lambda: "continue",
    )

    assert out == {"enabled": False, "actions": []}
    assert touched["capture"] is False  # never captured a pane
    assert rec.calls == []
    # state untouched, no events beyond the initial 'spawned'/'state' from setup
    assert store.get_session("s1", path=path)["state"] == "waiting"


# ── (e) reconcile updates state from get_pane_state ──────────────────────────
def test_reconcile_updates_state_from_pane(monkeypatch):
    path = _tmp_db()
    _insert(path, "rec1", state="running", pid=7777, tmux_target="orch-r:0.0")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    before = store.get_session("rec1", path=path)["heartbeat_at"]

    out = daemon.supervise_once(
        config=_cfg(),
        store_path=path,
        capture_pane=_const_pane("❯ waiting for you"),
        get_pane_state=_const_state("waiting"),   # running -> waiting
    )

    assert out["reconciled"] == 1
    assert "reconcile:rec1->waiting" in out["actions"]
    sess = store.get_session("rec1", path=path)
    assert sess["state"] == "waiting"
    # heartbeat was bumped (>= prior; equal only if same-second, so >= is the check)
    assert sess["heartbeat_at"] >= before
    # a 'state' transition event was logged by store.set_state
    details = [(e["kind"], e["detail"]) for e in store.recent_events(path=path)]
    assert ("state", "running→waiting") in details


def test_reconcile_default_heuristic_maps_prompt_to_waiting(monkeypatch):
    """Exercise the *default* get_pane_state (not injected) via a prompt pane."""
    path = _tmp_db()
    _insert(path, "rec2", state="running", pid=7777, tmux_target="orch-r2:0.0")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)

    out = daemon.supervise_once(
        config=_cfg(),
        store_path=path,
        capture_pane=_const_pane("some output\n❯ "),
        # get_pane_state omitted -> default heuristic
    )

    assert out["reconciled"] == 1
    assert store.get_session("rec2", path=path)["state"] == "waiting"


def test_reconcile_default_heuristic_busy_word_is_running(monkeypatch):
    path = _tmp_db()
    _insert(path, "rec3", state="waiting", pid=7777, tmux_target="orch-r3:0.0")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)

    out = daemon.supervise_once(
        config=_cfg(),
        store_path=path,
        capture_pane=_const_pane("✳ Thinking… (1.2k tokens)"),
    )

    assert out["reconciled"] == 1
    assert store.get_session("rec3", path=path)["state"] == "running"


# ── done-detection: exit sentinel ────────────────────────────────────────────
def test_sentinel_exit_zero_marks_done(monkeypatch):
    """Pane shows the session's sentinel with rc 0 -> done + exit_rc/final_output
    in meta + an 'exited' event + finished counted."""
    path = _tmp_db()
    _insert(path, "hdone", state="running", pid=7777, tmux_target="orch-hd",
            headless=True, meta={"exit_sentinel": "<<CLANKER_EXIT:hdone000:"})
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)

    out = daemon.supervise_once(
        config=_cfg(),
        store_path=path,
        capture_pane=_const_pane("agent output...\n<<CLANKER_EXIT:hdone000:0>>\n$ "),
    )

    sess = store.get_session("hdone", path=path)
    assert sess["state"] == "done"
    assert sess["meta"]["exit_rc"] == 0
    assert "<<CLANKER_EXIT:hdone000:0>>" in sess["meta"]["final_output"]
    assert out["finished"] == 1
    assert "exited:hdone:done" in out["actions"]
    kinds = [e["kind"] for e in store.recent_events(path=path)]
    assert "exited" in kinds


def test_sentinel_exit_nonzero_marks_failed(monkeypatch):
    path = _tmp_db()
    _insert(path, "hfail", state="running", pid=7777, tmux_target="orch-hf",
            headless=True, meta={"exit_sentinel": "<<CLANKER_EXIT:hfail000:"})
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)

    out = daemon.supervise_once(
        config=_cfg(),
        store_path=path,
        capture_pane=_const_pane("boom\n<<CLANKER_EXIT:hfail000:3>>\n$ "),
    )

    sess = store.get_session("hfail", path=path)
    assert sess["state"] == "failed"
    assert sess["meta"]["exit_rc"] == 3
    assert "exited:hfail:failed" in out["actions"]


def test_sentinel_command_echo_is_not_a_match(monkeypatch):
    """The echoed launch command shows the literal %s — it must never read as an
    exit. The session reconciles normally instead."""
    path = _tmp_db()
    _insert(path, "hrun", state="running", pid=7777, tmux_target="orch-hr",
            headless=True, meta={"exit_sentinel": "<<CLANKER_EXIT:hrun0000:"})
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    echo = "$ claude --print 'do x'; printf '\\n<<CLANKER_EXIT:hrun0000:%s>>\\n' \"$?\""

    out = daemon.supervise_once(
        config=_cfg(),
        store_path=path,
        capture_pane=_const_pane(echo),
        get_pane_state=_const_state("running"),
    )

    assert store.get_session("hrun", path=path)["state"] == "running"
    assert out["finished"] == 0


def test_sentinel_of_another_session_is_ignored(monkeypatch):
    """A different session's sentinel in the pane (sid mismatch) must not match."""
    path = _tmp_db()
    _insert(path, "haaa", state="running", pid=7777, tmux_target="orch-ha",
            headless=True, meta={"exit_sentinel": "<<CLANKER_EXIT:haaa0000:"})
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)

    out = daemon.supervise_once(
        config=_cfg(),
        store_path=path,
        capture_pane=_const_pane("<<CLANKER_EXIT:hbbb0000:0>>"),
        get_pane_state=_const_state("running"),
    )

    assert store.get_session("haaa", path=path)["state"] == "running"
    assert out["finished"] == 0


def test_sentinel_takes_last_match(monkeypatch):
    """A stale rc-1 sentinel above a newer rc-0 one (manual relaunch in the pane):
    the LAST printed exit wins."""
    path = _tmp_db()
    _insert(path, "hlast", state="running", pid=7777, tmux_target="orch-hl",
            headless=True, meta={"exit_sentinel": "<<CLANKER_EXIT:hlast000:"})
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)

    daemon.supervise_once(
        config=_cfg(),
        store_path=path,
        capture_pane=_const_pane(
            "<<CLANKER_EXIT:hlast000:1>>\nretry...\n<<CLANKER_EXIT:hlast000:0>>"),
    )

    sess = store.get_session("hlast", path=path)
    assert sess["state"] == "done"
    assert sess["meta"]["exit_rc"] == 0


# ── done-detection: interactive idle → done (opt-in) ─────────────────────────
_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _rest_pane(body):
    """A pane shaped like a Claude session at rest: result text, the input box,
    then the status bar (no question in view → classify_wait kind 'unknown')."""
    return f"{body}\n│ > \n⏵⏵ bypass permissions on"


def _idle_pass(path, pane, now, cfg=None, classify=None, state="waiting"):
    """One supervise pass with a fixed pane + injected clock for idle-done tests."""
    kwargs = {}
    if classify is not None:
        kwargs["classify_wait"] = classify
    return daemon.supervise_once(
        config=cfg or _cfg(done_idle_secs=120),
        store_path=path,
        now=now,
        capture_pane=_const_pane(pane),
        get_pane_state=_const_state(state),
        **kwargs,
    )


def test_idle_done_after_stable_pane(monkeypatch):
    """A non-headless waiting session whose pane is byte-stable past done_idle_secs
    with nothing pending goes done (uses the REAL nudge.classify_wait)."""
    path = _tmp_db()
    _insert(path, "idone", state="waiting", pid=7777, tmux_target="orch-id")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    pane = _rest_pane("task complete, committed.")

    _idle_pass(path, pane, _T0)  # records pane_sha + pane_changed_at
    assert store.get_session("idone", path=path)["state"] == "waiting"

    _idle_pass(path, pane, _T0 + timedelta(seconds=60))  # stable but under threshold
    assert store.get_session("idone", path=path)["state"] == "waiting"

    out = _idle_pass(path, pane, _T0 + timedelta(seconds=180))  # over threshold
    assert store.get_session("idone", path=path)["state"] == "done"
    assert "idle_done:idone" in out["actions"]
    assert out["finished"] == 1
    kinds = [e["kind"] for e in store.recent_events(path=path)]
    assert "idle_done" in kinds


def test_idle_done_blocked_by_decision_fork(monkeypatch):
    """A pane asking the human to choose must NEVER idle out to done."""
    path = _tmp_db()
    _insert(path, "ifork", state="waiting", pid=7777, tmux_target="orch-if")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    pane = "Which approach do you want?\n1. rewrite\n2. patch\n❯ "

    _idle_pass(path, pane, _T0)
    out = _idle_pass(path, pane, _T0 + timedelta(seconds=999))

    assert store.get_session("ifork", path=path)["state"] == "waiting"
    assert out["finished"] == 0


def test_idle_done_blocked_by_routine_wait(monkeypatch):
    """LIVE-TRIAL regression: a session waiting on a routine y/n question is the
    NUDGER's lane — idling it to done terminated a mid-task session (and GC'd
    its worktree) in the live trial. Only 'unknown' waits may idle out."""
    path = _tmp_db()
    _insert(path, "iyn", state="waiting", pid=7777, tmux_target="orch-iy")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    pane = "● Do you want to continue? (y/n)\n❯ "

    _idle_pass(path, pane, _T0)
    out = _idle_pass(path, pane, _T0 + timedelta(seconds=9999))

    assert store.get_session("iyn", path=path)["state"] == "waiting"
    assert out["finished"] == 0


def test_idle_done_ignores_scrolled_out_question(monkeypatch):
    """Only the pane TAIL is the current context: a y/n question that scrolled
    far above a finished agent's output must not block idle-done forever."""
    path = _tmp_db()
    _insert(path, "iscr", state="waiting", pid=7777, tmux_target="orch-is")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    filler = "\n".join(f"work line {i}" for i in range(20))
    pane = "Do you want to continue? (y/n)\n" + _rest_pane(filler)

    _idle_pass(path, pane, _T0)
    out = _idle_pass(path, pane, _T0 + timedelta(seconds=180))

    assert store.get_session("iscr", path=path)["state"] == "done"
    assert out["finished"] == 1


def test_idle_done_disabled_by_default(monkeypatch):
    """Without done_idle_secs (or = 0) a stable pane never idles to done."""
    path = _tmp_db()
    _insert(path, "ioff", state="waiting", pid=7777, tmux_target="orch-io")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    pane = "quiet\n❯ "

    _idle_pass(path, pane, _T0, cfg=_cfg())  # done_idle_secs absent -> 0 -> off
    out = _idle_pass(path, pane, _T0 + timedelta(hours=6), cfg=_cfg())

    assert store.get_session("ioff", path=path)["state"] == "waiting"
    assert out["finished"] == 0


def test_idle_done_skips_headless(monkeypatch):
    """A quiet --print session looks idle while working — headless must rely on
    the exit sentinel only, never the idle clock."""
    path = _tmp_db()
    _insert(path, "ihead", state="waiting", pid=7777, tmux_target="orch-ih",
            headless=True)
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    pane = "no output yet\n❯ "

    _idle_pass(path, pane, _T0)
    out = _idle_pass(path, pane, _T0 + timedelta(seconds=999))

    assert store.get_session("ihead", path=path)["state"] == "waiting"
    assert out["finished"] == 0


def test_idle_done_pane_change_resets_clock(monkeypatch):
    """Any pane change restarts the stability window."""
    path = _tmp_db()
    _insert(path, "ireset", state="waiting", pid=7777, tmux_target="orch-ir")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)

    _idle_pass(path, _rest_pane("output A"), _T0)
    # changed at +200s (> threshold since T0, but the change resets the clock)
    _idle_pass(path, _rest_pane("output B"), _T0 + timedelta(seconds=200))
    out = _idle_pass(path, _rest_pane("output B"), _T0 + timedelta(seconds=260))  # only 60s stable
    assert store.get_session("ireset", path=path)["state"] == "waiting"
    assert out["finished"] == 0

    out = _idle_pass(path, _rest_pane("output B"), _T0 + timedelta(seconds=340))  # 140s stable
    assert store.get_session("ireset", path=path)["state"] == "done"
    assert out["finished"] == 1


def test_idle_done_without_classifier_fails_safe(monkeypatch):
    """No classify_wait available (nudge module absent) -> never idle-done."""
    path = _tmp_db()
    _insert(path, "inocl", state="waiting", pid=7777, tmux_target="orch-in")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    monkeypatch.setattr(daemon, "_nudge", None)
    pane = "quiet\n❯ "

    _idle_pass(path, pane, _T0)
    out = _idle_pass(path, pane, _T0 + timedelta(seconds=999))

    assert store.get_session("inocl", path=path)["state"] == "waiting"
    assert out["finished"] == 0


# ── budget + overlap flagging ────────────────────────────────────────────────
def test_budget_over_is_flagged_not_killed(monkeypatch):
    path = _tmp_db()
    _insert(path, "spendy", state="running", pid=7777,
            tmux_target="orch-sp:0.0", cost_usd=12.0)
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)

    out = daemon.supervise_once(
        config=_cfg(budget_usd=10.0),
        store_path=path,
        capture_pane=_const_pane("running"),
        get_pane_state=_const_state("running"),
    )

    assert out["budget"] == "over"
    assert "budget_over" in out["actions"]
    # NOT killed — session is still active, only flagged via an event
    assert store.get_session("spendy", path=path)["state"] == "running"
    kinds = [e["kind"] for e in store.recent_events(path=path)]
    assert "budget_over" in kinds


def test_budget_under_is_not_flagged(monkeypatch):
    path = _tmp_db()
    _insert(path, "thrifty", state="running", pid=7777,
            tmux_target="orch-t:0.0", cost_usd=1.0)
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)

    out = daemon.supervise_once(
        config=_cfg(budget_usd=10.0),
        store_path=path,
        capture_pane=_const_pane("running"),
        get_pane_state=_const_state("running"),
    )

    assert out["budget"] == "normal"
    assert "budget_over" not in out["actions"]


def test_overlap_flags_shared_files(monkeypatch):
    path = _tmp_db()
    _insert(path, "ovA", state="running", pid=1, tmux_target="orch-a:0.0",
            meta={"files_touched": ["src/app.py", "src/util.py"]})
    _insert(path, "ovB", state="running", pid=2, tmux_target="orch-b:0.0",
            meta={"files_touched": ["src/app.py"]})
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)

    out = daemon.supervise_once(
        config=_cfg(),
        store_path=path,
        capture_pane=_const_pane("running"),
        get_pane_state=_const_state("running"),
    )

    assert out["overlaps"] == 1
    assert "overlap:src/app.py" in out["actions"]
    overlap_events = [e for e in store.recent_events(path=path) if e["kind"] == "overlap"]
    assert len(overlap_events) == 1
    assert "src/app.py" in overlap_events[0]["detail"]


# ── auto_nudge with the module absent + no injected gate -> safe no-op ────────
def test_auto_nudge_without_gate_is_safe(monkeypatch):
    """If auto_nudge is on but no should_nudge is available (module absent and not
    injected), the pass must not blow up and must not nudge."""
    path = _tmp_db()
    _insert(path, "wait4", state="waiting", pid=7777, tmux_target="orch-w4:0.0")
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    monkeypatch.setattr(daemon, "_nudge", None)  # simulate MVP-2 not built
    rec = _Recorder()

    out = daemon.supervise_once(
        config=_cfg(auto_nudge=True),
        store_path=path,
        capture_pane=_const_pane("❯ waiting"),
        get_pane_state=_const_state("waiting"),
        send_keys=rec,
        # should_nudge / nudge_text intentionally omitted
    )

    assert out["nudged"] == 0
    assert rec.calls == []


class _MonkeyPatch:
    """Minimal pytest-monkeypatch shim for the no-pytest __main__ runner:
    setattr(obj, name, value) records the prior value; undo() restores all."""
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self):
        while self._undo:
            target, name, old = self._undo.pop()
            setattr(target, name, old)


if __name__ == "__main__":
    import inspect

    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for fn in fns:
        # Provide a tiny monkeypatch shim so tests are runnable without pytest.
        kwargs = {}
        if "monkeypatch" in inspect.signature(fn).parameters:
            kwargs["monkeypatch"] = _MonkeyPatch()
        try:
            fn(**kwargs)
            print(f"  PASS {fn.__name__}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
        finally:
            if "monkeypatch" in kwargs:
                kwargs["monkeypatch"].undo()
    print(f"\n{passed}/{passed + failed} passed")
    sys.exit(0 if failed == 0 else 1)
