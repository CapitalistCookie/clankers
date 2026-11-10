"""Session account slots (docs/SESSION-ACCOUNTS.md): launch on a selected slot,
show the slot, and move a running session between slots.

End-to-end tests drive the REAL bin/clanker with three fakes (tests/fakes/):
a tmux that RUNS the launch lines it is sent (so the environment the launch line
builds is the one the process gets), a claude that writes a real registry record
and transcript from its own pid and environment, and a claude-acct with leases
on disk. Every slot dir, HOME and CLANKER_DATA is a tmp dir; no real tmux, claude
or account is touched (repo law 1).
"""
import asyncio
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKES = os.path.join(REPO, "tests", "fakes")
CLANKER = os.path.join(REPO, "bin", "clanker")
sys.path.insert(0, os.path.join(REPO, "lib"))
import accounts  # noqa: E402
import session_switch  # noqa: E402


def _row(slot, cfg, real, status="live", declared=None, plan="max", fh=0.1, sd=0.2, **kw):
    r = {"slot": slot, "status": status, "declared": declared or status, "plan": plan,
         "five_hour": fh, "seven_day": sd, "stale": False, "fence": None, "leases": 0,
         "caps": {"five_hour_stand_down": 0.8, "seven_day_stand_down": 0.98},
         "config_dir": cfg, "resolves_to": real, "resets_at": {}, "owner_note": "test",
         "figure_at": "2026-10-29T02:00:00Z", "figure_source": "mirror:clanker-statusline"}
    r.update(kw)
    return r


class Box:
    """A tmp box: HOME with ~/.claude (slot a1), slot a3 behind a stable link,
    a2 not logged in, a5 empty; fake tmux/claude/claude-acct on PATH."""

    def __init__(self, tmp):
        self.tmp = str(tmp)
        j = lambda *p: os.path.join(self.tmp, *p)
        self.home = j("home")
        self.a1 = j("home", ".claude")
        self.a3_real, self.a2_real = j("slots", "a3"), j("slots", "a2")
        for d in (self.a1, self.a3_real, self.a2_real, j("accounts"), j("acct"), j("tmux"), j("data"),
                  j("proj")):
            os.makedirs(d, exist_ok=True)
        os.symlink(self.a1, j("accounts", "a1"))
        os.symlink(self.a3_real, j("accounts", "a3"))
        os.symlink(self.a2_real, j("accounts", "a2"))
        self.cfg = {s: j("accounts", s) for s in ("a1", "a2", "a3")}
        self.rows = [
            _row("a1", self.cfg["a1"], self.a1),
            _row("a2", self.cfg["a2"], self.a2_real, status="needs-login", declared="live"),
            _row("a3", self.cfg["a3"], self.a3_real, fh=0.85, warn=["5h 85% >= stand-down 80%"]),
            _row("a5", j("accounts", "a5"), None, status="empty", plan=None, fh=None, sd=None),
        ]
        self.save_rows()
        self.proj = j("proj")
        reg = j("registry.yaml")
        with open(reg, "w") as f:
            f.write(f"projects:\n  bootproj:\n    archetype: tool\n    path: {self.proj}\n")
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("TMUX", "CLAUDE_CONFIG_DIR", "CLANKER_CLAUDE_SESSIONS_DIR",
                                 "CLANKER_CLAUDE_DIR", "CLANKER_SESSION")}
        self.env.update(HOME=self.home, PATH=FAKES + os.pathsep + os.environ["PATH"],
                        CLANKER_ACCOUNTS_CMD=os.path.join(FAKES, "claude-acct"),
                        FAKE_ACCT_DIR=j("acct"), FAKE_TMUX_DIR=j("tmux"), CLANKER_DATA=j("data"),
                        CLANKER_LAUNCH_TIMEOUT="15", CLANKER_REGISTRY=reg,
                        CLANKER_PROJECT_ROOTS=self.tmp, FAKE_CLAUDE_LIFETIME="60")

    def save_rows(self):
        with open(os.path.join(self.tmp, "acct", "slots.json"), "w") as f:
            json.dump(self.rows, f)

    def run(self, *args, env=None, timeout=90, cwd=None):
        """`clanker <args>` from inside the box's project (bootproj): every Claude
        session runs in a project (lib/session_flow.py)."""
        return subprocess.run([sys.executable, CLANKER, *args], capture_output=True, text=True,
                              env=dict(self.env, **(env or {})), timeout=timeout, stdin=subprocess.DEVNULL,
                              cwd=cwd or self.proj)

    def records(self, slot_dir):
        d = os.path.join(slot_dir, "sessions")
        out = []
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            rec = json.load(open(os.path.join(d, fn)))
            try:
                os.kill(rec["pid"], 0)
                out.append(rec)
            except OSError:
                pass
        return out

    def leases(self):
        p = os.path.join(self.tmp, "acct", "leases.json")
        return json.load(open(p)) if os.path.exists(p) else []

    def tmux_calls(self):
        p = os.path.join(self.tmp, "tmux", "calls.log")
        return open(p).read().splitlines() if os.path.exists(p) else []

    def kill_all(self):
        for d in (self.a1, self.a3_real, self.a2_real):
            for rec in self.records(d):
                try:
                    os.kill(rec["pid"], signal.SIGTERM)
                except OSError:
                    pass


def proc_env(pid):
    return dict(e.split("=", 1) for e in open(f"/proc/{pid}/environ").read().split("\0") if "=" in e)


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    yield b
    b.kill_all()


# ─── accounts.py ───────────────────────────────────────────────────────────

def test_slots_shape_usable_and_home(box, monkeypatch):
    monkeypatch.setenv("CLANKER_ACCOUNTS_CMD", box.env["CLANKER_ACCOUNTS_CMD"])
    monkeypatch.setenv("FAKE_ACCT_DIR", box.env["FAKE_ACCT_DIR"])
    monkeypatch.setenv("CLANKER_CLAUDE_DIR", box.a1)
    accounts.forget_cache()
    sl = {s["id"]: s for s in accounts.slots(online=True)}
    assert sl["a1"]["home"] and sl["a1"]["usable"] and not sl["a3"]["home"]
    assert not sl["a2"]["usable"] and "not logged in" in sl["a2"]["why"]
    assert not sl["a5"]["usable"]
    assert any("stand-down" in w for w in sl["a3"]["warnings"])    # policy is a warning, a3 stays usable
    assert sl["a3"]["usable"]
    assert accounts.launch_env(sl["a1"]) == "env -u CLAUDE_CONFIG_DIR"
    assert accounts.launch_env(sl["a3"]) == "env CLAUDE_CONFIG_DIR=" + box.cfg["a3"]
    assert accounts.slot_of_dir(None, list(sl.values()))["id"] == "a1"
    assert accounts.slot_of_dir(box.a3_real, list(sl.values()))["id"] == "a3"
    assert accounts.slot_of_dir("/elsewhere", list(sl.values())) is None


def test_resolve_never_picks(box, monkeypatch):
    monkeypatch.setenv("CLANKER_ACCOUNTS_CMD", box.env["CLANKER_ACCOUNTS_CMD"])
    monkeypatch.setenv("FAKE_ACCT_DIR", box.env["FAKE_ACCT_DIR"])
    accounts.forget_cache()
    s, err = accounts.resolve(None)
    assert s is None and "choose an account" in err and "a1" in err and "a3" in err
    s, err = accounts.resolve("a2")
    assert s is None and "not usable" in err
    s, err = accounts.resolve("a9")
    assert s is None and "unknown account" in err
    s, err = accounts.resolve("a3")
    assert err is None and s["id"] == "a3"


def test_single_slot_mode_needs_no_choice(monkeypatch):
    monkeypatch.setenv("CLANKER_ACCOUNTS_CMD", "none")
    accounts.forget_cache()
    s, err = accounts.resolve(None)
    assert err is None and s["id"] == accounts.SINGLE and accounts.launch_env(s) == ""
    assert accounts.resolve("a3")[0] is None


def test_server_env_strips_parent_session_identity(monkeypatch):
    import newsession
    for k, v in {"CLAUDE_CONFIG_DIR": "/x", "CLANKER_SESSION": "s", "CLAUDECODE": "1",
                 "CLAUDE_CODE_SESSION_ID": "i", "CLAUDE_CODE_MESSAGING_SOCKET": "/s",
                 "CLAUDE_CODE_DISABLE_SANDBOX": "1"}.items():
        monkeypatch.setenv(k, v)
    env = newsession.server_env()
    for k in ("CLAUDE_CONFIG_DIR", "CLANKER_SESSION", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
              "CLAUDE_CODE_MESSAGING_SOCKET"):
        assert k not in env
    assert env["CLAUDE_CODE_DISABLE_SANDBOX"] == "1"     # a setting, not an identity: kept


# ─── conversation copy rule ────────────────────────────────────────────────

def _w(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def test_copy_plan_verdicts_and_departure_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("CLANKER_DATA", str(tmp_path / "data"))
    src, dst = str(tmp_path / "src"), str(tmp_path / "dst")
    sid, cwd = "sid-1", "/w/x"
    enc = session_switch.encode_cwd(cwd)
    _w(f"{src}/projects/{enc}/{sid}.jsonl", b"l1\nl2\nl3\n")
    _w(f"{src}/projects/{enc}/{sid}/subagents/agent-a.jsonl", b"a1\n")
    _w(f"{src}/file-history/{sid}/h@v1", b"snap")
    _w(f"{src}/tasks/{sid}/1.json", b'{"s":"done"}')
    rels = session_switch.conversation_files(src, sid, cwd)
    assert rels[0] == f"projects/{enc}/{sid}.jsonl" and len(rels) == 4
    # target: transcript is a prefix, snapshot equal, task changed, subagent missing
    _w(f"{dst}/projects/{enc}/{sid}.jsonl", b"l1\n")
    _w(f"{dst}/file-history/{sid}/h@v1", b"snap")
    _w(f"{dst}/tasks/{sid}/1.json", b'{"s":"todo"}')
    copies, diverged = session_switch.plan_copy(src, dst, rels, {})
    assert diverged == [f"tasks/{sid}/1.json"]           # rewritten, never recorded: refuse
    session_switch.save_departure(sid, dst, rels)        # the session left dst in this state
    departed = session_switch.load_manifest(sid)[os.path.realpath(dst)]
    copies, diverged = session_switch.plan_copy(src, dst, rels, departed)
    assert diverged == []
    assert dict(copies) == {f"projects/{enc}/{sid}.jsonl": "prefix",
                            f"projects/{enc}/{sid}/subagents/agent-a.jsonl": "missing",
                            f"tasks/{sid}/1.json": "unchanged since it left"}
    session_switch.copy_files(src, dst, copies)
    for rel in rels:
        assert open(os.path.join(dst, rel), "rb").read() == open(os.path.join(src, rel), "rb").read()
        assert oct(os.stat(os.path.join(dst, rel)).st_mode & 0o777) in ("0o600", "0o644")
    # someone writes to dst after it was recorded: diverged again, and nothing is copied
    _w(f"{dst}/tasks/{sid}/1.json", b'{"s":"edited elsewhere"}')
    _w(f"{src}/tasks/{sid}/1.json", b'{"s":"newer"}')
    _c, diverged = session_switch.plan_copy(src, dst, rels, departed)
    assert diverged == [f"tasks/{sid}/1.json"]
    assert not [f for f in os.listdir(f"{dst}/tasks/{sid}") if f.startswith(".clanker-switch-")]


def test_transcript_found_outside_the_cwd_dir(tmp_path):
    _w(f"{tmp_path}/projects/-other/sid-2.jsonl", b"x\n")
    assert session_switch.conversation_files(str(tmp_path), "sid-2", "/not/it") == ["projects/-other/sid-2.jsonl"]
    assert session_switch.conversation_files(str(tmp_path), "sid-3", "/not/it") is None


# ─── launch (real CLI, fakes) ─────────────────────────────────────────────

def test_new_without_account_fails_and_creates_nothing(box):
    r = box.run("new", "t0")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "choose an account" in r.stderr and "a1" in r.stderr and "a3" in r.stderr
    assert "a2 (not logged in" in r.stderr
    assert not [c for c in box.tmux_calls() if c.startswith("new-session")]


def test_new_on_a3_pins_the_slot_leases_and_reports(box):
    r = box.run("new", "a3", "t1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "account: a3 max" in r.stderr and "warning: 5h 85% >= stand-down 80%" in r.stderr
    recs = box.records(box.a3_real)
    assert len(recs) == 1 and recs[0]["tmux"].startswith("t1:")
    env = proc_env(recs[0]["pid"])
    assert env["CLAUDE_CONFIG_DIR"] == box.cfg["a3"] and env["CLANKER_SESSION"] == "t1"
    assert box.records(box.a1) == []
    ls = box.leases()
    assert len(ls) == 1 and ls[0]["holder"] == "clanker:t1" and ls[0]["kind"] == "user"
    assert ls[0]["pid"] == recs[0]["pid"]                 # moved from the CLI's pid to claude's
    assert ls[0]["ttl_s"] == accounts.LEASE_TTL


def test_home_slot_unsets_an_inherited_config_dir(box):
    r = box.run("new", "a1", "t2", env={"CLAUDE_CONFIG_DIR": box.a3_real})
    assert r.returncode == 0, r.stdout + r.stderr
    recs = box.records(box.a1)
    assert len(recs) == 1
    assert "CLAUDE_CONFIG_DIR" not in proc_env(recs[0]["pid"])
    assert box.records(box.a3_real) == []


def test_unusable_or_refused_slot_creates_nothing(box):
    r = box.run("new", "a2", "t3")
    assert r.returncode == 1 and "not usable" in r.stderr
    box.rows[2]["refuse"] = ["config dir missing"]
    box.save_rows()
    r = box.run("new", "t4", "a3")
    assert r.returncode == 1 and "cannot run" in r.stderr
    assert not [c for c in box.tmux_calls() if c.startswith("new-session")]
    assert box.leases() == []


def test_trust_dialog_is_answered_on_yes(box):
    r = box.run("new", "a3", "t5", env={"FAKE_TMUX_TRUST": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    keys = [c for c in box.tmux_calls() if c.startswith("send-keys") and "-l" not in c]
    assert "send-keys -t =t5: Down" in keys                 # cursor moved off `No, exit` first
    assert len(box.records(box.a3_real)) == 1


def test_launch_that_never_starts_fails_fast_and_releases_the_lease(box):
    t0 = time.time()
    r = box.run("new", "a3", "t6", env={"FAKE_CLAUDE_FAIL_DIR": box.a3_real})
    assert r.returncode == 1 and "did not start" in r.stderr, r.stdout + r.stderr
    assert time.time() - t0 < 12                           # not the 15 s launch timeout
    assert box.leases() == []


def test_launch_during_a_claude_relink_retries_once(box):
    """2026-10-29 walk: a launch hit Claude Code's npm relink and got `env: 'claude':
    No such file or directory`. The launch retries once instead of failing."""
    r = box.run("new", "a3", "t7", env={"FAKE_TMUX_MISSING_ONCE": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    typed = [c for c in box.tmux_calls() if c.startswith("send-keys") and " -l -- cd " in c]
    assert len(typed) == 2
    assert len(box.records(box.a3_real)) == 1


def test_new_on_an_existing_name_asks_for_nothing(box):
    assert box.run("new", "a1", "ex").returncode == 0
    r = box.run("new", "ex")                                # no account needed: nothing starts
    assert r.returncode == 0 and "exists — opening it" in r.stderr, r.stdout + r.stderr


def test_work_attaches_without_account_and_launches_with_one(box):
    r = box.run("work", "bootproj", "--no-attach")
    assert r.returncode == 2 and "choose an account" in r.stderr
    r = box.run("work", "a1", "bootproj", "--no-attach")
    assert r.returncode == 0, r.stdout + r.stderr
    pid = box.records(box.a1)[0]["pid"]
    r = box.run("work", "bootproj", "--no-attach")          # running: attach only, never a switch
    assert r.returncode == 0 and "already exists" in r.stderr
    assert box.records(box.a1)[0]["pid"] == pid


# ─── switch (real CLI, fakes) ─────────────────────────────────────────────

def _transcript(slot_dir, rec):
    return os.path.join(slot_dir, "projects", session_switch.encode_cwd(rec["cwd"]),
                        rec["sessionId"] + ".jsonl")


def test_switch_a1_to_a3_and_back_keeps_the_conversation(box):
    assert box.run("new", "a1", "sw").returncode == 0
    rec1 = box.records(box.a1)[0]
    t1 = open(_transcript(box.a1, rec1), "rb").read()
    r = box.run("open", "sw", "a3")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "switched 'sw' from a1 to a3" in r.stderr and "SendMessage" in r.stderr
    rec3 = box.records(box.a3_real)[0]
    assert rec3["sessionId"] == rec1["sessionId"] and rec3["pid"] != rec1["pid"]
    assert box.records(box.a1) == []
    assert proc_env(rec3["pid"])["CLAUDE_CONFIG_DIR"] == box.cfg["a3"]
    t3 = open(_transcript(box.a3_real, rec3), "rb").read()
    assert t3.startswith(t1) and len(t3) > len(t1)            # a3 continued the copied conversation
    assert open(_transcript(box.a1, rec1), "rb").read() == t1  # the source is never touched
    ls = box.leases()
    assert [(l["slot"], l["pid"]) for l in ls] == [("a3", rec3["pid"])]
    # and back: the a1 copy is a prefix of a3's, so it is replaced and resumed
    r = box.run("open", "a1", "sw")
    assert r.returncode == 0, r.stdout + r.stderr
    back = box.records(box.a1)[0]
    assert back["sessionId"] == rec1["sessionId"]
    assert open(_transcript(box.a1, back), "rb").read().startswith(t3)
    log = [json.loads(l) for l in open(os.path.join(box.env["CLANKER_DATA"], "account-switch.jsonl"))]
    assert [(x["from"], x["to"], x["result"]) for x in log] == [("a1", "a3", "ok"), ("a3", "a1", "ok")]


def test_record_naming_the_web_bridge_session_is_found_by_pane(box):
    """2026-10-29 walk: with the dashboard terminal open, Claude's registry record
    named the grouped bridge session (`web-9e96f80f:@360.%391`), not the pane's own
    session. Matching by name missed it: the launch "timed out" and the rollback
    typed a launch line INTO the running Claude. Records are matched by pane id."""
    web = {"FAKE_CLAUDE_TMUX_SESSION": "web-9e96f80f"}
    r = box.run("new", "a1", "wb", env=web)
    assert r.returncode == 0, r.stdout + r.stderr
    sid = box.records(box.a1)[0]["sessionId"]
    r = box.run("open", "wb", "a3", env=web)
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.records(box.a3_real)[0]["sessionId"] == sid
    listing = box.run("tmux", "list").stdout.splitlines()
    assert [ln for ln in listing if ln.startswith("wb:")] == ["wb: 1 windows (created today)  [account a3]"]


def test_a_launch_line_is_never_typed_into_a_running_claude(box, monkeypatch):
    import newsession
    assert box.run("new", "a1", "nt").returncode == 0
    for k in ("HOME", "PATH", "CLANKER_ACCOUNTS_CMD", "FAKE_ACCT_DIR", "FAKE_TMUX_DIR", "CLANKER_DATA"):
        monkeypatch.setenv(k, box.env[k])
    monkeypatch.delenv("CLANKER_CLAUDE_SESSIONS_DIR", raising=False)
    accounts.forget_cache()
    before = len(box.tmux_calls())
    slot = accounts.get("a3", accounts.slots())
    rec, err = newsession.start_in_pane("nt", box.proj, slot, resume="x")
    assert rec is None and "not a shell — nothing typed" in err and "Claude pid" in err
    assert not [c for c in box.tmux_calls()[before:] if c.startswith("send-keys")]


def test_switch_of_a_session_without_a_conversation_starts_fresh(box):
    env = {"FAKE_CLAUDE_NO_TRANSCRIPT": "1"}
    assert box.run("new", "a1", "fr", env=env).returncode == 0
    old = box.records(box.a1)[0]
    r = box.run("open", "fr", "a3", env=env)
    assert r.returncode == 0 and "no conversation yet: started fresh" in r.stderr, r.stdout + r.stderr
    new = box.records(box.a3_real)[0]
    assert new["sessionId"] != old["sessionId"] and box.records(box.a1) == []


def test_switch_refusals_change_nothing(box):
    assert box.run("new", "a1", "busy", env={"FAKE_CLAUDE_STATUS": "busy"}).returncode == 0
    pid = box.records(box.a1)[0]["pid"]
    r = box.run("open", "busy", "a3")
    assert r.returncode == 1 and "only an idle session" in r.stderr
    assert box.records(box.a1)[0]["pid"] == pid
    r = box.run("open", "busy", "a2")
    assert r.returncode == 1 and "not usable" in r.stderr
    r = box.run("open", "busy", "a1")
    assert r.returncode == 0 and "already runs on account a1" in r.stderr


def test_switch_refuses_a_diverged_conversation(box):
    assert box.run("new", "a1", "dv").returncode == 0
    rec = box.records(box.a1)[0]
    _w(_transcript(box.a3_real, rec), b'{"type": "user", "other": "history"}\n')
    r = box.run("open", "dv", "a3")
    assert r.returncode == 1 and "cannot be merged" in r.stderr
    assert box.records(box.a1)[0]["pid"] == rec["pid"]        # Claude was never stopped


def test_switch_rolls_back_when_the_target_does_not_start(box):
    assert box.run("new", "a1", "rb").returncode == 0
    rec = box.records(box.a1)[0]
    r = box.run("open", "rb", "a3",
                env={"FAKE_CLAUDE_FAIL_DIR": box.a3_real, "CLANKER_LAUNCH_TIMEOUT": "3"})
    assert r.returncode == 1 and "rolled back" in r.stderr, r.stdout + r.stderr
    again = box.records(box.a1)
    assert len(again) == 1 and again[0]["sessionId"] == rec["sessionId"] and again[0]["pid"] != rec["pid"]
    assert [l["slot"] for l in box.leases()] == []             # target lease released
    log = [json.loads(l) for l in open(os.path.join(box.env["CLANKER_DATA"], "account-switch.jsonl"))]
    assert log[-1]["result"] == "rollback"


# ─── dashboard ─────────────────────────────────────────────────────────────

def _import_serve():
    old = os.environ.get("CLANKER_DATA")
    os.environ["CLANKER_DATA"] = tempfile.mkdtemp(prefix="clk-acct-serve-")
    try:
        import serve
    finally:
        if old is None:
            os.environ.pop("CLANKER_DATA", None)
        else:
            os.environ["CLANKER_DATA"] = old
    return serve


def _use_box_env(box, monkeypatch):
    for k in ("HOME", "PATH", "CLANKER_ACCOUNTS_CMD", "FAKE_ACCT_DIR", "FAKE_TMUX_DIR",
              "CLANKER_DATA", "CLANKER_LAUNCH_TIMEOUT", "CLANKER_REGISTRY", "CLANKER_PROJECT_ROOTS"):
        monkeypatch.setenv(k, box.env[k])
    for k in ("CLANKER_CLAUDE_SESSIONS_DIR", "CLAUDE_CONFIG_DIR", "CLANKER_CLAUDE_DIR"):
        monkeypatch.delenv(k, raising=False)
    accounts.forget_cache()


def test_registry_and_account_fields_span_every_slot(box, monkeypatch):
    serve = _import_serve()
    assert box.run("new", "a1", "d1").returncode == 0
    assert box.run("new", "a3", "d3").returncode == 0
    _use_box_env(box, monkeypatch)
    reg = serve.read_session_registry()
    by = {r["tmux_session"]: r for r in reg}
    assert set(by) == {"d1", "d3"}                         # the a3 session is no longer invisible
    sl, ls = serve._slots_and_leases()
    f1, f3 = serve.account_fields(by["d1"], sl, ls), serve.account_fields(by["d3"], sl, ls)
    assert f1["account"]["slot"] == "a1" and f3["account"]["slot"] == "a3"
    assert f3["lease"]["kind"] == "user" and f3["managed"] is True


def test_reconciler_leases_a_clanker_session_that_lost_its_lease(box, monkeypatch):
    serve = _import_serve()
    assert box.run("new", "a3", "rc").returncode == 0
    with open(os.path.join(box.tmp, "acct", "leases.json"), "w") as f:
        json.dump([], f)                                   # e.g. the ttl passed while the service was down
    _use_box_env(box, monkeypatch)
    assert serve.reconcile_leases() == (0, 1, 0)
    ls = box.leases()
    assert len(ls) == 1 and ls[0]["holder"] == "clanker:rc" and ls[0]["pid"] == box.records(box.a3_real)[0]["pid"]
    assert serve.reconcile_leases() == (1, 0, 0)            # renewed, not doubled


def test_web_new_session_requires_an_account(box, monkeypatch):
    serve = _import_serve()
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    _use_box_env(box, monkeypatch)

    async def main():
        app = web.Application()
        app.router.add_post("/api/session/new", serve.handle_session_new)
        app.router.add_get("/api/accounts", serve.handle_accounts)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get("/api/accounts")
            d = await r.json()
            ids = {s["id"]: s for s in d["slots"]}
            assert d["enabled"] and set(ids) == {"a1", "a2", "a3"} and not ids["a2"]["usable"]
            r = await client.post("/api/session/new", json={"name": "w1", "account": "a3"})
            assert r.status == 400 and (await r.json())["error"] == "choose a project"
            r = await client.post("/api/session/new", json={"name": "w1", "project": "nope", "account": "a3"})
            assert r.status == 400 and "unknown project" in (await r.json())["error"]
            r = await client.post("/api/session/new", json={"name": "w1", "project": "bootproj"})
            assert r.status == 400 and (await r.json())["error"] == "choose an account"
            r = await client.post("/api/session/new", json={"name": "w1", "project": "bootproj", "account": "a3"})
            d = await r.json()
            assert r.status == 200 and d["session"] == "w1" and "account: a3" in d["message"]
        finally:
            await client.close()

    asyncio.run(main())
    recs = box.records(box.a3_real)
    assert len(recs) == 1 and recs[0]["cwd"] == box.proj                # in the project, not at $HOME
    assert "w1:" + box.proj in open(os.path.join(box.home, ".tmux-startup.sh")).read()   # kept


# ─── statusline + F5 refresher ─────────────────────────────────────────────

def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_statusline_and_refresher_agree_on_the_slot_cache(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    other = tmp_path / "a3"
    other.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLANKER_USAGE_DIR", str(tmp_path / "cache"))
    ref = _load(os.path.join(REPO, "scripts", "statusline-usage-refresh.py"), "refresher_t")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert ref.cache_dir() == str(tmp_path / "cache")
    assert ref.credentials_path() == str(home / ".claude" / ".credentials.json")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    h = hashlib.sha256(os.path.realpath(str(other)).encode()).hexdigest()[:10]
    assert ref.cache_dir() == str(tmp_path / "cache" / ("cfg-" + h))
    assert ref.credentials_path() == str(other / ".credentials.json")
    sl = _load(os.path.join(REPO, "scripts", "statusline.py"), "statusline_t")
    monkeypatch.setattr(sl, "USAGE_DIR", str(tmp_path / "cache"))
    assert sl.slot_usage_dir() == ref.cache_dir()          # F5 of a3 is read where a3's refresher writes it


def test_statusline_names_the_slot(tmp_path, monkeypatch):
    sl = _load(os.path.join(REPO, "scripts", "statusline.py"), "statusline_t2")
    cache = tmp_path / "accounts.json"
    cache.write_text(json.dumps([{"slot": "a1", "resolves_to": str(tmp_path / "h")},
                                 {"slot": "a3", "resolves_to": str(tmp_path / "s3")}]))
    (tmp_path / "s3").mkdir()
    monkeypatch.setattr(sl, "ACCOUNTS_CACHE", str(cache))
    monkeypatch.setattr(sl, "USAGE_DIR", str(tmp_path))
    monkeypatch.setenv("CLANKER_ACCOUNTS_CMD", "true")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "s3"))
    assert sl.account_slot() == "a3"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert sl.account_slot() == "?"
    monkeypatch.setenv("CLANKER_ACCOUNTS_CMD", "none")
    assert sl.account_slot() is None


def test_prompt_box_suggestion_is_not_a_draft():
    """2026-10-29 walk: the switch reported Claude's dim suggestion (`Try "how do I log
    an error?"`) as text it cleared. Dim text is a suggestion; typed text is not dim."""
    E = "\x1b"
    assert session_switch.undimmed(f"{E}[39m❯ {E}[2mdelete the clones and snapshots{E}[0m").strip() == "❯"
    assert session_switch.undimmed(f"{E}[39m❯ fix the bug{E}[0m") == "❯ fix the bug"
    assert session_switch.undimmed(f"❯ {E}[38;5;2mgreen typed{E}[0m") == "❯ green typed"   # 38;5;2 is a colour
    assert session_switch.undimmed(f"❯ a{E}[2mb{E}[22mc") == "❯ ac"


def test_refresher_records_an_account_without_a_fable_limit(tmp_path, monkeypatch):
    """2026-10-29: the home login answers limits[] = session + weekly_all only. The
    refresher failed every 5 min for 18 days and the statusline kept `F5 61% ↻ reset`
    from 2026-10-10. Now: no Fable entry in a well-formed limits[] = no F5."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLANKER_USAGE_DIR", str(tmp_path))
    ref = _load(os.path.join(REPO, "scripts", "statusline-usage-refresh.py"), "refresher_f5")
    monkeypatch.setattr(ref, "read_token", lambda: ("tok", None))
    monkeypatch.setattr(ref, "endpoint_url", lambda: "https://example.invalid/api/oauth/usage")
    answer = {}
    monkeypatch.setattr(ref, "fetch", lambda token, url: (200, None, json.dumps(answer["body"]).encode()))
    cache = lambda: json.loads((tmp_path / "usage.json").read_text())
    fable = {"kind": "weekly_scoped", "percent": 40, "resets_at": "2033-01-01T00:00:00Z",
             "scope": {"model": {"display_name": "Fable 5.1"}}}
    answer["body"] = {"limits": [{"kind": "session", "percent": 7}, fable]}
    assert ref.refresh(str(tmp_path), True) == 0 and cache()["fable"]["percent"] == 40
    answer["body"] = {"limits": [{"kind": "session", "percent": 7}, {"kind": "weekly_all", "percent": 83}]}
    assert ref.refresh(str(tmp_path), True) == 0 and cache()["fable"] is None
    sl = _load(os.path.join(REPO, "scripts", "statusline.py"), "statusline_f5")
    monkeypatch.setattr(sl, "USAGE_DIR", str(tmp_path))
    assert sl.fable_gauge_data() is None                                  # no F5 segment
    before = (tmp_path / "usage.json").read_text()
    answer["body"] = {"limits": [dict(fable, percent=None)]}              # a Fable entry we cannot read
    assert ref.refresh(str(tmp_path), True) == 1 and (tmp_path / "usage.json").read_text() == before
    answer["body"] = {"five_hour": {}}                                     # no limits[] at all
    assert ref.refresh(str(tmp_path), True) == 1 and (tmp_path / "usage.json").read_text() == before


def test_accounts_command_is_found_without_bin_on_path(tmp_path, monkeypatch):
    """2026-10-29 deploy: systemd's PATH lacks ~/bin, so the dashboard found no
    claude-acct and read the home slot only (18 of 24 Claude panes)."""
    b = tmp_path / "bin"
    b.mkdir()
    exe = b / "claude-acct"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.delenv("CLANKER_ACCOUNTS_CMD", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert accounts.accounts_cmd() == [str(exe)]
    exe.chmod(0o644)                                      # not executable: single-slot mode
    assert accounts.accounts_cmd() is None
