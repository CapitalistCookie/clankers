"""hooks/harness/context-gauge.sh in nested runs (design audit 2026-09-24,
proposal 2): a scripted `claude -p` carries CLAUDE_CODE_ENTRYPOINT=sdk-cli, and
the gauge exits before it reads stdin. CLANKER_INJECT_NESTED=1 keeps it on.
`--selftest` clears both variables, so a nested caller still gets 16/16.
(That is checked with a stub selftest: the real one keys fixed /tmp marker
names, so it races any concurrent run of itself, such as ci/full's.)

The gauge keys /tmp markers on the transcript name; each test uses a unique
name and removes only the markers it created."""
import json
import os
import shutil
import subprocess
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, "hooks", "harness")


@pytest.fixture
def gauge(tmp_path):
    """The installed layout: wrapper, gauge and selftest side by side."""
    d = tmp_path / "hooks"
    (d / "tests").mkdir(parents=True)
    shutil.copy(os.path.join(HARNESS, "context-gauge.sh"), d)
    shutil.copy(os.path.join(ROOT, "hooks", "context-gauge.py"), d)
    shutil.copy(os.path.join(HARNESS, "tests", "test_context_gauge.sh"), d / "tests")
    key = f"ccgnested-{uuid.uuid4().hex[:8]}"
    tp = tmp_path / f"{key}.jsonl"
    tp.write_text(json.dumps({"type": "assistant", "message": {
        "role": "assistant", "model": "claude-fable-5[1m]",
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 199900}}}) + "\n")
    payload = json.dumps({"session_id": key, "transcript_path": str(tp), "cwd": str(tmp_path)},
                         separators=(",", ":"))
    markers = [f"/tmp/cc-ctxgauge-{m}-tp-{key}" for m in ("fast", "grounded")]
    yield d, payload, markers, tmp_path
    for m in markers:
        if os.path.exists(m):
            os.remove(m)


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_ENTRYPOINT", "CLANKER_INJECT_NESTED")}
    env["CLANKER_DATA"] = str(tmp_path / "data")
    env.update(extra)
    return env


def test_nested_run_exits_before_python(gauge):
    d, payload, markers, tmp_path = gauge
    probe = tmp_path / "py-ran"
    r = subprocess.run(["bash", str(d / "context-gauge.sh")], input=payload, text=True,
                       capture_output=True, timeout=30,
                       env=_env(tmp_path, CLAUDE_CODE_ENTRYPOINT="sdk-cli", CCG_PROBE=str(probe)))
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert not probe.exists()                                 # python never started
    assert not any(os.path.exists(m) for m in markers)        # nothing measured, nothing cached


@pytest.mark.parametrize("extra", [{"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "CLANKER_INJECT_NESTED": "1"},
                                   {"CLAUDE_CODE_ENTRYPOINT": "cli"}, {}])
def test_interactive_or_injected_run_still_measures(gauge, extra):
    d, payload, markers, tmp_path = gauge
    r = subprocess.run(["bash", str(d / "context-gauge.sh")], input=payload, text=True,
                       capture_output=True, timeout=30, env=_env(tmp_path, **extra))
    assert r.returncode == 0
    assert "first reading" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def test_selftest_branch_clears_the_nested_env(gauge):
    d, _payload, _markers, tmp_path = gauge
    (d / "tests" / "test_context_gauge.sh").write_text(
        'echo "entry=${CLAUDE_CODE_ENTRYPOINT:-unset} inject=${CLANKER_INJECT_NESTED:-unset}"\n')
    r = subprocess.run(["bash", str(d / "context-gauge.sh"), "--selftest"], text=True,
                       capture_output=True, timeout=30,
                       env=_env(tmp_path, CLAUDE_CODE_ENTRYPOINT="sdk-cli", CLANKER_INJECT_NESTED="1"))
    assert r.returncode == 0 and r.stdout == "entry=unset inject=unset\n", r.stdout + r.stderr
