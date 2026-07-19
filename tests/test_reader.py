"""Reader-mode transcript parsing — hermetic (synthetic transcript files)."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import reader  # noqa: E402


def _w(f, obj):
    f.write(json.dumps(obj) + "\n")


def _mk_transcript(path):
    with open(path, "w") as f:
        _w(f, {"type": "user", "timestamp": "T1",
               "message": {"content": "fix the login bug"}})
        _w(f, {"type": "assistant", "timestamp": "T2", "message": {"content": [
            {"type": "text", "text": "Looking at the auth module.\n\n| a | b |\n|---|---|\n| 1 | 2 |"},
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "pytest tests/ -q\n# second line ignored"}},
        ]}})
        _w(f, {"type": "user", "timestamp": "T3", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "boom"}]}})
        _w(f, {"type": "user", "timestamp": "T4",
               "message": {"content": "<system-reminder>noise</system-reminder>"}})
        _w(f, {"type": "assistant", "timestamp": "T5", "message": {"content": [
            {"type": "text", "text": "Fixed — tests green."}]}})
        f.write("{not json\n")
        _w(f, {"type": "user", "timestamp": "T6",
               "message": {"content": "<command-name>/goal</command-name><command-args>x</command-args>"}})


def test_parse_units_roles_tools_and_noise_filtering():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "t.jsonl")
        _mk_transcript(p)
        out = reader.parse_tail(p)
        roles = [u["role"] for u in out["units"]]
        assert roles == ["user", "assistant", "result", "assistant", "user"]
        a = out["units"][1]
        assert "auth module" in a["text"] and "| a | b |" in a["text"]
        assert a["tools"] == [{"name": "Bash", "detail": "pytest tests/ -q"}]
        assert out["units"][2]["errors"] == 1
        assert out["units"][-1]["text"] == "/goal"   # command wrapper unwrapped
        assert out["reset"] is True and out["offset"] == out["size"]


def test_incremental_offset_and_partial_last_line():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "t.jsonl")
        _mk_transcript(p)
        first = reader.parse_tail(p)
        # append one complete + one PARTIAL record
        with open(p, "a") as f:
            _w(f, {"type": "user", "timestamp": "T7",
                   "message": {"content": "next step please"}})
            f.write('{"type":"assistant","message":{"content":[{"type":"te')
        out = reader.parse_tail(p, offset=first["offset"])
        assert [u["text"] for u in out["units"]] == ["next step please"]
        assert out["reset"] is False
        # the partial record was NOT consumed; completing it yields it next call
        with open(p, "a") as f:
            f.write('xt","text":"done"}]}}\n')
        out2 = reader.parse_tail(p, offset=out["offset"])
        assert [u["role"] for u in out2["units"]] == ["assistant"]
        assert out2["units"][0]["text"] == "done"


def test_backward_pagination_reaches_session_start(monkeypatch):
    # Small window so a 30-record transcript spans several pages.
    monkeypatch.setattr(reader, "TAIL_BYTES", 600)
    monkeypatch.setattr(reader, "MAX_UNITS", 1000)
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "t.jsonl")
        with open(p, "w") as f:
            for i in range(30):
                _w(f, {"type": "user", "timestamp": f"T{i}",
                       "message": {"content": f"message number {i:03d}"}})
        tail = reader.parse_tail(p)
        assert tail["start"] > 0          # window did not cover the whole file
        texts = [u["text"] for u in tail["units"]]
        cursor, pages = tail["start"], 0
        while True:
            win = reader.parse_window(p, cursor)
            texts = [u["text"] for u in win["units"]] + texts
            pages += 1
            assert pages < 50, "pagination did not terminate"
            if win["at_start"]:
                break
            cursor = win["start"]
        # walked back to the very first record, no gaps, no duplicates
        assert texts == [f"message number {i:03d}" for i in range(30)]
        assert pages >= 2


def test_rotation_resets():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "t.jsonl")
        _mk_transcript(p)
        big = reader.parse_tail(p)["offset"]
        with open(p, "w") as f:   # rewritten shorter (rotation)
            _w(f, {"type": "user", "message": {"content": "fresh"}})
        out = reader.parse_tail(p, offset=big)
        assert out["reset"] is True
        assert [u["text"] for u in out["units"]] == ["fresh"]
