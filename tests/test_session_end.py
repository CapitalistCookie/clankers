"""End-to-end tests for hooks/session-end.sh (P6, audit M4): the SessionEnd
record must say WHY a session ended — end_reason (the hook input's own reason),
failure_reason (limit/API-error signature in the transcript tail, same catalog
as the subagent auto-resume detector), last_assistant_line — and the handoff
must carry a "Last activity" line so post-crash briefings aren't git-state-blind.

Hermetic: HOME→tmp (the memory-autocommit block cd's to $HOME/.claude and
exits — the live ~/.claude repo is never touched), CLANKER_DATA→conftest tmp,
unique session ids (the hook's /tmp dedup marker is per-session-id)."""
import json
import os
import subprocess
import uuid

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "hooks", "session-end.sh")


def _sid(tag):
    """Unique per RUN: the hook's /tmp/clanker-session-<id> dedup marker has a
    60s TTL shared across pytest invocations — a fixed id makes back-to-back
    runs (fast suite then full suite) silently skip the record write."""
    return f"p6-{tag}-{uuid.uuid4().hex[:10]}"


def _transcript_lines(final_error=None):
    lines = [
        json.dumps({"type": "user", "timestamp": "2026-07-22T05:00:00Z",
                    "message": {"role": "user", "content": "fix the flaky auth test"}}),
        json.dumps({"type": "assistant", "timestamp": "2026-07-22T05:01:00Z",
                    "message": {"model": "claude-fable-5",
                                "usage": {"input_tokens": 100, "output_tokens": 50},
                                "content": [{"type": "text",
                                             "text": "Working on the auth test now."}]}}),
        json.dumps({"type": "assistant", "timestamp": "2026-07-22T05:10:00Z",
                    "message": {"content": [{"type": "text",
                                             "text": "Fixed the auth test and pushed."}]}}),
    ]
    if final_error:
        lines.append(json.dumps({"type": "system", "timestamp": "2026-07-22T05:11:00Z",
                                 "content": final_error}))
    return "\n".join(lines) + "\n"


def _run_hook(tmp_path, session_id, transcript_text, cwd, reason="prompt_input_exit"):
    tp = tmp_path / f"{session_id}.jsonl"
    tp.write_text(transcript_text)
    payload = json.dumps({"session_id": session_id, "transcript_path": str(tp),
                          "cwd": str(cwd), "reason": reason})
    env = {**os.environ, "HOME": str(tmp_path)}
    r = subprocess.run(["bash", HOOK], input=payload, capture_output=True,
                       text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    day_dir = os.path.join(os.environ["CLANKER_DATA"], "raw", "sessions")
    rows = []
    for fn in os.listdir(day_dir):
        if fn.endswith(".jsonl"):
            with open(os.path.join(day_dir, fn)) as f:
                rows += [json.loads(l) for l in f if l.strip()]
    mine = [x for x in rows if x.get("session_id") == session_id]
    assert mine, f"no record written for {session_id}"
    return mine[-1]   # last-write-wins, same as analyze.load_sessions dedup


def test_limit_kill_yields_failure_reason_and_last_line(tmp_path):
    rec = _run_hook(tmp_path, _sid("limit"),
                    _transcript_lines(final_error="API error — Rate limited, retry later"),
                    cwd=tmp_path)
    assert rec["failure_reason"] == "Rate limited"   # matched from LIMIT_SIGNS
    assert rec["end_reason"] == "prompt_input_exit"
    assert rec["last_assistant_line"] == "Fixed the auth test and pushed."
    assert rec["tokens"]["input"] == 100             # existing fields intact


def test_clean_session_has_null_failure_reason(tmp_path):
    rec = _run_hook(tmp_path, _sid("clean"), _transcript_lines(), cwd=tmp_path,
                    reason="clear")
    assert rec["failure_reason"] is None
    assert rec["end_reason"] == "clear"


# ── P7: heartbeat stub (SessionStart) + duration cap-at-write ────────────────

START_HOOK = os.path.join(os.path.dirname(HOOK), "session-start.sh")


def _read_rows(session_id):
    day_dir = os.path.join(os.environ["CLANKER_DATA"], "raw", "sessions")
    rows = []
    for fn in sorted(os.listdir(day_dir)):
        if fn.endswith(".jsonl"):
            with open(os.path.join(day_dir, fn)) as f:
                rows += [json.loads(l) for l in f if l.strip()]
    return [x for x in rows if x.get("session_id") == session_id]


def test_session_start_writes_open_stub(tmp_path):
    """A session must EXIST in telemetry the moment it starts (07-20/21: ~49
    live sessions, zero rows). The stub is minimal and marked outcome=open."""
    sid = _sid("stub")
    payload = json.dumps({"session_id": sid, "cwd": str(tmp_path), "source": "startup"})
    env = {**os.environ, "HOME": str(tmp_path)}
    r = subprocess.run(["bash", START_HOOK], input=payload, capture_output=True,
                       text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    rows = _read_rows(sid)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "open"
    assert rows[0]["source"] == "startup"
    assert rows[0]["cwd"] == str(tmp_path)
    assert "duration_s" not in rows[0]   # stubs carry no final numbers


def test_lifecycle_stub_then_final_row(tmp_path):
    """SessionStart stub + SessionEnd final row for the same id: the final row
    is appended AFTER the stub, so consumers' last-write-wins dedup
    (analyze.load_sessions) sees the completed record."""
    sid = _sid("lifecycle")
    payload = json.dumps({"session_id": sid, "cwd": str(tmp_path), "source": "startup"})
    env = {**os.environ, "HOME": str(tmp_path)}
    subprocess.run(["bash", START_HOOK], input=payload, capture_output=True,
                   text=True, env=env, timeout=60)
    _run_hook(tmp_path, sid, _transcript_lines(), cwd=tmp_path)  # asserts 1 final row
    rows = _read_rows(sid)
    assert len(rows) == 2
    assert rows[0]["outcome"] == "open" and rows[-1]["outcome"] != "open"


def test_duration_capped_at_write_wall_clock_raw(tmp_path):
    """19-day OOM rows: duration_s is work-time (capped 8h AT WRITE, audit M4 —
    'one future consumer will forget'), wall_clock_s keeps the raw span."""
    lines = [
        json.dumps({"type": "user", "timestamp": "2026-07-03T05:00:00Z",
                    "message": {"role": "user", "content": "long-lived session"}}),
        json.dumps({"type": "assistant", "timestamp": "2026-07-22T05:00:00Z",
                    "message": {"content": [{"type": "text", "text": "still here"}]}}),
    ]
    rec = _run_hook(tmp_path, _sid("cap"), "\n".join(lines) + "\n", cwd=tmp_path)
    assert rec["duration_s"] == 28800                    # capped
    assert rec["wall_clock_s"] == 19 * 24 * 3600         # raw span preserved
    # short session: both equal, uncapped
    rec2 = _run_hook(tmp_path, _sid("short"), _transcript_lines(), cwd=tmp_path)
    assert rec2["duration_s"] == rec2["wall_clock_s"] == 600


def test_handoff_carries_last_activity_line(tmp_path):
    """cwd is a real git repo → the hook generates a handoff; it must contain
    the last assistant line (audit §6: 'what was I doing', not just git state)."""
    repo = tmp_path / "p6repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
                   check=True)
    _run_hook(tmp_path, _sid("handoff"), _transcript_lines(), cwd=repo)
    handoff = os.path.join(os.environ["CLANKER_DATA"], "wiki", "projects",
                           "p6repo-handoff.md")
    assert os.path.exists(handoff), "handoff not generated for git cwd"
    text = open(handoff).read()
    assert "**Last activity:** Fixed the auth test and pushed." in text
    assert "**Branch:**" in text                     # git section still present
