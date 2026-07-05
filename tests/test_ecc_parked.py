"""Hermetic tests for ecc.parked — the parked / stuck-session detector.

Builds tiny fake Claude-Code transcript .jsonl files in a tmp dir and asserts
the classification. No filesystem touched outside tempdirs; no network.

Run: python3 -m pytest tests/test_ecc_parked.py -v   (or: python3 tests/test_ecc_parked.py)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from ecc import parked  # noqa: E402

# Fixed reference clock so ages are deterministic.
NOW = 1_700_000_000.0  # epoch seconds
HOUR = 3600


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _write(lines, dirpath=None, name="session.jsonl"):
    """Write a list of dict entries (or raw strings) as JSONL; return the path."""
    dirpath = dirpath or tempfile.mkdtemp(prefix="clk-parked-test-")
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln if isinstance(ln, str) else json.dumps(ln))
            f.write("\n")
    return path


def _assistant_tool_use(ts_epoch, tool_id, name="Bash", session="sess-A", **inp):
    return {
        "type": "assistant",
        "sessionId": session,
        "timestamp": _iso(ts_epoch),
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}],
        },
    }


def _tool_result(ts_epoch, tool_id, session="sess-A"):
    return {
        "type": "user",
        "sessionId": session,
        "timestamp": _iso(ts_epoch),
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}],
        },
    }


# --------------------------------------------------------------------------- #
# Core classification
# --------------------------------------------------------------------------- #

def test_unanswered_old_tool_use_is_attention():
    # tool_use issued 1h ago, never answered -> parked (default threshold 1800s).
    path = _write([_assistant_tool_use(NOW - HOUR, "t1", command="sleep 9999")])
    rec = parked.scan_transcript(path, now=NOW)
    assert rec is not None
    assert rec["session"] == "sess-A"
    assert rec["state"] == "attention", rec
    assert rec["pending_tool"] == {"name": "Bash", "age_secs": HOUR}
    assert any("pending_tool_result" in r for r in rec["reasons"]), rec["reasons"]
    assert "interrupt" in rec["recommended_action"].lower()


def test_recent_unanswered_tool_use_is_ok():
    # Pending only 60s -> below the 1800s threshold -> ok (still reported as pending_tool).
    path = _write([_assistant_tool_use(NOW - 60, "t1", command="echo hi")])
    rec = parked.scan_transcript(path, now=NOW)
    assert rec is not None
    assert rec["state"] == "ok", rec
    assert rec["pending_tool"] == {"name": "Bash", "age_secs": 60}
    assert rec["reasons"] == []


def test_answered_tool_use_is_ok():
    # Every tool_use has a matching tool_result -> ok, no pending tool.
    path = _write([
        _assistant_tool_use(NOW - HOUR, "t1", command="ls"),
        _tool_result(NOW - HOUR + 5, "t1"),
        _assistant_tool_use(NOW - HOUR + 10, "t2", command="pwd"),
        _tool_result(NOW - HOUR + 12, "t2"),
    ])
    rec = parked.scan_transcript(path, now=NOW)
    assert rec is not None
    assert rec["state"] == "ok", rec
    assert rec["pending_tool"] is None
    assert rec["reasons"] == []


def test_empty_file_is_none():
    path = _write([])
    assert parked.scan_transcript(path, now=NOW) is None


def test_garbage_file_is_none():
    # All lines unparseable -> no objects -> None (malformed lines are skipped).
    path = _write(["{not json", "<<<garbage>>>", "   ", "}{"])
    assert parked.scan_transcript(path, now=NOW) is None


def test_nonexistent_file_is_none():
    assert parked.scan_transcript("/no/such/transcript.jsonl", now=NOW) is None


# --------------------------------------------------------------------------- #
# Robustness: malformed lines, missing timestamps, alternate shapes
# --------------------------------------------------------------------------- #

def test_malformed_lines_are_skipped_but_valid_classified():
    # A garbage line interleaved with a valid old unanswered tool_use.
    path = _write([
        "{ broken json line",
        _assistant_tool_use(NOW - HOUR, "t1", command="sleep 9999"),
        "another !!! broken line",
    ])
    rec = parked.scan_transcript(path, now=NOW)
    assert rec is not None
    assert rec["state"] == "attention", rec
    assert rec["pending_tool"]["age_secs"] == HOUR


def test_missing_block_timestamp_falls_back_to_last_assistant_ts():
    # tool_use lives in an entry with no timestamp; the prior assistant entry
    # (1h ago) supplies the pending-since reference -> attention.
    no_ts = {
        "type": "assistant",
        "sessionId": "sess-A",
        "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "x"}}]},
    }
    path = _write([
        {"type": "assistant", "sessionId": "sess-A", "timestamp": _iso(NOW - HOUR),
         "message": {"role": "assistant", "content": [{"type": "text", "text": "thinking"}]}},
        no_ts,
    ])
    rec = parked.scan_transcript(path, now=NOW)
    assert rec is not None
    assert rec["state"] == "attention", rec
    assert rec["pending_tool"]["age_secs"] == HOUR


def test_direct_tool_use_and_tool_result_entry_shapes():
    # No message.content wrapper: direct {type: tool_use} / {type: tool_result}.
    path = _write([
        {"type": "tool_use", "id": "d1", "name": "Bash", "sessionId": "sess-B",
         "timestamp": _iso(NOW - HOUR), "input": {"command": "y"}},
        {"type": "tool_result", "tool_use_id": "d1", "sessionId": "sess-B",
         "timestamp": _iso(NOW - HOUR + 1)},
    ])
    rec = parked.scan_transcript(path, now=NOW)
    assert rec is not None
    assert rec["session"] == "sess-B"
    assert rec["state"] == "ok", rec          # answered
    assert rec["pending_tool"] is None


def test_session_id_falls_back_to_basename():
    # No sessionId field anywhere -> basename (minus .jsonl) is the session.
    path = _write([
        {"type": "assistant", "timestamp": _iso(NOW - 30),
         "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
    ], name="abc123.jsonl")
    rec = parked.scan_transcript(path, now=NOW)
    assert rec is not None
    assert rec["session"] == "abc123", rec


# --------------------------------------------------------------------------- #
# ScheduleWakeup-overdue logic
# --------------------------------------------------------------------------- #

def test_schedule_wakeup_overdue_is_attention():
    # Wake scheduled 1h ago for 60s; now far past scheduledAt + delay*grace,
    # and no assistant progress after due -> overdue -> attention.
    # The wake tool_use is itself answered so the ONLY signal is the overdue
    # wake (isolating the ScheduleWakeup-overdue path from the pending-tool path).
    path = _write([
        _assistant_tool_use(NOW - HOUR, "w1", name="ScheduleWakeup", delaySeconds=60),
        _tool_result(NOW - HOUR + 1, "w1"),
    ])
    rec = parked.scan_transcript(path, now=NOW)
    assert rec is not None
    assert rec["state"] == "attention", rec
    assert rec["pending_tool"] is None, rec        # wake was answered
    assert any("schedule_wakeup_overdue" in r for r in rec["reasons"]), rec["reasons"]
    assert "wake" in rec["recommended_action"].lower()


def test_schedule_wakeup_with_progress_after_due_is_ok():
    # Wake fired long ago BUT an assistant entry exists after the due time
    # (the model resumed) -> not overdue. ScheduleWakeup is itself a tool_use,
    # but the later text entry answers nothing; we add a tool_result to keep it
    # from being a stale pending tool, isolating the wake-progress logic.
    path = _write([
        _assistant_tool_use(NOW - HOUR, "w1", name="ScheduleWakeup", delaySeconds=60),
        _tool_result(NOW - HOUR + 1, "w1"),
        {"type": "assistant", "sessionId": "sess-A", "timestamp": _iso(NOW - HOUR + 120),
         "message": {"role": "assistant", "content": [{"type": "text", "text": "resumed"}]}},
    ])
    rec = parked.scan_transcript(path, now=NOW)
    assert rec is not None
    assert rec["state"] == "ok", rec
    assert rec["reasons"] == []


# --------------------------------------------------------------------------- #
# Helpers exported for clanker reuse
# --------------------------------------------------------------------------- #

def test_extract_helpers_pair_uses_and_results():
    objs = [
        _assistant_tool_use(NOW - 10, "a", command="x"),
        _assistant_tool_use(NOW - 9, "b", command="y"),
        _tool_result(NOW - 8, "a"),
    ]
    uses = parked.extract_tool_uses(objs)
    ids = parked.extract_tool_result_ids(objs)
    assert sorted(u["id"] for u in uses) == ["a", "b"]
    assert ids == ["a"]
    # b is the unanswered (pending) one.
    pending = [u["id"] for u in uses if u["id"] not in set(ids)]
    assert pending == ["b"]


# --------------------------------------------------------------------------- #
# Directory scan
# --------------------------------------------------------------------------- #

def test_scan_dir_returns_records_newest_first_and_skips_empty():
    root = tempfile.mkdtemp(prefix="clk-parked-dir-")
    proj = os.path.join(root, "-some-project")
    os.makedirs(proj)
    # Older: ok. Newer: attention. Plus an empty file that must be skipped.
    p_old = _write([
        _assistant_tool_use(NOW - 120, "t1", command="z"), _tool_result(NOW - 118, "t1"),
    ], dirpath=proj, name="old.jsonl")
    p_new = _write([_assistant_tool_use(NOW - HOUR, "t2", command="sleep 9999")],
                   dirpath=proj, name="new.jsonl")
    _write([], dirpath=proj, name="empty.jsonl")
    # Make mtimes deterministic: new.jsonl strictly newer than old.jsonl.
    os.utime(p_old, (NOW - 200, NOW - 200))
    os.utime(p_new, (NOW - 10, NOW - 10))

    recs = parked.scan_dir(root, now=NOW)
    assert len(recs) == 2, recs                      # empty skipped
    assert recs[0]["path"] == p_new                  # newest mtime first
    assert recs[0]["state"] == "attention"
    assert recs[1]["path"] == p_old
    assert recs[1]["state"] == "ok"
    # Filterable by state.
    attention = [r for r in recs if r["state"] == "attention"]
    assert len(attention) == 1


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
