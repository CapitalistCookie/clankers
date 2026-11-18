"""subagent-delivery-gate.py (PreToolUse Agent|Task): the report contract a
dispatch prompt must carry depends on whether agent teams are on.

Teams on: SendMessage to "main" AND final message (unchanged since 2026-08-01).
Teams off (operator 2026-11-18): a "final message" line is enough.

Hermetic: CLANKER_DATA points into tmp_path; the hook runs as a subprocess with
fake payloads, as Claude Code runs it."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "hooks", "harness", "subagent-delivery-gate.py")
TEAMS = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"
FULL = 'Do X. Deliver your final report via SendMessage with to: "main", AND make that report your final message.'
SOLO = "Do X. Make your final report your final message."
NONE = "Do X and tell me how it went."


def _run(tmp_path, prompt, teams, tool="Agent", raw=None):
    env = {k: v for k, v in os.environ.items() if k != TEAMS}
    env["CLANKER_DATA"] = str(tmp_path / "data")
    if teams is not None:
        env[TEAMS] = teams
    data = raw if raw is not None else json.dumps(
        {"session_id": "s1", "cwd": str(tmp_path), "tool_name": tool,
         "tool_input": {"description": "d", "prompt": prompt}})
    return subprocess.run([sys.executable, GATE], input=data, capture_output=True, text=True,
                          env=env, timeout=30)


def _rows(tmp_path):
    d = tmp_path / "data" / "raw" / "health"
    if not d.exists():
        return []
    return [json.loads(l) for f in sorted(d.iterdir()) for l in f.read_text().splitlines()]


@pytest.mark.parametrize("teams", ["1", "true", "TRUE", " yes ", "on"])
def test_teams_on_needs_the_sendmessage_contract(tmp_path, teams):
    r = _run(tmp_path, SOLO, teams)
    assert r.returncode == 2 and 'SendMessage with to: "main"' in r.stderr
    assert _run(tmp_path, FULL, teams).returncode == 0
    assert _run(tmp_path, NONE, teams).returncode == 2


@pytest.mark.parametrize("teams", [None, "", "0", "false", "off", "no", "2"])
def test_teams_off_needs_only_a_final_message_line(tmp_path, teams):
    assert _run(tmp_path, SOLO, teams).returncode == 0
    assert _run(tmp_path, FULL, teams).returncode == 0      # the old contract still passes
    r = _run(tmp_path, NONE, teams)
    assert r.returncode == 2 and "final message" in r.stderr and "teams are off" in r.stderr


@pytest.mark.parametrize("teams", ["1", "0"])
def test_no_report_needed_opts_out(tmp_path, teams):
    assert _run(tmp_path, "fire and forget NO-REPORT-NEEDED", teams).returncode == 0


def test_other_tools_and_missing_prompt_pass(tmp_path):
    assert _run(tmp_path, NONE, "1", tool="Bash").returncode == 0
    r = _run(tmp_path, None, "1", raw=json.dumps({"tool_name": "Agent", "tool_input": {}}))
    assert r.returncode == 0 and _rows(tmp_path) == []


def test_malformed_payload_fails_open_and_is_logged(tmp_path):
    r = _run(tmp_path, None, "1", raw="{not json")
    assert r.returncode == 0 and r.stdout == ""
    row = _rows(tmp_path)[-1]
    assert row["hook"] == "subagent-delivery-gate.py" and row["stderr_tail"].startswith("parse hook input: ")


def test_a_passing_dispatch_logs_nothing(tmp_path):
    assert _run(tmp_path, FULL, "1").returncode == 0
    assert _run(tmp_path, SOLO, "0").returncode == 0
    assert _rows(tmp_path) == []
