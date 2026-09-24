"""`clanker analyze --rewrites` (lib/analyze.py:rewrite_report) and the
interactive / nested split in analyze.load_sessions (2026-09-24).

A rewrite is a call that read less from the cache than the call before it on
the same model (design audit proposal 3). The report counts it the way
hooks/session-end.sh counts rewrite_tokens, through the pricing block both
carry; a test runs the hook and the report on one transcript.

Hermetic: transcripts and session rows live in tmp_path; analyze.SESSIONS_DIR
is pointed there and the memo cleared."""
import json
import os
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import analyze  # noqa: E402

HOOK = os.path.join(ROOT, "hooks", "session-end.sh")


def _line(mid, model, cr, cc, ts, entry="cli", cwd="/work/proj"):
    return json.dumps({"type": "assistant", "timestamp": ts, "entrypoint": entry,
                       "cwd": cwd, "uuid": uuid.uuid4().hex, "requestId": "req_" + mid,
                       "message": {"id": mid, "model": model, "role": "assistant",
                                   "usage": {"input_tokens": 5, "output_tokens": 7,
                                             "cache_read_input_tokens": cr,
                                             "cache_creation_input_tokens": cc},
                                   "content": [{"type": "text", "text": "x"}]}})


def _boundary(ts):
    return json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": ts,
                       "content": "Conversation compacted"})


def _transcript(entry="cli"):
    """Rewrites: msg_3 2 min after msg_2 (fast), msg_5 after a compaction,
    msg_7 40 min after msg_6 (in the hour), msg_9 3 h after msg_8 (expiry)."""
    t = lambda h, m: f"2026-09-20T{h:02d}:{m:02d}:00.000Z"   # noqa: E731
    rows = [
        _line("msg_1", "claude-opus-5-5", 0, 50_000, t(1, 0), entry),
        _line("msg_2", "claude-opus-5-5", 50_000, 1_000, t(1, 1), entry),
        _line("msg_2", "claude-opus-5-5", 50_000, 1_000, t(1, 1), entry),   # 2nd block
        _line("msg_3", "claude-opus-5-5", 20_000, 31_000, t(1, 3), entry),  # fast rewrite
        _line("msg_4", "claude-opus-5-5", 51_000, 500, t(1, 4), entry),
        _boundary(t(1, 5)),
        _line("msg_5", "claude-opus-5-5", 20_000, 9_000, t(1, 6), entry),   # after compaction
        _line("msg_6", "claude-opus-5-5", 29_000, 100, t(1, 7), entry),
        _line("msg_7", "claude-opus-5-5", 20_000, 9_100, t(1, 47), entry),  # 40 min: in the hour
        _line("msg_8", "claude-opus-5-5", 29_100, 100, t(1, 48), entry),
        _line("msg_9", "claude-opus-5-5", 0, 29_200, t(4, 48), entry),      # 3 h: expiry
        _line("msg_10", "claude-sonnet-5", 0, 5_000, t(4, 49), entry),      # model switch
    ]
    return "\n".join(rows) + "\n"


def test_transcript_rewrites_counts_and_attributes(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_transcript())
    d = analyze.transcript_rewrites(str(p))
    assert d["api_calls"] == 10
    assert d["rewrites"] == 4
    assert d["rewrite_tokens"] == 31_000 + 9_000 + 9_100 + 29_200
    assert d["fast_tokens"] == 31_000
    assert d["hour_tokens"] == 31_000 + 9_100
    assert d["after_compact_tokens"] == 9_000
    assert d["compactions"] == 1
    assert d["cwd"] == "/work/proj" and d["entrypoint"] == "cli"
    assert d["model"] == "claude-opus-5-5"
    assert analyze.transcript_rewrites(str(tmp_path / "missing.jsonl")) is None


def test_hook_and_report_agree(tmp_path):
    """The hook's rewrite_tokens and the report's, on the same transcript."""
    sid = f"rw-{uuid.uuid4().hex[:10]}"
    tp = tmp_path / f"{sid}.jsonl"
    tp.write_text(_transcript())
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PROJECT_DIR")}
    env["HOME"] = str(tmp_path)
    r = subprocess.run(["bash", HOOK], capture_output=True, text=True, env=env, timeout=60,
                       input=json.dumps({"session_id": sid, "transcript_path": str(tp),
                                         "cwd": str(tmp_path), "reason": "other"}))
    assert r.returncode == 0, r.stderr
    day = os.path.join(os.environ["CLANKER_DATA"], "raw", "sessions")
    rows = [json.loads(l) for f in os.listdir(day) if f.endswith(".jsonl")
            for l in open(os.path.join(day, f)) if l.strip()]
    row = [x for x in rows if x.get("session_id") == sid][-1]
    assert row["rewrite_tokens"] == analyze.transcript_rewrites(str(tp))["rewrite_tokens"]


@pytest.fixture
def store(tmp_path, monkeypatch):
    sessions = tmp_path / "data" / "raw" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(analyze, "SESSIONS_DIR", str(sessions))
    analyze._sessions_memo.clear()
    yield sessions
    analyze._sessions_memo.clear()


def _write_rows(sessions, rows):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for r in rows:
        r.setdefault("timestamp", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    (sessions / (now.strftime("%Y-%m-%d") + ".jsonl")).write_text(
        "".join(json.dumps(r) + "\n" for r in rows))


def test_report_ranks_transcripts_and_row_only_sessions(tmp_path, store, capsys, monkeypatch):
    monkeypatch.setenv("CLANKER_TRANSCRIPTS_DIR", str(tmp_path / "transcripts"))
    tdir = tmp_path / "transcripts" / "-work-proj"
    tdir.mkdir(parents=True)
    (tdir / "sess-big.jsonl").write_text(_transcript())
    (tdir / "sess-nested.jsonl").write_text(
        _line("msg_a", "claude-sonnet-5", 9_000, 1, "2026-09-20T01:00:00Z", "sdk-cli")
        + "\n" + _line("msg_b", "claude-sonnet-5", 0, 9_500, "2026-09-20T01:01:00Z", "sdk-cli")
        + "\n")
    _write_rows(store, [
        {"session_id": "sess-rowonly", "cwd": "/gone", "rewrite_tokens": 50_000,
         "api_calls": 4, "nested": False, "entrypoint": "cli",
         "tokens": {"cache_create": 100_000}},
        {"session_id": "sess-legacy", "cwd": "/old"},              # no data at all
    ])
    rc = analyze.rewrite_report(last_days=7, transcripts_dir=str(tmp_path / "transcripts"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "3 sessions (2 from transcripts, 1 from rows only; 1 rows without data)" in out
    table = out.split("session", 2)[-1].splitlines()[1:]
    assert [ln.split()[-2] for ln in table] == ["sess-big", "sess-rowonly", "sess-nested"]
    assert table[0].split()[1] == "0.1M" and "/work/proj" in table[0]
    assert " nested " in table[2]
    rc = analyze.rewrite_report(last_days=7, transcripts_dir=str(tmp_path / "transcripts"),
                                json_output=True, top=1)
    rows = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert len(rows) == 1 and rows[0]["rank"] == 1 and rows[0]["session_id"] == "sess-big"
    assert rows[0]["rewrite_tokens"] == 78_300 and rows[0]["source"] == "transcript"
    assert analyze.run_analysis("daily", rewrites=True, last_days=7) == 0   # the CLI's path
    assert "3 sessions (2 from transcripts" in capsys.readouterr().out     # env dir, not ~/.claude


def test_load_sessions_splits_interactive_nested_unknown(store, capsys):
    _write_rows(store, [
        {"session_id": "a", "nested": False, "entrypoint": "cli"},
        {"session_id": "b", "nested": True, "entrypoint": "cli"},       # env said sdk-cli
        {"session_id": "c", "entrypoint": "sdk-cli"},
        {"session_id": "d", "kind": "nested", "wrap": True},             # clanker wrap row
        {"session_id": "e", "outcome": "open", "nested": False},         # a start stub
        {"session_id": "f", "project": "p"},                             # before 2026-09-24
    ])
    rows = analyze.load_sessions(last_days=7)
    kinds = {r["session_id"]: r["kind"] for r in rows}
    assert kinds == {"a": "interactive", "b": "nested", "c": "nested", "d": "nested",
                     "e": "interactive", "f": "unknown"}
    assert {r["session_id"] for r in analyze.load_sessions(last_days=7, kind="nested")} == \
        {"b", "c", "d"}
    assert {r["session_id"] for r in analyze.load_sessions(
        last_days=7, kind=("interactive", "unknown"))} == {"a", "e", "f"}
    analyze.run_analysis("daily")
    assert "Interactive: 2 | Nested: 3 | Unknown (rows before 2026-09-24): 1" in \
        capsys.readouterr().out
