"""Hermetic tests for orch.nudge — risk-gated auto-continue of waiting sessions.

Pure stdlib, no filesystem / network / DB: operates entirely on string inputs and
plain dicts. The config is always passed in explicitly (we never touch the on-disk
control file), so the tests are deterministic regardless of environment.

Run: python3 -m pytest tests/test_orch_nudge.py -v   (or: python3 tests/test_orch_nudge.py)
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from orch import nudge, control  # noqa: E402,F401
from ecc import risk, guard      # noqa: E402,F401


# Reference "now" used wherever a session needs a wait duration derived from timestamps.
NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cfg(**over):
    """A config dict with sane defaults for nudging, overridable per-test."""
    base = {
        "auto_nudge": True,
        "nudge_idle_secs": 45,
        "nudge_risk_max": "confirm",
    }
    base.update(over)
    return base


# ─── _risk_rank ───────────────────────────────────────────────────────────────
def test_risk_rank_ladder_order():
    assert nudge._risk_rank("allow") == 0
    assert nudge._risk_rank("review") == 1
    assert nudge._risk_rank("confirm") == 2
    assert nudge._risk_rank("block") == 3
    # Strictly increasing along the ladder.
    assert (nudge._risk_rank("allow") < nudge._risk_rank("review")
            < nudge._risk_rank("confirm") < nudge._risk_rank("block"))


def test_risk_rank_unknown_is_most_dangerous():
    # Unknown / missing actions fail safe to block-level so they can't slip a ceiling.
    assert nudge._risk_rank("bogus") == nudge._risk_rank("block")
    assert nudge._risk_rank(None) == nudge._risk_rank("block")
    assert nudge._risk_rank("") == nudge._risk_rank("block")


# ─── nudge_text ──────────────────────────────────────────────────────────────
def test_nudge_text_is_a_proceed_message():
    txt = nudge.nudge_text()
    assert isinstance(txt, str) and txt.strip()
    low = txt.lower()
    assert "judgment" in low and "continue" in low


# ─── classify_wait: routine ──────────────────────────────────────────────────
def test_classify_routine_continue_yn_is_nudgeable():
    v = nudge.classify_wait("Continue? (y/n)")
    assert v["kind"] == "routine"
    assert v["nudgeable"] is True


def test_classify_routine_proceed_is_nudgeable():
    v = nudge.classify_wait("Proceed?")
    assert v["kind"] == "routine"
    assert v["nudgeable"] is True


def test_classify_routine_press_enter_is_nudgeable():
    v = nudge.classify_wait("Press enter to continue")
    assert v["kind"] == "routine"
    assert v["nudgeable"] is True


def test_classify_routine_bare_prompt_is_nudgeable():
    # A plain interactive prompt sitting at the end with nothing pending.
    v = nudge.classify_wait("some output finished\n❯ ")
    assert v["kind"] == "routine"
    assert v["nudgeable"] is True


# ─── classify_wait: destructive_confirm ──────────────────────────────────────
def test_classify_destructive_rm_rf_not_nudgeable():
    pane = "About to run:\n$ rm -rf /data\nProceed? (y/n)"
    v = nudge.classify_wait(pane)
    assert v["kind"] == "destructive_confirm"
    assert v["nudgeable"] is False
    # The worst action must be at least confirm-level.
    assert nudge._risk_rank(v["risk_action"]) >= nudge._risk_rank("confirm")


def test_classify_destructive_beats_routine_prompt():
    # Even with a routine-looking "(y/n)" prompt, a pending destructive command wins.
    pane = "$ git reset --hard origin/main\nContinue? (y/n)"
    v = nudge.classify_wait(pane)
    assert v["kind"] == "destructive_confirm"
    assert v["nudgeable"] is False


def test_classify_destructive_from_last_assistant_text():
    # The dangerous command can come from the assistant's message, not just the pane.
    v = nudge.classify_wait("Proceed? (y/n)", last_assistant_text="I will run `$ rm -rf /data` now.")
    assert v["kind"] == "destructive_confirm"
    assert v["nudgeable"] is False


# ─── classify_wait: decision ─────────────────────────────────────────────────
def test_classify_decision_numbered_options_not_nudgeable():
    pane = "How should I proceed?\n1) refactor the module\n2) rewrite it from scratch"
    v = nudge.classify_wait(pane)
    assert v["kind"] == "decision"
    assert v["nudgeable"] is False


def test_classify_decision_dotted_options_not_nudgeable():
    pane = "Options:\n1. keep current design\n2. migrate to the new API"
    v = nudge.classify_wait(pane)
    assert v["kind"] == "decision"
    assert v["nudgeable"] is False


def test_classify_decision_phrase_not_nudgeable():
    v = nudge.classify_wait("Do you want me to deploy this to production now?")
    assert v["kind"] == "decision"
    assert v["nudgeable"] is False


def test_classify_decision_should_x_or_y_not_nudgeable():
    v = nudge.classify_wait("Should I bump the minor version or the patch version?")
    assert v["kind"] == "decision"
    assert v["nudgeable"] is False


# ─── classify_wait: error ────────────────────────────────────────────────────
def test_classify_error_traceback_not_nudgeable():
    pane = (
        "Traceback (most recent call last):\n"
        '  File "x.py", line 3, in <module>\n'
        "ValueError: boom\n"
    )
    v = nudge.classify_wait(pane)
    assert v["kind"] == "error"
    assert v["nudgeable"] is False


def test_classify_error_failed_line_not_nudgeable():
    v = nudge.classify_wait("running suite...\n3 failed, 0 passed")
    assert v["kind"] == "error"
    assert v["nudgeable"] is False


# ─── classify_wait: unknown (fail safe) ──────────────────────────────────────
def test_classify_unknown_is_not_nudgeable():
    v = nudge.classify_wait("the quick brown fox jumps over the lazy dog")
    assert v["kind"] == "unknown"
    assert v["nudgeable"] is False


def test_classify_empty_is_unknown_not_nudgeable():
    v = nudge.classify_wait("")
    assert v["kind"] == "unknown"
    assert v["nudgeable"] is False


# ─── should_nudge: gating ────────────────────────────────────────────────────
def _waiting_session(secs=120):
    """A waiting session that has been idle `secs` seconds (via explicit waiting_secs)."""
    return {"id": "s1", "state": "waiting", "waiting_secs": secs}


def test_should_nudge_gated_off_when_auto_nudge_false():
    ok, reason = nudge.should_nudge(_waiting_session(), "Continue? (y/n)",
                                    config=_cfg(auto_nudge=False))
    assert ok is False
    assert "auto_nudge" in reason


def test_should_nudge_gated_off_when_not_waiting():
    sess = {"id": "s1", "state": "running", "waiting_secs": 999}
    ok, reason = nudge.should_nudge(sess, "Continue? (y/n)", config=_cfg())
    assert ok is False
    assert "waiting" in reason


def test_should_nudge_gated_off_when_waited_too_short():
    # Waited 10s but the threshold is 45s.
    ok, reason = nudge.should_nudge(_waiting_session(secs=10), "Continue? (y/n)",
                                    config=_cfg(nudge_idle_secs=45))
    assert ok is False
    assert "nudge_idle_secs" in reason or "<" in reason


def test_should_nudge_gated_off_when_destructive_pending():
    pane = "$ rm -rf /data\nProceed? (y/n)"
    ok, reason = nudge.should_nudge(_waiting_session(), pane, config=_cfg())
    assert ok is False
    assert "nudgeable" in reason or "dangerous" in reason


def test_should_nudge_gated_off_when_decision_fork():
    pane = "1) refactor\n2) rewrite"
    ok, reason = nudge.should_nudge(_waiting_session(), pane, config=_cfg())
    assert ok is False


def test_should_nudge_gated_off_when_risk_at_or_above_ceiling():
    # A pending command that scores at review-level, with the ceiling set to review,
    # must be blocked (rank must be strictly BELOW the ceiling). We use a command
    # that the risk scorer rates at review but the guard does not call destructive.
    pane = "$ git push origin main\nContinue? (y/n)"
    scored = risk.score_tool_call("Bash", {"command": "git push origin main"})
    # Sanity: this command scores review-level (shared-state blast radius) and is
    # NOT classified destructive by the guard — so the only thing blocking a nudge
    # is the ceiling comparison.
    assert scored["action"] == "review"
    assert guard.classify_command("git push origin main")["destructive"] is False
    ok, reason = nudge.should_nudge(_waiting_session(), pane,
                                    config=_cfg(nudge_risk_max="review"))
    assert ok is False
    assert "nudge_risk_max" in reason or "ceiling" in reason


def test_should_nudge_honored_when_all_conditions_met():
    ok, reason = nudge.should_nudge(_waiting_session(secs=120), "Continue? (y/n)",
                                    config=_cfg(nudge_idle_secs=45, nudge_risk_max="confirm"))
    assert ok is True
    assert isinstance(reason, str) and reason


def test_should_nudge_honored_via_heartbeat_timestamp():
    # No explicit waiting_secs — derive the wait from heartbeat_at vs `now`.
    sess = {"id": "s1", "state": "waiting", "heartbeat_at": _iso(NOW - timedelta(seconds=120))}
    ok, reason = nudge.should_nudge(sess, "Continue? (y/n)", config=_cfg(), now=NOW)
    assert ok is True, reason


def test_should_nudge_conservative_when_no_timestamps():
    # Waiting, but no waiting_secs and no heartbeat/updated timestamps → must NOT nudge.
    sess = {"id": "s1", "state": "waiting"}
    ok, reason = nudge.should_nudge(sess, "Continue? (y/n)", config=_cfg(), now=NOW)
    assert ok is False
    assert "wait duration" in reason or "waiting_secs" in reason


def test_should_nudge_bad_session_fails_safe():
    ok, reason = nudge.should_nudge("not-a-dict", "Continue? (y/n)", config=_cfg())
    assert ok is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
