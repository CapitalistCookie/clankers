"""The idle-agent gate (global rule 30): teammate-idle-record.py (TeammateIdle)
writes a record per idle teammate; idle-agent-gate.py (Stop, main conversation)
blocks the lead's stop once per idle period and names the teammates to TaskStop.

Hermetic: CLANKER_DATA and CLAUDE_CONFIG_DIR point into tmp_path; the hooks run
as subprocesses with fake payloads, as Claude Code runs them."""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, "hooks", "harness")
RECORD = os.path.join(HARNESS, "teammate-idle-record.py")
GATE = os.path.join(HARNESS, "idle-agent-gate.py")
SID = "11111111-2222-3333-4444-555555555555"
TEAM = "session-11111111"


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("IDLE_GATE_OFF", "IDLE_GATE_MINUTES")}
    env.update(CLANKER_DATA=str(tmp_path / "data"), CLAUDE_CONFIG_DIR=str(tmp_path / "cfg"))
    env.update(extra)
    return env


def _run(script, tmp_path, payload, **extra):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    t0 = time.time()
    r = subprocess.run([sys.executable, script], input=data, capture_output=True, text=True,
                       env=_env(tmp_path, **extra), timeout=30)
    return r, time.time() - t0


def _team(tmp_path, members, lead_sid=SID, team=TEAM):
    d = tmp_path / "cfg" / "teams" / team
    d.mkdir(parents=True, exist_ok=True)
    ms = [{"agentId": f"team-lead@{team}", "name": "team-lead", "joinedAt": 0}]
    ms += [{"agentId": f"{n}@{team}", "name": n, "joinedAt": 1000} for n in members]
    (d / "config.json").write_text(json.dumps(
        {"name": team, "leadSessionId": lead_sid, "members": ms}))


def _lead_transcript(tmp_path, sid=SID):
    p = tmp_path / "proj" / (sid + ".jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    return p


def _teammate(tmp_path, lead, agent_id, final_text):
    sub = tmp_path / "proj" / lead.stem / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "message": {"role": "assistant",
                                              "content": [{"type": "text", "text": final_text}]}}]
    p = sub / f"agent-{agent_id}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    os.utime(p, (time.time() - 7200, time.time() - 7200))   # written before it went idle
    return p


def _idle(tmp_path, name, final_text, agent_id=None):
    lead = _lead_transcript(tmp_path)
    agent_id = agent_id or f"a{name}-0123456789abcdef"
    _teammate(tmp_path, lead, agent_id, final_text)
    r, _ = _run(RECORD, tmp_path, {
        "hook_event_name": "TeammateIdle", "session_id": SID, "team_name": TEAM,
        "teammate_name": name, "agent_id": agent_id, "agent_type": "opus-high",
        "transcript_path": str(lead), "cwd": str(tmp_path)})
    assert r.returncode == 0 and r.stdout == "", r
    return tmp_path / "data" / "raw" / "idle-gate" / TEAM / f"{name}.json"


def _stop(tmp_path, active=False, **extra):
    lead = _lead_transcript(tmp_path)
    return _run(GATE, tmp_path, {"hook_event_name": "Stop", "session_id": SID,
                                 "transcript_path": str(lead), "cwd": str(tmp_path),
                                 "stop_hook_active": active}, **extra)


def _age(rec_path, minutes):
    rec = json.loads(rec_path.read_text())
    rec["idle_ts"] -= minutes * 60
    rec_path.write_text(json.dumps(rec))


def _errors(tmp_path):
    d = tmp_path / "data" / "raw" / "health"
    return [json.loads(l) for f in sorted(d.glob("hook-errors-*.jsonl")) if d.is_dir()
            for l in f.read_text().splitlines() if l.strip()]


def test_record_marks_done_only_on_the_final_line(tmp_path):
    a = json.loads(_idle(tmp_path, "alpha", "report\nAGENT-DONE\n").read_text())
    b = json.loads(_idle(tmp_path, "beta", "AGENT-DONE was mentioned\nstill working").read_text())
    c = json.loads(_idle(tmp_path, "gamma", "handed\n  AGENT-HANDED-OFF").read_text())
    assert (a["done"], b["done"], c["done"]) == (True, False, True)
    assert a["asked"] is False and a["lead_session_id"] == SID and a["idle_at"].endswith("Z")


def test_done_blocks_once_naming_the_teammate(tmp_path):
    _team(tmp_path, ["alpha", "beta"])
    rec = _idle(tmp_path, "alpha", "all done\nAGENT-DONE")
    _idle(tmp_path, "beta", "waiting for input")              # fresh, not done: not flagged
    r, dt = _stop(tmp_path)
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["decision"] == "block", r
    assert "alpha (done, idle 0m)" in out["reason"] and "beta" not in out["reason"]
    assert "TaskStop <name> now (global rule 30)" in out["reason"]
    assert json.loads(rec.read_text())["asked"] is True
    assert dt < 1.0
    r2, _ = _stop(tmp_path)                                   # asked → pass
    assert r2.returncode == 0 and r2.stdout == ""


def test_long_idle_blocks_short_idle_passes(tmp_path):
    _team(tmp_path, ["beta"])
    rec = _idle(tmp_path, "beta", "waiting")
    _age(rec, 10)
    r, _ = _stop(tmp_path)
    assert r.stdout == ""
    _age(rec, 25)                                             # now 35 min
    r, _ = _stop(tmp_path)
    assert "beta (not done, idle 35m)" in json.loads(r.stdout)["reason"]
    rec2 = _idle(tmp_path, "beta", "AGENT-DONE")              # a new idle period resets asked
    assert json.loads(rec2.read_text())["asked"] is False
    r, _ = _stop(tmp_path)
    assert "beta (done" in json.loads(r.stdout)["reason"]


def test_idle_gate_minutes_env(tmp_path):
    _team(tmp_path, ["beta"])
    _age(_idle(tmp_path, "beta", "waiting"), 6)
    r, _ = _stop(tmp_path, IDLE_GATE_MINUTES="5")
    assert "beta (not done, idle 6m)" in json.loads(r.stdout)["reason"]


def test_stop_hook_active_passes(tmp_path):
    _team(tmp_path, ["alpha"])
    rec = _idle(tmp_path, "alpha", "AGENT-DONE")
    r, _ = _stop(tmp_path, active=True)
    assert r.returncode == 0 and r.stdout == ""
    assert json.loads(rec.read_text())["asked"] is False


def test_member_already_stopped_passes(tmp_path):
    _team(tmp_path, ["beta"])                                 # alpha was TaskStopped
    _idle(tmp_path, "alpha", "AGENT-DONE")
    r, _ = _stop(tmp_path)
    assert r.returncode == 0 and r.stdout == ""


def test_resumed_lead_finds_its_team_through_the_sidecars(tmp_path):
    _team(tmp_path, ["alpha"], lead_sid="99999999-old-session")
    _idle(tmp_path, "alpha", "AGENT-DONE")
    sub = tmp_path / "proj" / SID / "subagents"
    (sub / "agent-aalpha-0123456789abcdef.meta.json").write_text(json.dumps({"teamName": TEAM}))
    r, _ = _stop(tmp_path)
    assert "alpha (done" in json.loads(r.stdout)["reason"]


def test_woken_teammate_is_not_flagged(tmp_path):
    _team(tmp_path, ["alpha"])
    rec = _idle(tmp_path, "alpha", "AGENT-DONE")
    _age(rec, 5)
    os.utime(json.loads(rec.read_text())["transcript"], None)  # it wrote after going idle
    r, _ = _stop(tmp_path)
    assert r.stdout == ""


def test_no_team_passes(tmp_path):
    r, _ = _stop(tmp_path)
    assert r.returncode == 0 and r.stdout == "" and _errors(tmp_path) == []


def test_garbage_payload_and_files_pass_with_a_hook_error(tmp_path):
    r, _ = _run(GATE, tmp_path, "not json")
    assert r.returncode == 0 and r.stdout == ""
    r, _ = _run(RECORD, tmp_path, "{broken")
    assert r.returncode == 0 and r.stdout == ""
    _team(tmp_path, ["alpha"])
    d = tmp_path / "data" / "raw" / "idle-gate" / TEAM
    d.mkdir(parents=True)
    (d / "alpha.json").write_text("garbage{")
    r, _ = _stop(tmp_path)
    assert r.returncode == 0 and r.stdout == ""
    (tmp_path / "cfg" / "teams" / TEAM / "config.json").write_text("garbage{")
    r, _ = _stop(tmp_path)
    assert r.returncode == 0 and r.stdout == ""
    hooks = [e["hook"] for e in _errors(tmp_path)]
    assert hooks.count("idle-agent-gate.py") >= 3 and "teammate-idle-record.py" in hooks


def test_missing_teammate_transcript_records_not_done_with_a_hook_error(tmp_path):
    r, _ = _run(RECORD, tmp_path, {"session_id": SID, "team_name": TEAM, "teammate_name": "x",
                                   "agent_id": "ax-1", "transcript_path": "/nonexistent/l.jsonl"})
    assert r.returncode == 0 and r.stdout == ""
    rec = json.loads((tmp_path / "data" / "raw" / "idle-gate" / TEAM / "x.json").read_text())
    assert rec["done"] is False
    assert any(e["hook"] == "teammate-idle-record.py" for e in _errors(tmp_path))
