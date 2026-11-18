"""agent-message-gate.py (PreToolUse SendMessage, global rule 30): a subagent's
or teammate's message to a stopped teammate revives its whole context
(11-18 10:21Z, look4 -> perf, 452k tokens). The gate blocks it; with
CLANKER_AGENT_MESSAGES=lead-only it blocks every send except to the lead.

Hermetic: CLAUDE_CONFIG_DIR and CLANKER_DATA point into tmp_path; the hook runs
as a subprocess with payloads shaped like the one captured on 2026-11-18
(Claude Code 2.1.322): session_id and transcript_path are the lead's,
agent_id is the caller's, tool_input has to/recipient/recipient_kind."""
import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "hooks", "harness", "agent-message-gate.py")
SID = "0e769cc0-bad5-4463-af00-103841897abc"
FIRST = "233c55cf-0e44-4c6c-9716-5e3cfbb263ed"
TEAM = "session-233c55cf"
CALLER = "alook4-fcdfeb74c9517537"


@pytest.fixture
def box(tmp_path):
    cfgdir = tmp_path / "cfg"
    team = cfgdir / "teams" / TEAM
    team.mkdir(parents=True)
    team.joinpath("config.json").write_text(json.dumps({
        "name": TEAM, "leadAgentId": "team-lead@" + TEAM, "leadSessionId": FIRST,
        "members": [{"name": "team-lead", "agentType": "team-lead"},
                    {"name": "look4"}, {"name": "interiors"}]}))
    proj = tmp_path / "projects" / "-repo"
    sub = proj / SID / "subagents"
    sub.mkdir(parents=True)
    for name, hexid in (("look4", "fcdfeb74c9517537"), ("perf", "3e907590154536fe"),
                        ("interiors", "1111222233334444")):
        sub.joinpath(f"agent-a{name}-{hexid}.meta.json").write_text(
            json.dumps({"name": name, "teamName": TEAM}))
    old = proj / FIRST / "subagents"                 # the team's first lead session
    old.mkdir(parents=True)
    old.joinpath("agent-afid-76a0e3e095251b8c.meta.json").write_text(
        json.dumps({"name": "fid", "teamName": TEAM}))
    rec = tmp_path / "data" / "raw" / "idle-gate" / TEAM
    rec.mkdir(parents=True)
    rec.joinpath("wx.json").write_text(json.dumps({"done": True}))
    return {"tmp": tmp_path, "cfg": str(cfgdir), "transcript": str(proj / (SID + ".jsonl"))}


def _run(box, to=None, agent_id=CALLER, env_extra=None, raw=None):
    env = {k: v for k, v in os.environ.items() if k != "CLANKER_AGENT_MESSAGES"}
    env["CLAUDE_CONFIG_DIR"] = box["cfg"]
    env["CLANKER_DATA"] = str(box["tmp"] / "data")
    env.update(env_extra or {})
    if raw is None:
        p = {"session_id": SID, "transcript_path": box["transcript"], "cwd": "/repo",
             "hook_event_name": "PreToolUse", "tool_name": "SendMessage",
             "tool_input": {"to": to, "message": "x", "summary": "x", "type": "message",
                            "recipient": to, "recipient_kind": "main" if to == "main" else "name"}}
        if agent_id:
            p["agent_id"], p["agent_type"] = agent_id, "opus-high"
        raw = json.dumps(p)
    return subprocess.run([sys.executable, GATE], input=raw, capture_output=True, text=True,
                          env=env, timeout=30)


def _rows(box):
    d = box["tmp"] / "data" / "raw" / "health"
    if not d.exists():
        return []
    return [json.loads(l) for f in sorted(d.iterdir()) for l in f.read_text().splitlines()]


@pytest.mark.parametrize("to", ["perf", "aperf-3e907590154536fe", "perf [3fa9c1]",
                                "fid", "wx"])
def test_a_stopped_teammate_is_blocked(box, to):
    r = _run(box, to)
    name = "perf" if "perf" in to else to
    assert r.returncode == 2
    assert f"{name} is stopped; a message revives it with its whole context." in r.stderr
    assert "global rule 30" in r.stderr and _rows(box) == []


@pytest.mark.parametrize("to", ["interiors", "look4", "main", "team-lead", "ghost"])
def test_live_teammates_the_lead_and_unknown_names_pass(box, to):
    r = _run(box, to)
    assert r.returncode == 0 and r.stderr == "" and _rows(box) == []


def test_lead_only_mode_blocks_every_send_but_the_lead(box):
    env = {"CLANKER_AGENT_MESSAGES": "lead-only"}
    for to in ("interiors", "ghost", "perf"):
        r = _run(box, to, env_extra=env)
        assert r.returncode == 2 and "lead-only" in r.stderr and to in r.stderr
    for to in ("main", "team-lead"):
        assert _run(box, to, env_extra=env).returncode == 0


def test_lead_only_mode_without_a_team_still_blocks(box):
    r = _run(box, "worker", env_extra={"CLANKER_AGENT_MESSAGES": "lead-only",
                                       "CLAUDE_CONFIG_DIR": str(box["tmp"] / "none")})
    assert r.returncode == 2
    assert _run(box, "main", env_extra={"CLANKER_AGENT_MESSAGES": "lead-only"}).returncode == 0


def test_the_lead_is_never_gated(box):
    for env in ({}, {"CLANKER_AGENT_MESSAGES": "lead-only"}):
        assert _run(box, "perf", agent_id=None, env_extra=env).returncode == 0
        assert _run(box, "interiors", agent_id=None, env_extra=env).returncode == 0


@pytest.mark.parametrize("raw", ["{not json", "[]", "null", "", json.dumps(
    {"agent_id": "a1", "tool_name": "SendMessage", "tool_input": "perf"})])
def test_garbage_input_fails_open(box, raw):
    r = _run(box, raw=raw)
    assert r.returncode == 0 and r.stdout == ""


def test_a_parse_failure_is_logged(box):
    _run(box, raw="{not json")
    row = _rows(box)[-1]
    assert row["hook"] == "agent-message-gate.py"
    assert row["stderr_tail"].startswith("parse hook input: ")


def test_a_broken_team_config_fails_open_and_is_logged(box):
    (box["tmp"] / "cfg" / "teams" / TEAM / "config.json").write_text("{broken")
    r = _run(box, "perf")
    assert r.returncode == 0
    assert any(row["stderr_tail"].startswith("read team config") for row in _rows(box))


def test_runs_in_under_100_ms(box):
    _run(box, "perf")                                  # warm the page cache
    t = time.monotonic()
    for _ in range(5):
        _run(box, "perf")
    assert (time.monotonic() - t) / 5 < 0.1
