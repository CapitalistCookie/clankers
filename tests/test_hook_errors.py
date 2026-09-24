"""Fail-visible hooks (design audit 2026-09-24, proposal 6).

Every hook that sync distributes carries the same hook-error block (bash or
python, byte for byte). A failure the hook swallows appends one JSON row
{ts, hook, session_id, cwd, rc, stderr_tail} to
$CLANKER_DATA/raw/health/hook-errors-<UTC day>.jsonl, the contract that
`clanker doctor --harness` reads. The hook stays fail-open: same exit code,
nothing extra on stdout.

Hermetic: CLANKER_DATA and HOME point into tmp_path; failures are made on
purpose with PATH shims and scratch copies, never in the repo's hooks."""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(ROOT, "hooks")
HARNESS = os.path.join(HOOKS, "harness")
sys.path.insert(0, os.path.join(ROOT, "lib"))
import synccmd  # noqa: E402

KEYS = {"ts", "hook", "session_id", "cwd", "rc", "stderr_tail"}
BASH_START, PY_START = ("# ---- hook-error log: the same block in every clanker bash hook",
                        "# ---- hook-error log: the same block in every clanker python hook")
END = "# ---- end of hook-error log"


def distributed_hooks():
    """Every file sync installs that is a hook (the harness README is a document)."""
    out = [os.path.join(HOOKS, n) for n in synccmd.REPO_RUN]
    out.append(os.path.join(HOOKS, "context-gauge.py"))
    for n in sorted(os.listdir(HARNESS)):
        p = os.path.join(HARNESS, n)
        if os.path.isfile(p) and n.endswith((".sh", ".py")):
            out.append(p)
    return out


def blocks(text, start):
    found, i = [], 0
    while True:
        a = text.find(start, i)
        if a < 0:
            return found
        b = text.index(END, a)
        found.append(text[a:text.index("\n", b) + 1])
        i = b


def test_every_distributed_hook_carries_the_same_block():
    bash_blocks, py_blocks = set(), set()
    for p in distributed_hooks():
        text = open(p, encoding="utf-8").read()
        b, y = blocks(text, BASH_START), blocks(text, PY_START)
        assert b or y, f"{os.path.basename(p)} has no hook-error block"
        if p.endswith(".py"):
            assert y and not b, p
        bash_blocks.update(b)
        py_blocks.update(y)
    assert len(bash_blocks) == 1, "the bash blocks differ between hooks"
    assert len(py_blocks) == 1, "the python blocks differ between hooks"
    assert "/raw/health/hook-errors-" in next(iter(bash_blocks))
    assert '"raw", "health"' in next(iter(py_blocks))


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_ENTRYPOINT", "CLANKER_INJECT_NESTED", "CLAUDE_PROJECT_DIR",
                        "CLAUDE_ENV_FILE", "CLANKER_REGISTRY", "CLANKER_PROJECT_ROOTS")}
    env.update(HOME=str(tmp_path / "home"), CLANKER_DATA=str(tmp_path / "data"),
               CLANKER_REGISTRY=str(tmp_path / "registry.yaml"),
               CLANKER_PROJECT_ROOTS=str(tmp_path / "roots"), CLANKER_HARNESS_ENV="/dev/null")
    (tmp_path / "home" / ".claude").mkdir(parents=True, exist_ok=True)   # every install has it
    env.update(extra)
    return env


def _rows(tmp_path):
    d = tmp_path / "data" / "raw" / "health"
    rows = []
    for f in sorted(d.glob("hook-errors-*.jsonl")) if d.is_dir() else []:
        rows += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    for r in rows:
        assert set(r) == KEYS, r
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", r["ts"]), r
        assert isinstance(r["rc"], int) and len(r["stderr_tail"]) <= 300, r
    return rows


def _shim(tmp_path, name, body):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/usr/bin/env bash\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(d) + os.pathsep + os.environ["PATH"]


def _run(cmd, env, payload, cwd=None):
    return subprocess.run(cmd, input=payload, capture_output=True, text=True, env=env,
                          timeout=60, cwd=cwd)


def test_session_start_python_crash_is_logged_and_the_hook_still_exits_0(tmp_path):
    sid = f"he-{uuid.uuid4().hex[:8]}"
    env = _env(tmp_path, PATH=_shim(tmp_path, "python3",
                                    'cat >/dev/null; echo "SyntaxError: boom on purpose" >&2; exit 3'))
    r = _run(["bash", os.path.join(HOOKS, "session-start.sh")], env,
             json.dumps({"session_id": sid, "cwd": "/w/\"odd\" dir", "source": "startup"}))
    assert r.returncode == 0 and r.stdout == ""
    rows = _rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["hook"] == "session-start.sh" and row["rc"] == 3
    assert row["session_id"] == sid and row["cwd"] == '/w/"odd" dir'
    assert row["stderr_tail"] == "brief (python): SyntaxError: boom on purpose"


def test_session_end_pipeline_failure_is_logged(tmp_path):
    sid = f"he-{uuid.uuid4().hex[:8]}"
    tp = tmp_path / f"{sid}.jsonl"
    tp.write_text(json.dumps({"type": "user", "timestamp": "2026-09-24T00:00:00Z",
                              "message": {"role": "user", "content": "hi"}}) + "\n")
    env = _env(tmp_path, PATH=_shim(tmp_path, "flock", 'cat >/dev/null; echo "flock: lock broke" >&2; exit 4'))
    r = _run(["bash", os.path.join(HOOKS, "session-end.sh")], env,
             json.dumps({"session_id": sid, "transcript_path": str(tp), "cwd": str(tmp_path)}))
    assert r.returncode == 0
    row = [x for x in _rows(tmp_path) if x["hook"] == "session-end.sh"][-1]
    assert row["rc"] == 4 and row["session_id"] == sid
    assert row["stderr_tail"].startswith("metrics (python | flock tee): ")
    assert "flock: lock broke" in row["stderr_tail"]


def test_gauge_python_failure_is_logged_from_a_scratch_copy(tmp_path):
    """The installed layout: wrapper and gauge side by side. The gauge here is
    broken on purpose (a syntax error, as after a bad edit)."""
    d = tmp_path / "hooks"
    d.mkdir()
    shutil.copy(os.path.join(HARNESS, "context-gauge.sh"), d)
    (d / "context-gauge.py").write_text("def broken(:\n")
    sid = f"he-{uuid.uuid4().hex[:8]}"
    tp = tmp_path / f"{sid}.jsonl"
    tp.write_text("{}\n")
    # compact JSON, as Claude Code sends it: the wrapper's pure-bash field match needs it
    r = _run(["bash", str(d / "context-gauge.sh")], _env(tmp_path),
             json.dumps({"session_id": sid, "transcript_path": str(tp), "cwd": "/w"},
                        separators=(",", ":")))
    assert r.returncode == 0 and r.stdout == ""
    row = _rows(tmp_path)[-1]
    assert row["hook"] == "context-gauge.sh" and row["rc"] == 1 and row["session_id"] == sid
    assert row["stderr_tail"].startswith("context-gauge.py: ") and "SyntaxError" in row["stderr_tail"]


def test_dispatcher_gate_error_is_logged_and_fails_open(tmp_path):
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy(os.path.join(HARNESS, "pretooluse-bash-dispatch.sh"), hooks)
    (hooks / "ssh-tunnel-port-guard.py").write_text(
        "import sys\nsys.stdin.read()\nsys.stderr.write('guard exploded on purpose')\nsys.exit(1)\n")
    sid = f"he-{uuid.uuid4().hex[:8]}"
    r = _run(["bash", str(hooks / "pretooluse-bash-dispatch.sh")], _env(tmp_path),
             json.dumps({"session_id": sid, "cwd": "/w", "tool_name": "Bash",
                         "tool_input": {"command": "ssh -L 18999:localhost:80 host"}}))
    assert r.returncode == 0 and r.stdout == ""               # fail-open: the call proceeds
    row = _rows(tmp_path)[-1]
    assert row["hook"] == "pretooluse-bash-dispatch.sh" and row["rc"] == 1
    assert row["session_id"] == sid
    assert row["stderr_tail"] == ("gate ssh-tunnel-port-guard: error, skipped (fail-open): "
                                  "guard exploded on purpose")


def test_python_gate_malformed_payload_is_logged(tmp_path):
    r = _run([sys.executable, os.path.join(HARNESS, "task-payload-gate.py")], _env(tmp_path),
             "{not json")
    assert r.returncode == 0 and r.stdout == ""
    row = _rows(tmp_path)[-1]
    assert row["hook"] == "task-payload-gate.py" and row["rc"] == 1
    assert row["stderr_tail"].startswith("parse hook input: ") and "JSONDecodeError" in row["stderr_tail"]


def test_registry_lookup_failure_is_logged_not_silent(tmp_path):
    """pre-commit-verification's registry lookup needs PyYAML; a failing
    import turned the research prompt off with no trace."""
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    (home / "projects" / ".clanker.yaml").write_text("projects: {}\n")
    shim = _shim(tmp_path, "python3",
                 'cat >/dev/null; echo "ModuleNotFoundError: No module named yaml" >&2; exit 3')
    r = _run(["bash", os.path.join(HARNESS, "pre-commit-verification.sh")],
             _env(tmp_path, PATH=shim),
             json.dumps({"session_id": "s1", "cwd": "/w",
                         "tool_input": {"command": "git commit -m 'backtest research'"}}),
             cwd=str(tmp_path))
    assert r.returncode == 0 and r.stdout == ""
    row = _rows(tmp_path)[-1]
    assert row["hook"] == "pre-commit-verification.sh" and row["rc"] == 3
    assert row["stderr_tail"] == ("registry lookup (python): "
                                  "ModuleNotFoundError: No module named yaml")


NORMAL = {
    "session_id": "normal-1", "cwd": "/tmp", "transcript_path": "",
    "hook_event_name": "PostToolUse", "tool_name": "Bash",
    "tool_input": {"command": "ls -la"}, "tool_response": {"stdout": "x"},
}


def test_normal_payloads_log_nothing(tmp_path):
    """No false positives: each hook, fed an ordinary payload, writes no row."""
    tp = tmp_path / "normal.jsonl"
    tp.write_text(json.dumps({"type": "assistant", "timestamp": "2026-09-24T00:00:00Z",
                              "message": {"id": "m1", "model": "claude-sonnet-5",
                                          "usage": {"input_tokens": 1, "output_tokens": 1},
                                          "content": [{"type": "text", "text": "ok"}]}}) + "\n")
    base = dict(NORMAL, transcript_path=str(tp), cwd=str(tmp_path))
    env = _env(tmp_path)
    cases = [
        ("session-start.sh", dict(base, source="startup")),
        ("session-end.sh", dict(base, reason="other", session_id=f"n-{uuid.uuid4().hex[:8]}")),
        ("skill-tracker.sh", dict(base, tool_name="Skill", tool_input={"skill": "x"})),
        ("subagent-tier-gate.py", dict(base, tool_name="Agent", tool_input={"model": "opus"})),
        ("harness/pretooluse-bash-dispatch.sh", base),
        ("harness/iron-law-check.sh", dict(base, last_assistant_message="looked at it")),
        ("harness/closure-claim-verifier.sh", base),
        ("harness/governance-gates-autorun.sh", dict(base, tool_input={"file_path": "/x/a.md"})),
        ("harness/post-build-review-reminder.sh", base),
        ("harness/pre-commit-verification.sh", base),
        ("harness/gpu-vm-guard.py", base),
        ("harness/ssh-tunnel-port-guard.py", base),
        ("harness/task-payload-gate.py", dict(base, tool_name="TaskCreate",
                                               tool_input={"subject": "s", "description": "d"})),
    ]
    for rel, payload in cases:
        path = os.path.join(HOOKS, rel)
        cmd = ["bash", path] if rel.endswith(".sh") else [sys.executable, path]
        r = _run(cmd, env, json.dumps(payload), cwd=str(tmp_path))
        assert r.returncode == 0, (rel, r.stderr)
    r = _run([sys.executable, os.path.join(HARNESS, "last-assistant-msg.py"), str(tp)], env, "")
    assert r.stdout.strip() == "ok"
    # the gauge, in the installed layout, on its first (python) call
    gdir = tmp_path / "gauge"
    gdir.mkdir()
    shutil.copy(os.path.join(HARNESS, "context-gauge.sh"), gdir)
    shutil.copy(os.path.join(HOOKS, "context-gauge.py"), gdir)
    key = f"he-normal-{uuid.uuid4().hex[:8]}"
    gtp = tmp_path / f"{key}.jsonl"
    shutil.copy(tp, gtp)
    try:
        r = _run(["bash", str(gdir / "context-gauge.sh")], env,
                 json.dumps(dict(base, transcript_path=str(gtp)), separators=(",", ":")))
        assert r.returncode == 0 and "CONTEXT GAUGE" in r.stdout, r.stderr
    finally:
        for f in ("fast", "grounded"):                  # the markers this run created
            p = f"/tmp/cc-ctxgauge-{f}-tp-{key}"
            if os.path.exists(p):
                os.remove(p)
    assert _rows(tmp_path) == []
