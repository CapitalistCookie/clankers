"""The everyday CLI (2026-10-29): the account as a plain word, `clanker ls`, forgiving
session names, the grouped help, completion, and every account's usage — from live
sessions' statuslines (harvest_limits) to `clanker ls` and the dashboard.

The end-to-end tests reuse the tmp box of test_session_accounts.py (fake tmux, claude
and claude-acct; nothing live is touched)."""
import importlib.machinery
import importlib.util
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))
import accounts  # noqa: E402
import fleet  # noqa: E402
from test_session_accounts import Box  # noqa: E402


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    yield b
    b.kill_all()


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("clanker_cli", os.path.join(REPO, "bin", "clanker"))
    spec = importlib.util.spec_from_loader("clanker_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _load_statusline():
    spec = importlib.util.spec_from_file_location("statusline_ux", os.path.join(REPO, "scripts", "statusline.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── the account as a word ─────────────────────────────────────────────────

def test_take_slot_anywhere_one_at_most():
    assert accounts.take_slot(["a3", "foo"]) == ("a3", ["foo"], None)
    assert accounts.take_slot(["foo", "bar", "a12"]) == ("a12", ["foo", "bar"], None)
    assert accounts.take_slot(["foo"]) == (None, ["foo"], None)
    slot, rest, err = accounts.take_slot(["a1", "a3", "foo"])
    assert slot is None and "two accounts" in err
    for not_a_slot in ("a0", "A3", "a3b", "acct3", "3", "a123"):
        assert accounts.take_slot([not_a_slot])[0] is None


def test_new_with_the_account_as_a_word(box):
    r = box.run("new", "a3", "foo", "bar")
    assert r.returncode == 0, r.stdout + r.stderr
    recs = box.records(box.a3_real)
    assert len(recs) == 1 and recs[0]["tmux"].startswith("foo-bar:")
    assert box.run("new", "a9", "x").returncode == 1               # slot-shaped typo: never a session a9
    r = box.run("new", "a1", "a3", "x")
    assert r.returncode == 2 and "two accounts" in r.stderr
    r = box.run("new", "x")                                         # no account, no terminal
    assert r.returncode == 2 and "clanker new <slot> [project] [name]" in r.stderr


# ─── forgiving names ───────────────────────────────────────────────────────

NAMES = ["maintainence", "constructionmanagement", "batteryVML", "colonizers", "colonizers-build",
         "trade-relay", "clanker", "clanker-41"]


@pytest.mark.parametrize("words,want,how", [
    (["clanker"], "clanker", "exact"),
    (["colonizers"], "colonizers", "exact"),
    (["batteryvml"], "batteryVML", "case"),
    (["construction", "management"], "constructionmanagement", "exact"),     # the words joined
    (["Construction", "Management"], "constructionmanagement", "case"),
    (["construction-man", "agement"], "constructionmanagement", "spacing"),
    (["trade", "relay"], "trade-relay", "exact"),
    (["constr"], "constructionmanagement", "prefix"),
])
def test_resolve_finds_the_session(words, want, how):
    assert fleet.resolve(words, NAMES)[:2] == (want, how)


@pytest.mark.parametrize("words,suggested", [
    (["maintanence"], "maintainence"),
    (["battertVML"], "batteryVML"),
    (["trade", "relya"], "trade-relay"),
    (["colon"], "colonizers"),                                      # two prefixes: never a guess
])
def test_resolve_only_suggests_when_unsure(words, suggested):
    name, _how, sugg = fleet.resolve(words, NAMES)
    assert name is None and suggested in sugg


def test_open_forgives_case_and_suggests_on_typos(box):
    assert box.run("new", "a1", "Sw").returncode == 0
    r = box.run("open", "sw")
    assert r.returncode == 0 and "→ Sw" in r.stderr, r.stdout + r.stderr
    r = box.run("open", "swx")
    assert r.returncode == 1 and "did you mean: Sw" in r.stderr


def test_a_script_never_moves_a_terminal(box):
    """2026-10-29 walk: `clanker open` from a Claude tool call inside tmux switched the
    owner's client. Without a terminal it only says what to run."""
    assert box.run("new", "a1", "sx").returncode == 0
    r = box.run("open", "sx", env={"TMUX": "/tmp/tmux-test/default,1,0"})
    assert r.returncode == 0 and "run:  tmux switch-client -t sx" in r.stderr
    assert r.stdout == "sx\n"                       # stdout: the result only (clig.dev)
    assert not [c for c in box.tmux_calls() if c.startswith(("switch-client", "attach"))]


# ─── ls ────────────────────────────────────────────────────────────────────

def test_bare_clanker_is_ls_with_every_accounts_usage(box):
    assert box.run("new", "a3", "l3").returncode == 0
    assert box.run("new", "a1", "l1").returncode == 0
    assert box.run("new", "--shell", "sh1").returncode == 0
    r = box.run()
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "ACCOUNT" in out and "not usable: a2 (not logged in)" in out
    a3 = [ln for ln in out.splitlines() if ln.startswith("a3 ")][0]
    import re
    assert "85%" in a3 and re.search(r"\s1\s+live\b", a3), a3          # 1 session on a3, live figure
    rows = [ln for ln in out.splitlines() if ln.startswith(("l1", "l3", "sh1"))]
    assert [ln.split()[:2] for ln in rows] == [["l1", "a1"], ["l3", "a3"]]   # shells hidden by default
    assert "plain shell session(s) — clanker ls -a" in out
    assert "sh1" in box.run("ls", "-a").stdout
    only = box.run("ls", "a3").stdout
    assert "l3" in only and "l1 " not in only
    d = json.loads(box.run("ls", "--json").stdout)
    assert {s["name"] for s in d["sessions"]} >= {"l1", "l3"}
    assert {a["id"] for a in d["accounts"]} == {"a1", "a2", "a3"}


# ─── help, unknown verbs, completion ───────────────────────────────────────

def test_every_verb_is_in_the_help_or_hidden():
    cli = _load_cli()
    _parser, sub = cli.build_parser()
    listed = set(cli.help_verbs())
    missing = [v for v in sub.choices if v not in listed and v not in cli.HIDDEN]
    assert missing == []
    assert not [v for v in listed if v not in sub.choices]            # the help names real verbs


def test_help_and_unknown_verbs(box):
    r = box.run("help")
    assert r.returncode == 0 and "Sessions" in r.stdout and "ls [slot]" in r.stdout
    assert "temporal" not in r.stdout and "temporal" in box.run("help", "all").stdout
    r = box.run("openclanker")
    assert r.returncode == 2 and "clanker open clanker" in r.stderr
    r = box.run("ope")
    assert r.returncode == 2 and "clanker open" in r.stderr


def test_completion_candidates(box):
    assert box.run("new", "a1", "c1").returncode == 0
    words = lambda *w: box.run("_complete", *w).stdout.split()
    assert {"ls", "new", "open", "close"} <= set(words()) and "work" not in words()
    assert set(words("new")) == {"a1", "a3", "bootproj"}            # usable slots only, projects
    assert set(words("new", "a3", "bootproj")) == set()              # after the project: a free name
    assert "c1" in words("open") and "a3" in words("open")
    assert "a3" not in words("open", "c1", "a1")                     # one account per command
    assert "bootproj" in words("work")
    script = box.run("completion").stdout
    assert "complete -o default -F _clanker_complete clanker" in script


# ─── every account's usage: statusline → mirror → ls / dashboard ───────────

def test_harvest_publishes_only_real_api_responses(tmp_path, monkeypatch):
    sl = _load_statusline()
    monkeypatch.setenv("CLANKER_LIMITS_DIR", str(tmp_path))
    path = tmp_path / "a3.json"
    read = lambda: json.loads(path.read_text())
    # a session already running when this code arrives: its figures may be old — record only
    assert sl.harvest_limits("a3", "old", 40, 70, 2000000000, 2000500000, now=1000) is False
    assert "five_hour" not in read()
    # a new session: no figures yet, then its first API response — published
    assert sl.harvest_limits("a3", "new", None, None, None, None, now=1010) is False
    assert sl.harvest_limits("a3", "new", 55, 80, 2000000000, 2000500000, now=1020) is True
    d = read()
    assert (d["five_hour"], d["seven_day"], d["source"]) == (0.55, 0.8, "statusline")
    assert d["windows_at"] == "1970-01-01T00:17:00Z" and d["resets_at"]["five_hour"].startswith("2033-")
    # the idle session re-runs every 10 s with its old figures: nothing is published
    assert sl.harvest_limits("a3", "old", 40, 70, 2000000000, 2000500000, now=1030) is False
    assert read()["five_hour"] == 0.55
    # the busy session's next response: published
    assert sl.harvest_limits("a3", "new", 57, 80, 2000000000, 2000500000, now=1040) is True
    assert read()["five_hour"] == 0.57
    # no slot, or a dir that is no slot: nothing
    assert sl.harvest_limits(None, "x", 1, 1, None, None) is False
    assert sl.harvest_limits("?", "x", 1, 1, None, None) is False


def test_dashboard_status_carries_every_accounts_usage(box, monkeypatch):
    from test_session_accounts import _import_serve, _use_box_env
    serve = _import_serve()
    assert box.run("new", "a3", "u3").returncode == 0
    _use_box_env(box, monkeypatch)
    sl, ls = serve._slots_and_leases()
    sessions = [{"session": "u3", "account": {"slot": "a3"}}, {"session": "u3", "account": {"slot": "a3"}}]
    us = {u["id"]: u for u in serve.account_usage(sl, sessions)}
    assert set(us) == {"a1", "a2", "a3"}                              # a5 (empty) is not shown
    assert us["a3"]["sessions"] == 1 and us["a3"]["five_hour"] == 0.85
    assert us["a3"]["figure_source"] == "mirror:clanker-statusline"
    assert us["a2"]["usable"] is False


def test_dashboard_takes_login_state_from_the_checked_list(box, monkeypatch):
    """2026-10-29 walk: a4 (never checked: `live?` in the fast list) showed as a usable
    account on the dashboard while `clanker ls` said "not logged in"."""
    from test_session_accounts import _use_box_env
    _use_box_env(box, monkeypatch)
    calls = []
    real = accounts.slots

    def fake(online=False, max_age=accounts.LIST_TTL):
        calls.append(online)
        rows = [dict(r) for r in real(online=online, max_age=0)]
        if not online:
            for r in rows:
                if r["id"] == "a2":
                    r.update(status="live?", usable=True, why="")
        return rows
    monkeypatch.setattr(accounts, "slots", fake)
    sl, _err = accounts.slots_display()
    a2 = accounts.get("a2", sl)
    assert a2["usable"] is False and "not logged in" in a2["why"] and True in calls


# ─── project-first sessions: open / new / close (lib/session_flow.py) ──────

def _boot(box):
    p = os.path.join(box.home, ".tmux-startup.sh")
    from tmux_manager import startup_entries
    import tmux_manager
    old = tmux_manager.STARTUP_SCRIPT
    tmux_manager.STARTUP_SCRIPT = p
    try:
        return startup_entries()
    finally:
        tmux_manager.STARTUP_SCRIPT = old


def test_new_outside_every_project_starts_nothing(box):
    """Owner, 2026-10-29: "no session should be projectless"."""
    r = box.run("new", "a3", "foo", cwd=box.tmp)
    assert r.returncode == 1 and "no project 'foo'" in r.stderr and "clanker create foo" in r.stderr
    r = box.run("new", "a3", "bootprj", cwd=box.tmp)                  # a typo of a project
    assert r.returncode == 1 and "did you mean: bootproj" in r.stderr
    r = box.run("new", "a3", cwd=box.tmp)
    assert r.returncode == 2 and "not inside a project" in r.stderr
    assert box.records(box.a3_real) == [] and box.leases() == []
    r = box.run("new", "--shell", "sh0", cwd=box.tmp)                  # a plain shell may be anywhere
    assert r.returncode == 0, r.stderr


def test_new_names_and_places_sessions_in_their_project(box):
    sub = os.path.join(box.proj, "deep", "er")
    os.makedirs(sub)
    assert box.run("new", "a3", "bootproj", "-d", cwd=box.tmp).returncode == 0      # named from outside
    assert box.run("new", "a3", "-d", cwd=sub).returncode == 0                       # the project you are in
    assert box.run("new", "a3", "review", "-d", cwd=sub).returncode == 0             # a name of your own
    recs = box.records(box.a3_real)
    assert sorted(r["tmux"].split(":")[0] for r in recs) == ["bootproj", "bootproj-2", "review"]
    assert {r["cwd"] for r in recs} == {box.proj}                                    # the project root
    assert {"bootproj", "bootproj-2", "review"} <= set(_boot(box))                   # kept across reboots
    r = box.run("new", "a3", "review", "-d")
    assert r.returncode == 0 and "exists — opening it" in r.stderr
    assert len(box.records(box.a3_real)) == 3


def test_new_in_a_git_worktree_stays_in_the_worktree(box):
    import subprocess
    git = lambda *a: subprocess.run(["git", "-C", box.proj, *a], capture_output=True, text=True, check=True)
    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
    wt = os.path.join(box.tmp, "wt-feature")
    git("worktree", "add", "-q", "-b", "feature", wt)
    r = box.run("new", "a3", "feat", "-d", cwd=wt)
    assert r.returncode == 0, r.stdout + r.stderr
    rec = box.records(box.a3_real)[0]
    assert rec["cwd"] == os.path.realpath(wt) and _boot(box)["feat"] == os.path.realpath(wt)


def test_open_a_project_starts_its_session_then_attaches(box):
    r = box.run("open", "bootproj", "-d")
    assert r.returncode == 2 and "choose an account" in r.stderr       # Claude never starts on a guess
    r = box.run("open", "bootproj", "a3", "-d", cwd=box.tmp)
    assert r.returncode == 0, r.stdout + r.stderr
    rec = box.records(box.a3_real)[0]
    assert rec["cwd"] == box.proj and rec["tmux"].startswith("bootproj:")
    assert _boot(box)["bootproj"] == box.proj
    r = box.run("open", "bootproj", "-d")
    assert r.returncode == 0 and "already exists" in r.stderr and box.records(box.a3_real) == [rec]
    r = box.run("work", "bootproj", "--no-attach")                       # the old verb and flag still work
    assert r.returncode == 0 and box.records(box.a3_real) == [rec]


def test_open_restarts_claude_in_a_kept_shell(box):
    """After a reboot a kept session is a plain shell: open starts Claude again."""
    import signal
    assert box.run("open", "bootproj", "a1", "-d").returncode == 0
    os.kill(box.records(box.a1)[0]["pid"], signal.SIGTERM)
    import time
    for _ in range(50):
        if not box.records(box.a1):
            break
        time.sleep(0.1)
    r = box.run("open", "bootproj", "-d")
    assert r.returncode == 2 and "choose an account" in r.stderr
    r = box.run("open", "bootproj", "a3", "-d")
    assert r.returncode == 0 and "was a plain shell" in r.stderr, r.stdout + r.stderr
    assert box.records(box.a3_real)[0]["cwd"] == box.proj


def test_open_never_starts_claude_outside_a_project(box):
    assert box.run("new", "--shell", "loose", "-d", cwd=box.tmp).returncode == 0
    r = box.run("open", "loose", "a3", "-d")
    assert r.returncode == 1 and "outside every project" in r.stderr
    assert box.records(box.a3_real) == []
    r = box.run("open", "loose", "-d")
    assert r.returncode == 0 and "outside every project" in r.stderr


def test_close_ends_claude_cleanly_and_forgets_the_session(box):
    assert box.run("new", "a3", "c1", "-d").returncode == 0
    rec = box.records(box.a3_real)[0]
    assert [l for l in box.leases() if l.get("holder") == "clanker:c1" and not l.get("released")]
    r = box.run("close", "c1")
    assert r.returncode == 2 and "clanker close c1 -y" in r.stderr     # no terminal: only -y closes
    assert box.records(box.a3_real) == [rec]
    r = box.run("close", "C1", "-y")                                    # names forgiven
    assert r.returncode == 0, r.stdout + r.stderr
    assert "closed 'c1' (and its boot entry)" in r.stderr
    assert f"clanker new a3 bootproj c1 --resume {rec['sessionId']}" in r.stderr
    assert box.records(box.a3_real) == []                               # Claude exited
    assert "c1" not in _boot(box)
    assert box.run("_complete", "close").stdout.split() == []          # the session is gone
    assert not [l for l in box.leases() if l.get("holder") == "clanker:c1" and not l.get("released")]
    assert any(c.startswith("send-keys -t %") and c.endswith("/exit") for c in box.tmux_calls())
    assert box.run("close", "c1", "-y").returncode == 1
    assert box.run("new", "a3", "c2", "-d", env={"FAKE_CLAUDE_NO_TRANSCRIPT": "1"}).returncode == 0
    r = box.run("close", "c2", "-y")                                    # never got a message: nothing to resume
    assert r.returncode == 0 and "--resume" not in r.stderr and "conversation" not in r.stderr


def test_close_also_ends_the_dashboard_terminal_on_it(box):
    import subprocess
    assert box.run("new", "a3", "c3", "-d").returncode == 0
    subprocess.run(["tmux", "new-session", "-d", "-t", "=c3", "-s", "web-abc123"], env=box.env, check=True)
    r = box.run("close", "c3", "-y")
    assert r.returncode == 0 and "closed 1 dashboard terminal(s) on it" in r.stderr, r.stdout + r.stderr
    assert "kill-session -t =web-abc123" in box.tmux_calls()
    assert box.records(box.a3_real) == []


def test_close_refuses_working_claude_and_unsent_text(box):
    assert box.run("new", "a3", "busy1", "-d", env={"FAKE_CLAUDE_STATUS": "busy"}).returncode == 0
    r = box.run("close", "busy1", "-y")
    assert r.returncode == 1 and "Claude is working" in r.stderr and "--force" in r.stderr
    assert len(box.records(box.a3_real)) == 1
    r = box.run("close", "busy1", "-y", "--force")
    assert r.returncode == 0 and box.records(box.a3_real) == []
    assert box.run("new", "a3", "draft1", "-d").returncode == 0
    import subprocess
    subprocess.run(["tmux", "send-keys", "-t", "=draft1:", "-l", "--", "half a thought"],
                   env=box.env, check=True)
    r = box.run("close", "draft1", "-y")
    assert r.returncode == 1 and "unsent text" in r.stderr and "half a thought" in r.stderr
    assert len(box.records(box.a3_real)) == 1


def test_ls_shows_the_project_and_flags_loose_sessions():
    now = 1000.0
    rs = [{"name": "a", "panes": 1, "claude": 1, "slots": ["a3"], "state": "waiting", "since_s": 60,
           "stuck": False, "attached": False, "dir": "~/projects/x", "project": "x", "session_ids": []},
          {"name": "b", "panes": 1, "claude": 1, "slots": ["a1"], "state": "waiting", "since_s": 60,
           "stuck": False, "attached": False, "dir": "~", "project": None, "session_ids": []}]
    out = fleet.render_rows(rs, now)
    a = [ln for ln in out.splitlines() if ln.startswith("a ")][0]
    b = [ln for ln in out.splitlines() if ln.startswith("b ")][0]
    assert a.rstrip().endswith(" x") and b.rstrip().endswith("— ~")
    assert "outside every project (1 Claude session(s))" in out


def test_help_names_the_four_session_verbs(box):
    out = box.run("help").stdout
    for v in ("ls [slot]", "open <project|session> [slot]", "new [slot] [project] [name]", "close <session>"):
        assert v in out
    assert "work" not in out.split("Projects")[0] and "tmux" not in out


def test_a_claude_without_a_registry_record_is_no_plain_shell(monkeypatch):
    """2026-10-29: `main` ran a Claude Code from a since-replaced binary that left only
    sessions/<pid>.key — `ls` hid it among 51 plain shells."""
    import claude_registry
    monkeypatch.setattr(fleet, "panes", lambda: [
        {"session": "main", "pane_id": "%27", "pane_pid": 1767, "command": "claude", "attached": False,
         "path": "/nowhere"},
        {"session": "sh", "pane_id": "%28", "pane_pid": 1768, "command": "bash", "attached": False,
         "path": "/nowhere"}])
    monkeypatch.setattr(claude_registry, "read_all", lambda sl: [])
    monkeypatch.setattr(claude_registry, "proc_children", lambda pid: [113101] if pid == 1767 else [])
    monkeypatch.setattr(accounts, "slot_of_pid", lambda pid, sl: {"id": "a1"} if pid == 113101 else None)
    rs = {r["name"]: r for r in fleet.rows(sl=[])}
    assert (rs["main"]["claude"], rs["main"]["state"], rs["main"]["slots"]) == (1, "unknown", ["a1"])
    assert rs["sh"]["state"] == "shell"
    out = fleet.render_rows(list(rs.values()), 0)
    assert "main" in out and "unknown = Claude runs but wrote no registry record" in out


# ─── CLI conventions (clig.dev), 2026-10-29: items 1-12 ─────────────────────

def test_held_interrupts_hold_ctrl_c_on_the_main_thread_only():
    import signal
    import threading
    import time
    import session_switch
    notes = []
    before = signal.getsignal(signal.SIGINT)
    with session_switch.held_interrupts(notes.append) as got:
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.05)                                   # no KeyboardInterrupt here
    assert got == [signal.SIGINT] and "Ctrl-C is held" in notes[0]
    assert signal.getsignal(signal.SIGINT) is before
    seen = []
    t = threading.Thread(target=lambda: seen.append(session_switch.held_interrupts().__enter__()))
    t.start(); t.join()
    assert seen == [[]]                                    # a worker thread sets no handler


def test_ctrl_c_during_a_switch_waits_until_it_is_done(box):
    """Before: a Ctrl-C between Claude stopping and starting again skipped the
    rollback and left a bare shell."""
    import signal
    import subprocess
    import time
    assert box.run("new", "a1", "sw2", "-d").returncode == 0
    p = subprocess.Popen([sys.executable, os.path.join(REPO, "bin", "clanker"), "open", "sw2", "a3", "-d"],
                         cwd=box.proj, env=dict(box.env, FAKE_CLAUDE_REGISTER_DELAY="4"),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
    time.sleep(2.5)                                        # Claude has stopped; the relaunch waits
    p.send_signal(signal.SIGINT)
    out, err = p.communicate(timeout=60)
    assert p.returncode == 0, out + err
    assert "switched 'sw2' from a1 to a3" in err
    assert [r["tmux"].split(":")[0] for r in box.records(box.a3_real)] == ["sw2"]


def test_remove_asks_and_ends_the_session_through_close(box):
    import yaml
    reg = lambda: yaml.safe_load(open(box.env["CLANKER_REGISTRY"]))["projects"]
    assert box.run("open", "bootproj", "a3", "-d", env={"FAKE_CLAUDE_STATUS": "busy"}).returncode == 0
    r = box.run("remove", "bootproj")
    assert r.returncode == 2 and "clanker remove bootproj -y" in r.stderr and "bootproj" in reg()
    r = box.run("remove", "bootproj", "-y", "--force-schedules")
    assert r.returncode == 1 and "Claude is working" in r.stderr and "kept as it is" in r.stdout + r.stderr
    assert "bootproj" in reg() and len(box.records(box.a3_real)) == 1
    r = box.run("project", "remove", "bootproj", "-y", "-f", "--force-schedules")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "bootproj" not in reg() and box.records(box.a3_real) == []


def test_a_script_names_sessions_exactly(box):
    assert box.run("new", "a1", "longname", "-d").returncode == 0
    r = box.run("open", "longn", "-d")                     # a prefix: fine on a terminal only
    assert r.returncode == 1 and "a script names it exactly" in r.stderr and "longname" in r.stderr
    assert box.run("open", "LongName", "-d").returncode == 0     # case aside


def test_version_quiet_and_json_results(box):
    r = box.run("--version")
    assert r.returncode == 0 and r.stdout.startswith("clanker ")
    r = box.run("new", "a3", "j1", "-d")
    assert r.returncode == 0 and r.stdout == "j1\n" and "started claude" in r.stderr
    r = box.run("new", "a3", "j2", "-d", "-q")
    assert r.returncode == 0 and r.stdout == "j2\n" and r.stderr == ""
    r = box.run("-q", "open", "j2", "--json")
    d = json.loads(r.stdout)
    assert r.returncode == 0 and d["session"] == "j2" and d["action"] == "attached" and r.stderr == ""
    r = box.run("new", "a3", "j3", "--json")
    d = json.loads(r.stdout)
    assert d["action"] == "started" and d["account"] == "a3" and d["project"] == "bootproj"
    r = box.run("close", "j3", "-y", "--json")
    assert r.returncode == 0 and json.loads(r.stdout) == {"session": "j3", "action": "closed"}
    r = box.run("close", "j2", "-n")
    assert r.returncode == 0 and "close 'j2'" in r.stdout and box.run("_complete", "close").stdout.count("j2") == 1


def test_help_pages_carry_examples(box):
    for verb in ("new", "open", "close", "ls", "project"):
        assert "examples:" in box.run(verb, "-h").stdout, verb
    assert "clanker open -" in box.run("help").stdout


def test_colour_only_on_a_terminal(tmp_path, monkeypatch):
    class Tty:
        def isatty(self):
            return True
    pal = tmp_path / "palette.json"
    pal.write_text(json.dumps({"accents": {"orange": {"hex": "#e78a4e"}, "green": {"hex": "#a9b665"}},
                               "greys": {"grey": {"hex": "#7c6f64"}}}))
    monkeypatch.setenv("CLANKER_PALETTE", str(pal))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert fleet.painter(open(os.devnull, "w")) is None              # not a terminal
    paint = fleet.painter(Tty())
    assert paint("decision ", "decision") == "\033[38;2;231;138;78mdecision \033[0m"   # the palette's orange
    monkeypatch.setenv("NO_COLOR", "1")
    assert fleet.painter(Tty()) is None
    rs = [{"name": "a", "panes": 1, "claude": 1, "slots": ["a3"], "state": "decision", "since_s": 5,
           "stuck": False, "attached": False, "dir": "~", "project": "x", "session_ids": []}]
    assert "\033[" in fleet.render_rows(rs, 0, paint=paint) and "\033[" not in fleet.render_rows(rs, 0)


def test_close_idle_lists_then_closes_once(box):
    import time
    assert box.run("new", "a3", "busy9", "-d", env={"FAKE_CLAUDE_STATUS": "busy"}).returncode == 0
    assert box.run("new", "a3", "idle9", "-d").returncode == 0
    for n in ("oldsh", "newsh"):
        assert box.run("new", "--shell", n, "-d").returncode == 0
    st_path = os.path.join(box.tmp, "tmux", "state.json")
    st = json.load(open(st_path))
    for n in ("busy9", "idle9", "oldsh"):
        st["sessions"][n]["activity"] = time.time() - 40 * 86400
    json.dump(st, open(st_path, "w"))
    r = box.run("close", "--idle", "30d", "-n")
    assert r.returncode == 0, r.stderr
    listed = [ln.split()[0] for ln in r.stdout.splitlines()[1:] if not ln.startswith("  skipped")]
    assert sorted(listed) == ["idle9", "oldsh"] and "skipped busy9: Claude is working" in r.stdout
    assert box.run("close", "--idle", "30d").returncode == 2          # a script needs -y
    r = box.run("close", "--idle", "30d", "--shells", "-y")
    assert r.returncode == 0 and "closed 1 session(s)" in r.stderr
    names = box.run("_complete", "close").stdout.split()
    assert "oldsh" not in names and {"idle9", "busy9", "newsh"} <= set(names)
    assert box.run("close", "--idle", "soon").returncode == 2


def test_project_noun_lists_projects_with_their_sessions(box):
    assert box.run("open", "bootproj", "a3", "-d").returncode == 0
    r = box.run("project")
    row = [ln for ln in r.stdout.splitlines() if ln.startswith("bootproj")][0]
    assert r.returncode == 0 and row.split()[1] == "1"
    assert box.run("project", "path", "bootproj").stdout.strip() == box.proj
    d = json.loads(box.run("project", "ls", "--json").stdout)
    assert d == [{"name": "bootproj", "path": box.proj, "claude_sessions": 1}]
    help_page = box.run("help").stdout
    assert "project [ls|create|adopt|remove|path]" in help_page and "\n  create" not in help_page


def test_open_dash_goes_back(box, monkeypatch):
    assert box.run("new", "a3", "one", "-d").returncode == 0
    assert box.run("new", "a3", "two", "-d").returncode == 0
    cli = _load_cli()
    monkeypatch.setenv("CLANKER_DATA", box.env["CLANKER_DATA"])
    monkeypatch.delenv("TMUX", raising=False)
    cli._remember_open("one")
    cli._remember_open("two")
    assert cli._history() == {"prev": "one", "cur": "two"}
    r = box.run("open", "-", "-d")
    assert r.returncode == 0 and r.stdout == "one\n", r.stderr
    cli._remember_open("gone")
    cli._remember_open("one")                               # prev is 'gone', which is not running
    r = box.run("open", "-")
    assert r.returncode == 1 and "'gone' is gone" in r.stderr


def test_completion_for_bash_zsh_and_fish(box):
    bash = box.run("completion").stdout
    zsh = box.run("completion", "zsh").stdout
    fish = box.run("completion", "fish").stdout
    assert "complete -o default -F _clanker_complete clanker" in bash
    assert "bashcompinit" in zsh and "_clanker_complete" in zsh
    assert "complete -c clanker" in fish and "clanker _complete (commandline -opc)[2..-1]" in fish
    assert set(box.run("_complete", "project").stdout.split()) == {"ls", "create", "adopt", "remove", "path"}


def test_the_project_cache_follows_the_registry(tmp_path, monkeypatch):
    import session_flow
    a, b = tmp_path / "pa", tmp_path / "pb"
    a.mkdir(); b.mkdir()
    reg = tmp_path / "reg.yaml"
    reg.write_text(f"projects:\n  pa:\n    path: {a}\n")
    monkeypatch.setenv("CLANKER_REGISTRY", str(reg))
    monkeypatch.setenv("CLANKER_PROJECT_ROOTS", str(tmp_path / "noroot"))
    monkeypatch.setenv("CLANKER_DATA", str(tmp_path / "data"))
    assert set(session_flow.projects()) == {"pa"}
    reg.write_text(f"projects:\n  pa:\n    path: {a}\n  pb:\n    path: {b}\n")     # an edit: seen at once
    assert set(session_flow.projects()) == {"pa", "pb"}
    b.rmdir()
    assert set(session_flow.projects()) == {"pa"}                                 # a gone dir: never listed


def test_the_login_check_is_shared_for_five_minutes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("CLANKER_DATA", str(tmp_path))
    monkeypatch.setenv("CLANKER_ACCOUNTS_CMD", "/bin/first")
    monkeypatch.setattr(accounts, "slots", lambda online=False, max_age=0: calls.append(online) or
                        [{"id": "a1", "status": "live", "plan": "pro", "usable": True, "why": "", "warnings": []}])
    assert accounts.checked_logins()["a1"]["plan"] == "pro"
    assert accounts.checked_logins()["a1"]["usable"] is True and calls == [True]   # the second read: the file
    monkeypatch.setenv("CLANKER_ACCOUNTS_CMD", "/bin/other")                       # another command: asked again
    accounts.checked_logins()
    assert calls == [True, True]
    assert accounts.checked_logins(max_age=0) and len(calls) == 3                  # expired: asked again


def test_tmux_add_with_a_slot_keeps_the_project_rule(box):
    r = box.run("tmux", "add", "loose2", "--path", box.tmp, "a3", cwd=box.tmp)
    assert r.returncode == 1 and "outside every project" in r.stderr
    assert box.records(box.a3_real) == [] and "loose2" not in box.run("ls", "-a").stdout
    r = box.run("tmux", "add", "inproj", "--path", box.proj, "a3")
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.records(box.a3_real)[0]["cwd"] == box.proj


def test_a_switch_never_clears_unsent_text_unasked(box):
    import subprocess
    assert box.run("new", "a1", "dsw", "-d").returncode == 0
    subprocess.run(["tmux", "send-keys", "-t", "=dsw:", "-l", "--", "half typed"], env=box.env, check=True)
    r = box.run("open", "dsw", "a3", "-d")
    assert r.returncode == 1 and "unsent text" in r.stderr and "half typed" in r.stderr
    assert len(box.records(box.a1)) == 1 and box.records(box.a3_real) == [] and \
        not [l for l in box.leases() if l.get("slot") == "a3" and not l.get("released")]
    r = box.run("open", "dsw", "a3", "-d", "-f")
    assert r.returncode == 0 and "the prompt box held text that the switch cleared" in r.stderr
    assert len(box.records(box.a3_real)) == 1


def test_the_ntfy_token_comes_from_a_command(tmp_path, monkeypatch):
    """2026-10-29: the token sat in a world-readable systemd drop-in (and in
    `systemctl show`). A command (the secret store's `get`) can hand it over instead."""
    import alerts
    alerts._token.clear()
    tool = tmp_path / "get-token"
    calls = tmp_path / "calls"
    tool.write_text(f"#!/bin/sh\necho x >> {calls}\nprintf 'tok-123\\n'\n")
    tool.chmod(0o755)
    monkeypatch.delenv("CLANKER_NTFY_TOKEN", raising=False)
    monkeypatch.setenv("CLANKER_NTFY_TOKEN_CMD", f"{tool} get ntfy")
    assert alerts.ntfy_token() == "tok-123" and alerts.ntfy_token() == "tok-123"
    assert calls.read_text().count("x") == 1                               # run once per process
    monkeypatch.setenv("CLANKER_NTFY_TOKEN", "from-env")
    assert alerts.ntfy_token() == "from-env"
    monkeypatch.delenv("CLANKER_NTFY_TOKEN")
    monkeypatch.setenv("CLANKER_NTFY_TOKEN_CMD", "/nonexistent/tool get")
    assert alerts.ntfy_token() == ""
