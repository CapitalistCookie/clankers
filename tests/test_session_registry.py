"""Claude Code session registry -> session state (the ntfy state source).

2026-09-07: Claude Code 2.1.236 stopped animating the pane-title spinner under
tmux, so the title heuristic classified every session as at-rest forever and
the notifier went silent for a week. State now comes from Claude's own
per-process record (~/.claude/sessions/<pid>.json: status busy/shell/idle/
waiting + the tmux pane ref). Hermetic: a temp registry dir, a fake /proc,
no tmux, no live store, no network."""

import json
import os
import sys
import tempfile

import pytest

# Same import-time isolation dance as test_serve_state.py (serve pulls in
# webauth, which captures CLANKER_DATA at import time).
_OLD_DATA = os.environ.get("CLANKER_DATA")
os.environ["CLANKER_DATA"] = tempfile.mkdtemp(prefix="clk-registry-test-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import serve  # noqa: E402
if _OLD_DATA is None:
    os.environ.pop("CLANKER_DATA", None)
else:
    os.environ["CLANKER_DATA"] = _OLD_DATA

LIVE = {4242: "111", 4343: "222", 9999: "333"}   # pid -> /proc start ticks


@pytest.fixture
def registry_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setenv("CLANKER_CLAUDE_SESSIONS_DIR", str(d))
    monkeypatch.setattr(serve, "_proc_start", lambda pid: LIVE.get(pid))
    monkeypatch.setattr(serve, "_proc_children", lambda pid: [])
    return d


def _write(d, pid, **fields):
    rec = {"pid": pid, "procStart": LIVE.get(pid, "0"), "sessionId": f"s-{pid}",
           "cwd": "/x", "status": "idle", "tmux": f"sess{pid}:@1.%{pid}"}
    rec.update(fields)
    (d / f"{pid}.json").write_text(json.dumps(rec))
    return rec


def _pane(pid, command="claude", title="✳ Claude Code"):
    return {"session": f"sess{pid}", "command": command, "pane_id": f"%{pid}",
            "pane_pid": pid + 1, "title": title, "target": f"sess{pid}:0.0"}


@pytest.mark.parametrize("status,state", [
    ("busy", "working"), ("shell", "working"), ("idle", "waiting"), ("waiting", "waiting"),
])
def test_every_registry_status_maps_to_a_state(registry_dir, status, state):
    _write(registry_dir, 4242, status=status)
    reg = serve.read_session_registry()
    assert [r["pane_id"] for r in reg] == ["%4242"]
    assert serve.detect_session_state(_pane(4242), reg) == state


def test_title_is_no_longer_consulted(registry_dir):
    """Regression safety for the 2026-09 blindness: a braille-spinner title with
    an idle record is at rest; a static-star title with a busy record works."""
    _write(registry_dir, 4242, status="idle")
    _write(registry_dir, 4343, status="busy")
    reg = serve.read_session_registry()
    assert serve.detect_session_state(_pane(4242, title="⠐ spinning"), reg) == "waiting"
    assert serve.detect_session_state(_pane(4343, title="✳ static"), reg) == "working"


def test_dead_and_reused_pids_are_dropped(registry_dir):
    _write(registry_dir, 4242, status="busy", procStart="not-the-live-ticks")  # pid reused
    _write(registry_dir, 5555, status="busy")                                   # not running
    assert serve.read_session_registry() == []
    assert serve.detect_session_state(_pane(4242), []) == "waiting"


def test_non_claude_pane_is_idle_and_unrecorded_claude_pane_is_at_rest(registry_dir):
    reg = serve.read_session_registry()
    assert serve.detect_session_state(_pane(1, command="bash"), reg) == "idle"
    assert serve.detect_session_state(_pane(1), reg) == "waiting"


def test_malformed_records_and_missing_dir_never_raise(registry_dir, monkeypatch):
    (registry_dir / "1.json").write_text("{not json")
    (registry_dir / "2.json").write_text(json.dumps(["not", "a", "dict"]))
    (registry_dir / "3.json").write_text(json.dumps({"pid": "4242"}))   # pid not int
    (registry_dir / "notes.txt").write_text("ignored")
    _write(registry_dir, 4242, status="busy", tmux="garbage-ref")
    reg = serve.read_session_registry()
    assert len(reg) == 1 and reg[0]["pane_id"] is None
    monkeypatch.setenv("CLANKER_CLAUDE_SESSIONS_DIR", str(registry_dir / "nope"))
    assert serve.read_session_registry() == []
    assert serve.detect_session_state(_pane(4242)) == "waiting"   # registry=None path


def test_pid_fallback_when_record_has_no_tmux_ref(registry_dir, monkeypatch):
    _write(registry_dir, 4242, status="busy", tmux=None)
    monkeypatch.setattr(serve, "_proc_children", lambda pid: [4242] if pid == 100 else [])
    pane = {"session": "s", "command": "claude", "pane_id": "%77", "pane_pid": 100}
    assert serve.detect_session_state(pane, serve.read_session_registry()) == "working"
    # the REPL exec'd in place: the pane pid IS the REPL pid
    pane = {"session": "s", "command": "claude", "pane_id": "%77", "pane_pid": 4242}
    assert serve.detect_session_state(pane, serve.read_session_registry()) == "working"


def test_sessions_dir_is_resolved_at_call_time(monkeypatch, tmp_path):
    monkeypatch.delenv("CLANKER_CLAUDE_SESSIONS_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert serve._sessions_dir() == str(tmp_path / "sessions")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert serve._sessions_dir() == os.path.expanduser("~/.claude/sessions")


def test_waiting_for_upgrades_a_plain_tail_to_a_decision_ping():
    tail = "⏺ Done.\n\n❯ \n"
    title, prio, tags, body = serve._craft_notification("s", tail, waiting_for="input needed")
    assert title == "s: decision needed" and prio == "max" and "input needed" in body
    # without Claude's reason the same tail is a plain finished ping
    assert serve._craft_notification("s", tail)[0] == "s finished — your turn"
    # a dialog visible in the tail keeps its own (more specific) detail
    dialog = "│ Do you want to proceed?\n│ ❯ 1. Yes\n│   2. No\n"
    assert "proceed" in serve._craft_notification("s", dialog, waiting_for="permission")[3]


def test_registry_loss_alerts_once_after_grace_and_recovers(registry_dir):
    panes = [_pane(1), _pane(2), _pane(3)]
    watch = {"lost_since": None, "alerted_at": None}
    sent = []
    alert = lambda **kw: sent.append(kw)  # noqa: E731
    t0 = 1000.0
    assert serve._registry_watch(panes, [], t0, watch, alert=alert) == (0, 3)
    assert watch["lost_since"] == t0 and sent == []                       # grace period
    serve._registry_watch(panes, [], t0 + serve.REGISTRY_LOSS_GRACE_SECS - 1, watch, alert=alert)
    assert sent == []
    serve._registry_watch(panes, [], t0 + serve.REGISTRY_LOSS_GRACE_SECS + 1, watch, alert=alert)
    assert len(sent) == 1 and sent[0]["alert_id"] == "session-state-source-lost"
    assert sent[0]["severity"] == "critical" and "BLIND" in sent[0]["message"]
    serve._registry_watch(panes, [], t0 + serve.REGISTRY_LOSS_GRACE_SECS + 30, watch, alert=alert)
    assert len(sent) == 1                                                 # throttled
    serve._registry_watch(panes, [], t0 + serve.REGISTRY_LOSS_REALERT_SECS + 200, watch, alert=alert)
    assert len(sent) == 2                                                 # re-raised later
    # one live record anywhere in the fleet = coverage back, watchdog resets
    _write(registry_dir, 4242, status="idle")
    reg = serve.read_session_registry()
    assert serve._registry_watch([_pane(4242), _pane(2), _pane(3)], reg, t0 + 99999, watch,
                                 alert=alert) == (1, 3)
    assert watch["lost_since"] is None


def test_registry_loss_needs_a_real_fleet(registry_dir):
    """Two record-less panes (e.g. `claude agents` + a -p run) are not a loss."""
    watch = {"lost_since": None, "alerted_at": None}
    sent = []
    now = 1000.0
    for _ in range(3):
        serve._registry_watch([_pane(1), _pane(2)], [], now, watch, alert=lambda **kw: sent.append(kw))
        now += serve.REGISTRY_LOSS_GRACE_SECS
    assert sent == [] and watch["lost_since"] is None


def test_unknown_status_is_working_never_waiting(registry_dir):
    """A Claude Code vocabulary change must not silence the notifier again."""
    _write(registry_dir, 4242, status="streaming")
    assert serve.detect_session_state(_pane(4242), serve.read_session_registry()) == "working"


def test_vocabulary_drift_alerts_once_per_window(registry_dir):
    _write(registry_dir, 4242, status="streaming")
    _write(registry_dir, 4343, status="busy")
    reg = serve.read_session_registry()
    sent = []
    alert = lambda **kw: sent.append(kw)  # noqa: E731
    watch = {"alerted_at": None}
    assert serve._vocab_watch(reg, 1000.0, watch, alert=alert) == ["streaming"]
    assert len(sent) == 1 and sent[0]["alert_id"] == "session-state-vocabulary-drift"
    assert "streaming" in sent[0]["message"]
    serve._vocab_watch(reg, 1060.0, watch, alert=alert)
    assert len(sent) == 1                                          # throttled
    serve._vocab_watch(reg, 1000.0 + serve.REGISTRY_LOSS_REALERT_SECS + 1, watch, alert=alert)
    assert len(sent) == 2                                          # re-raised later
    assert serve._vocab_watch([{"status": "idle"}], 9000.0, watch, alert=alert) == []


def test_is_trust_dialog():
    assert serve.is_trust_dialog("❯ No, exit\n  Yes, I trust this folder")
    assert serve.is_trust_dialog("Quick safety check: Is this a project you created")
    assert not serve.is_trust_dialog("⏺ Done.\n❯ ")
    assert not serve.is_trust_dialog("")
