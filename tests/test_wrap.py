"""clanker wrap: the scripted-claude launcher (lib/wrap.py) and its CLI verb.

A fake `claude` (CLANKER_WRAP_CLAUDE) records the argv, cwd and environment it
was given and prints a Claude-Code-shaped result, so every flag, the output
contract, the exit codes, the timeout kill and the telemetry row are checked
without calling a model. CLANKER_DATA points at a per-test dir (repo law 1).
"""

import json
import os
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CLANKER = os.path.join(REPO, "bin", "clanker")
sys.path.insert(0, os.path.join(REPO, "lib"))

import wrap  # noqa: E402

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
log = os.environ.get("FAKE_LOG")
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
if log:
    with open(log, "w") as f:
        json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": stdin, "pid": os.getpid(),
                   "env": {k: v for k, v in os.environ.items()
                           if k.startswith(("CLAUDE", "CLANKER_WRAP", "DISABLE_", "FAKE_"))}}, f)
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "sleep":
    time.sleep(60)
res = {"type": "result", "subtype": "success", "is_error": mode == "error", "num_turns": 1,
       "result": "OK" if mode != "error" else "You've hit your session limit", "session_id": "sid-123",
       "total_cost_usd": 0.0123, "duration_api_ms": 900,
       "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200,
                 "output_tokens": 5},
       "modelUsage": {"claude-haiku-4-5-20251001": {"costUSD": 0.0123}},
       "permission_denials": []}
fmt = sys.argv[sys.argv.index("--output-format") + 1] if "--output-format" in sys.argv else "text"
if fmt == "stream-json":
    print(json.dumps({"type": "system", "subtype": "init", "tools": []}), flush=True)
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "OK"}]}}), flush=True)
    print(json.dumps(res), flush=True)
else:
    print(json.dumps(res))
sys.exit(int(os.environ.get("FAKE_RC", "0")))
'''


@pytest.fixture
def fake(tmp_path):
    path = tmp_path / "fake_claude"
    path.write_text(FAKE)
    path.chmod(0o755)
    data = tmp_path / "data"
    data.mkdir()
    log = tmp_path / "fake.json"
    env = {k: v for k, v in os.environ.items()}
    env.update({"CLANKER_WRAP_CLAUDE": str(path), "CLANKER_DATA": str(data), "FAKE_LOG": str(log),
                "CLAUDECODE": "1", "CLAUDE_CODE_EFFORT_LEVEL": "max",
                "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/x.sock", "CLAUDE_CODE_OAUTH_TOKEN": "tok",
                "CLAUDE_EFFORT": "max"})
    return {"env": env, "log": log, "data": data, "tmp": tmp_path}


def run_cli(args, env, input=None, timeout=30):
    return subprocess.run([CLANKER, "wrap", *args], env=env, capture_output=True, text=True,
                          input=input, timeout=timeout)


def rows(data):
    out = []
    d = data / "raw" / "sessions"
    for f in sorted(d.glob("*.jsonl")) if d.exists() else []:
        out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    return out


# ─── pure functions ──────────────────────────────────────────────────────────

def test_split_claude_args_reads_owned_options_and_keeps_the_rest():
    owned, rest = wrap.split_claude_args(
        ["-p", "--model=m1", "--effort", "low", "--tools", "", "--output-format", "json",
         "--strict-mcp-config", "prompt", "--", "--model", "literal"])
    assert owned == {"-p": True, "--model": "m1", "--effort": "low", "--output-format": "json",
                     "--strict-mcp-config": True}
    assert rest == ["--tools", "", "prompt", "--", "--model", "literal"]


def test_split_claude_args_rejects_a_duplicate_pin():
    with pytest.raises(wrap.UsageError):
        wrap.split_claude_args(["--model", "a", "--model", "b"])


def plan(**kw):
    base = dict(tokens=["-p", "hi"], model="m", effort="low", max_cost=None, max_turns=None,
                caller="c", cwd=None, allow_user_layer=False, base_env={"PATH": "/usr/bin"})
    base.update(kw)
    return wrap.build_plan(**base)


def test_build_plan_isolates_and_pins():
    p = plan()
    a = p["argv"]
    assert a[1] == "-p"
    assert a[a.index("--setting-sources") + 1] == "project,local"
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in a
    assert a[a.index("--model") + 1] == "m" and a[a.index("--effort") + 1] == "low"
    assert a[a.index("--max-budget-usd") + 1] == "5"
    assert a[a.index("--output-format") + 1] == "json" and p["output_format"] == "text"
    assert json.loads(a[a.index("--settings") + 1]) == {"attribution": {"commit": ""}}
    assert a[-1] == "hi" and a.count("-p") == 1


def test_build_plan_requires_model_and_effort():
    with pytest.raises(wrap.UsageError, match="--model is required"):
        plan(model=None)
    with pytest.raises(wrap.UsageError, match="--effort is required"):
        plan(effort=None)
    with pytest.raises(wrap.UsageError, match="must be one of"):
        plan(effort=None, tokens=["-p", "--effort", "huge", "x"])


def test_build_plan_accepts_pins_inside_the_claude_args_but_not_twice():
    p = plan(model=None, effort=None, tokens=["claude", "-p", "--model", "m2", "--effort", "high", "x"])
    assert p["model"] == "m2" and p["effort"] == "high"
    with pytest.raises(wrap.UsageError, match="give it once"):
        plan(tokens=["-p", "--model", "m2", "x"])


def test_build_plan_user_layer_flag_and_setting_sources_guard():
    assert "--setting-sources" not in plan(allow_user_layer=True)["argv"]
    with pytest.raises(wrap.UsageError, match="allow-user-layer"):
        plan(tokens=["-p", "--setting-sources", "user", "x"])


def test_build_plan_cost_cap_default_env_and_zero():
    assert plan(base_env={"CLANKER_WRAP_MAX_COST": "0.5"})["argv"].count("0.5") == 1
    assert "--max-budget-usd" not in plan(max_cost=0.0)["argv"]
    p = plan(model=None, tokens=["-p", "--model", "m", "--max-budget-usd", "8", "x"])
    assert p["argv"][p["argv"].index("--max-budget-usd") + 1] == "8"
    with pytest.raises(wrap.UsageError):
        plan(max_cost=-1.0)


def test_build_plan_default_cwd_is_the_per_caller_run_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLANKER_DATA", str(tmp_path))
    assert plan(caller="my job/../x")["cwd"] == str(tmp_path / "run" / "my-job-..-x".strip(".-"))
    assert plan(cwd=str(tmp_path / "w"))["cwd"] == str(tmp_path / "w")


def test_child_env_strips_the_parent_session_and_keeps_auth():
    env, removed = wrap.child_env({"CLAUDECODE": "1", "CLAUDE_CODE_EFFORT_LEVEL": "max",
                                   "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_EFFORT": "max",
                                   "CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CODE_USE_BEDROCK": "1",
                                   "CLAUDE_CONFIG_DIR": "/c", "HOME": "/h"}, "job")
    assert removed == ["CLAUDECODE", "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "t" and env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["CLAUDE_CONFIG_DIR"] == "/c" and env["HOME"] == "/h"
    assert env["CLANKER_WRAP"] == "1" and env["CLANKER_WRAP_CALLER"] == "job"
    for k, v in wrap.POLICY_ENV.items():
        assert env[k] == v


def test_merged_settings_inline_file_and_invalid(tmp_path):
    assert json.loads(wrap.merged_settings('{"x": 1}')) == {"attribution": {"commit": ""}, "x": 1}
    f = tmp_path / "s.json"
    f.write_text('{"attribution": {"commit": "mine"}}')
    assert json.loads(wrap.merged_settings(str(f))) == {"attribution": {"commit": "mine"}}
    with pytest.raises(wrap.UsageError):
        wrap.merged_settings("/nonexistent/settings.json")


def test_first_turn_context_only_for_single_turn_runs():
    r = {"num_turns": 1, "usage": {"input_tokens": 1, "cache_read_input_tokens": 2,
                                   "cache_creation_input_tokens": 3}}
    assert wrap.first_turn_context(r) == 6
    r["num_turns"] = 2
    assert wrap.first_turn_context(r) is None


# ─── the CLI verb, end to end against the fake ──────────────────────────────

def test_text_mode_prints_result_isolates_and_logs_a_nested_row(fake):
    r = run_cli(["--caller", "cron-job", "--model", "claude-haiku-4-5-20251001", "--effort", "low",
                 "--", "claude", "-p", "Reply with exactly: OK"], fake["env"])
    assert r.returncode == 0, r.stderr
    assert r.stdout == "OK\n"
    got = json.loads(fake["log"].read_text())
    assert got["cwd"] == str(fake["data"] / "run" / "cron-job")
    assert "--setting-sources" in got["argv"] and got["argv"][-1] == "Reply with exactly: OK"
    env = got["env"]
    assert env["CLANKER_WRAP"] == "1" and env["CLANKER_WRAP_CALLER"] == "cron-job"
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_EFFORT_LEVEL" not in env
    assert "CLAUDE_CODE_MESSAGING_SOCKET" not in env and "CLAUDE_EFFORT" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1" and env["CLAUDE_CODE_DISABLE_WORKFLOWS"] == "1"
    [row] = rows(fake["data"])
    assert row["kind"] == "nested" and row["nested"] is True and row["wrap"] is True
    assert row["caller"] == "cron-job" and row["session_id"] == "sid-123"
    assert row["total_cost_usd"] == 0.0123 and row["estimated_cost_usd"] == 0.0123
    assert row["first_call_ctx"] == 1210 and row["outcome"] == "ok" and row["exit_code"] == 0
    assert row["tokens"] == {"input": 10, "output": 5, "cache_read": 1000, "cache_create": 200}
    assert "caller=cron-job" in r.stderr and "cost=$0.0123" in r.stderr


def test_json_mode_prints_claude_json_verbatim_and_reads_stdin(fake):
    r = run_cli(["--caller", "b", "--model", "m", "--effort", "medium", "--",
                 "claude", "-p", "--output-format", "json", "--tools", ""], fake["env"],
                input="prompt on stdin")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["result"] == "OK"
    got = json.loads(fake["log"].read_text())
    assert got["stdin"] == "prompt on stdin"
    assert got["argv"].count("--output-format") == 1 and "--tools" in got["argv"]


def test_stream_json_is_forwarded_line_by_line(fake):
    r = run_cli(["--caller", "s", "--model", "m", "--effort", "low", "--",
                 "-p", "--output-format", "stream-json", "--verbose", "x"], fake["env"])
    assert r.returncode == 0, r.stderr
    lines = [json.loads(l) for l in r.stdout.splitlines()]
    assert [l["type"] for l in lines] == ["system", "assistant", "result"]
    assert rows(fake["data"])[0]["total_cost_usd"] == 0.0123


def test_is_error_result_exits_nonzero_and_is_logged_as_error(fake):
    env = dict(fake["env"], FAKE_MODE="error")
    r = run_cli(["--caller", "e", "--model", "m", "--effort", "low", "--", "-p", "x"], env)
    assert r.returncode == 1
    assert "session limit" in r.stdout
    assert rows(fake["data"])[0]["outcome"] == "error"


def test_claude_exit_code_passes_through(fake):
    env = dict(fake["env"], FAKE_RC="3")
    r = run_cli(["--caller", "e", "--model", "m", "--effort", "low", "--", "-p", "x"], env)
    assert r.returncode == 3


def test_timeout_kills_claude_and_exits_124(fake):
    env = dict(fake["env"], FAKE_MODE="sleep")
    t0 = time.monotonic()
    r = run_cli(["--caller", "t", "--model", "m", "--effort", "low", "--timeout", "1", "--", "-p", "x"], env)
    assert r.returncode == wrap.TIMEOUT_RC, r.stderr
    assert time.monotonic() - t0 < 15
    assert "timeout" in r.stderr
    pid = json.loads(fake["log"].read_text())["pid"]
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    [row] = rows(fake["data"])
    assert row["outcome"] == "timeout" and row["exit_code"] == 124


def test_usage_errors_exit_2_and_run_nothing(fake):
    r = run_cli(["--caller", "u", "--", "-p", "x"], fake["env"])
    assert r.returncode == 2 and "--model is required" in r.stderr
    assert not fake["log"].exists() and rows(fake["data"]) == []


def test_missing_claude_exits_127(fake):
    env = dict(fake["env"], CLANKER_WRAP_CLAUDE="/nonexistent/claude")
    r = run_cli(["--caller", "m", "--model", "m", "--effort", "low", "--", "-p", "x"], env)
    assert r.returncode == 127 and "claude not found" in r.stderr


def test_dry_run_prints_the_plan_and_runs_nothing(fake):
    r = run_cli(["--caller", "d", "--model", "m", "--effort", "low", "--dry-run", "--", "-p", "x"],
                fake["env"])
    assert r.returncode == 0
    d = json.loads(r.stdout)
    assert d["argv"][0] == fake["env"]["CLANKER_WRAP_CLAUDE"] and "CLAUDECODE" in d["env_removed"]
    assert not fake["log"].exists()


def test_generic_command_returns_its_own_exit_code(fake):
    assert run_cli(["true"], fake["env"]).returncode == 0
    assert run_cli(["sh", "-c", "exit 3"], fake["env"]).returncode == 3
    r = run_cli(["--timeout", "5", "true"], fake["env"])
    assert r.returncode == 2 and "claude calls only" in r.stderr
