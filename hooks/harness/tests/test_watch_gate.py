"""Self-test of watch-gate.py (global rule 25, orchestrator wake economy).

Run: python3 -u tests/test_watch_gate.py      (prints "watch-gate selftest: n/n PASS")
 or: python3 -u -m pytest tests/test_watch_gate.py -q
The file finds the hook one directory up, so the same file works in the clanker
repo (hooks/harness/tests/) and installed (~/.claude/hooks/tests/).
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

GATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "watch-gate.py")


def _load():
    spec = importlib.util.spec_from_file_location("watch_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


G = _load()


def _env(tmp, **kw):
    e = {"CLAUDE_SCRATCHPAD_DIR": tmp}
    e.update(kw)
    return e


def _mon(cmd, sid="s1", timeout_ms=600000, **kw):
    d = {"session_id": sid, "tool_name": "Monitor",
         "tool_input": {"command": cmd, "description": "t", "timeout_ms": timeout_ms}}
    d.update(kw)
    return d


def _cron(expr, recurring=None):
    ti = {"cron": expr, "prompt": "p"}
    if recurring is not None:
        ti["recurring"] = recurring
    return {"session_id": "s1", "tool_name": "CronCreate", "tool_input": ti}


SINGLE_SHOT = ("until [ -f /tmp/r/report.md ] || ! kill -0 4242 2>/dev/null; do sleep 20; done; "
               "echo \"terminal: $(ls /tmp/r)\"; exit 0")


def _tmp():
    return tempfile.mkdtemp(prefix="watch-gate-test-")


def test_a_refuses_endless_while_true():
    t = _tmp()
    try:
        code, msg, _, _ = G.check(_mon("while true; do ci/status; sleep 30; done"), _env(t))
        assert code == 2 and "rule 25" in msg
    finally:
        shutil.rmtree(t)


def test_a_refuses_tail_follow_without_exit():
    t = _tmp()
    try:
        code, _, _, _ = G.check(_mon("tail -F /x/run.log | grep --line-buffered -E 'DONE|FAIL'"), _env(t))
        assert code == 2
        code, _, _, _ = G.check(_mon("inotifywait -m -e create /x/out"), _env(t))
        assert code == 2
    finally:
        shutil.rmtree(t)


def test_allows_single_shot_until_exit_and_stamps():
    t = _tmp()
    try:
        data = _mon(SINGLE_SHOT)
        code, _, warn, d = G.check(data, _env(t), now=1000.0)
        assert code == 0 and warn is None and d
        G.write_stamp(d, data["tool_input"], 1000.0)
        assert len(G.live_stamps(d, 1001.0)) == 1
    finally:
        shutil.rmtree(t)


def test_b_refuses_a_second_live_monitor_and_names_the_reset():
    t = _tmp()
    try:
        data = _mon(SINGLE_SHOT)
        _, _, _, d = G.check(data, _env(t), now=1000.0)
        G.write_stamp(d, data["tool_input"], 1000.0)
        code, msg, _, _ = G.check(_mon(SINGLE_SHOT), _env(t), now=1100.0)
        assert code == 2 and "--reset s1" in msg and "WATCH_GATE_RESET=1" in msg
    finally:
        shutil.rmtree(t)


def test_b_expired_stamp_is_ignored():
    t = _tmp()
    try:
        data = _mon(SINGLE_SHOT, timeout_ms=300000)
        _, _, _, d = G.check(data, _env(t), now=1000.0)
        G.write_stamp(d, data["tool_input"], 1000.0)
        code, _, _, _ = G.check(_mon(SINGLE_SHOT), _env(t), now=1000.0 + 301)
        assert code == 0
        code, _, _, _ = G.check(_mon(SINGLE_SHOT), _env(t), now=1000.0 + 299)
        assert code == 2
    finally:
        shutil.rmtree(t)


def test_b_timeout_is_capped_at_the_monitor_cap():
    t = _tmp()
    try:
        data = _mon(SINGLE_SHOT, timeout_ms=3600000)
        _, _, _, d = G.check(data, _env(t), now=0.0)
        G.write_stamp(d, data["tool_input"], 0.0)
        assert G.check(_mon(SINGLE_SHOT), _env(t), now=1801.0)[0] == 0
    finally:
        shutil.rmtree(t)


def test_b_reset_env_and_other_agent_are_not_blocked():
    t = _tmp()
    try:
        data = _mon(SINGLE_SHOT)
        _, _, _, d = G.check(data, _env(t), now=1000.0)
        G.write_stamp(d, data["tool_input"], 1000.0)
        assert G.check(_mon(SINGLE_SHOT, agent_id="a9"), _env(t), now=1010.0)[0] == 0
        assert G.check(_mon(SINGLE_SHOT), _env(t, WATCH_GATE_RESET="1"), now=1010.0)[0] == 0
        assert G.live_stamps(d, 1010.0) == []
    finally:
        shutil.rmtree(t)


def test_c_refuses_recurring_cron_under_30_min():
    assert G.check(_cron("*/5 * * * *"), {})[0] == 2
    assert G.check(_cron("7,27,47 * * * *", recurring=True), {})[0] == 2
    assert G.check(_cron("*/15 9-17 * * 1-5"), {})[0] == 2


def test_c_allows_one_shot_and_slow_recurring_cron():
    assert G.check(_cron("*/5 * * * *", recurring=False), {})[0] == 0
    assert G.check(_cron("17 3 26 9 *", recurring=False), {})[0] == 0
    assert G.check(_cron("7 * * * *"), {})[0] == 0
    assert G.check(_cron("7,37 * * * *"), {})[0] == 0
    assert G.cron_min_gap("@hourly") == 60


def test_d_state_emitter_is_a_warning_not_a_refusal():
    t = _tmp()
    try:
        cmd = "while true; do echo still running; [ -f /x/done ] && exit 0; sleep 60; done"
        code, _, warn, _ = G.check(_mon(cmd), _env(t))
        assert code == 0 and warn and "rule 25" in warn
    finally:
        shutil.rmtree(t)


def test_other_tools_and_off_switch_pass():
    assert G.check({"tool_name": "Bash", "tool_input": {"command": "tail -f x"}}, {})[0] == 0
    assert G.check(_mon("while true; do :; done"), {"WATCH_GATE_OFF": "1"})[0] == 0


def test_end_to_end_process_stamps_refuses_resets_and_fails_open():
    t = _tmp()
    try:
        env = dict(os.environ, CLAUDE_SCRATCHPAD_DIR=t, CLANKER_DATA=os.path.join(t, "cd"))
        env.pop("WATCH_GATE_OFF", None)
        env.pop("WATCH_GATE_RESET", None)

        def run(payload, *args):
            return subprocess.run([sys.executable, GATE] + list(args), input=payload,
                                  capture_output=True, text=True, env=env, timeout=30)
        p = json.dumps(_mon(SINGLE_SHOT, sid="e2e"))
        assert run(p).returncode == 0
        r = run(p)
        assert r.returncode == 2 and "rule 25" in r.stderr
        assert run("", "--reset", "e2e").returncode == 0
        assert run(p).returncode == 0
        warn = run(json.dumps(_mon("sleep 1; echo heartbeat; exit 0", sid="w")))
        assert warn.returncode == 0 and "additionalContext" in warn.stdout
        assert run("{not json").returncode == 0          # fail open
    finally:
        shutil.rmtree(t)


def _main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    fails = []
    for name, f in tests:
        try:
            f()
        except Exception as e:
            fails.append("%s: %s: %s" % (name, type(e).__name__, e))
    if not fails:
        print("watch-gate selftest: %d/%d PASS" % (len(tests), len(tests)))
        return 0
    print("watch-gate selftest: %d FAILURE(S) of %d" % (len(fails), len(tests)))
    for f in fails:
        print("  -", f)
    return 1


if __name__ == "__main__":
    t0 = time.time()
    rc = _main()
    print("wall %.2fs" % (time.time() - t0))
    sys.exit(rc)
