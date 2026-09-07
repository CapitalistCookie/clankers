"""Hermetic tests for lib/newsession.py (`clanker new` / `clanker open`).

subprocess.run and os.execvp are replaced by recorders — no real tmux is ever
invoked and nothing is spawned. The integration path (real tmux, fake claude)
was verified separately on a disposable container.
"""
import os
import shutil
import subprocess
import sys
import time

import pytest

_REAL_RUN = subprocess.run   # grabbed before the autouse fixture fakes it

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "lib"))
import newsession  # noqa: E402

calls = []


class FakeProc:
    def __init__(self, rc=0, out=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def fake_run(argv, **kw):
    calls.append(list(argv))
    if "has-session" in argv:
        return FakeProc(rc=1)            # nothing exists yet
    if "capture-pane" in argv:
        return FakeProc(rc=0, out="$ ")  # shell prompt ready, no trust dialog
    return FakeProc(rc=0)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    calls.clear()
    monkeypatch.setattr(newsession, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(newsession, "_LAST_FILE", str(tmp_path / ".last_new_session"))
    monkeypatch.setattr(newsession.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(newsession.subprocess, "run", fake_run)
    monkeypatch.setattr(newsession.os, "execvp",
                        lambda *a: calls.append(["EXECVP", *a[1]]))


def test_slug_sanitizes():
    assert newsession._slug("My Cool Idea!!") == "My-Cool-Idea"
    assert newsession._slug("") == "claude"
    assert newsession._slug("--rm -rf") == "rm--rf"


def test_spawn_launches_claude_with_flags():
    name, msg = newsession.spawn(name="project", shell=False)
    assert name == "project"
    assert any("new-session" in c and "project" in c for c in calls)
    assert newsession.LAUNCH == \
        "CLAUDE_CODE_DISABLE_SANDBOX=1 claude --dangerously-skip-permissions"
    # fleet-regression law (2026-07-05): the typed line MUST cd into the project
    # dir first — a pane re-homed by restores/keepalives still lands in-repo.
    sent = [c for c in calls if "send-keys" in c and "-l" in c]
    assert sent and any(newsession.LAUNCH in arg and arg.startswith("cd ")
                        for c in sent for arg in c if isinstance(arg, str))
    assert any("send-keys" in c and c[-1] == "Enter" for c in calls)


def test_launch_cmd_cd_prefix_and_resume():
    lc = newsession.launch_cmd("/tmp/some repo")
    assert lc.startswith("cd '/tmp/some repo' && ") and lc.endswith(newsession.LAUNCH)
    assert newsession.launch_cmd("/x", resume=True).endswith("--resume")
    assert newsession.launch_cmd("/x", resume="abc-123").endswith("--resume abc-123")


def test_spawn_waits_for_prompt_and_polls_trust():
    newsession.spawn(name="x", shell=False)
    assert any("capture-pane" in c for c in calls)


def test_spawn_shell_skips_claude():
    name, msg = newsession.spawn(name="scratch", shell=True)
    assert name == "scratch"
    assert not any("claude" in " ".join(c) for c in calls)
    assert "shell" in msg


def test_last_session_recorded_and_used_by_attach():
    newsession.spawn(name="scratch", shell=True)
    assert newsession.read_last() == "scratch"

    def run_exists(argv, **kw):
        calls.append(list(argv))
        return FakeProc(rc=0)
    newsession.subprocess.run = run_exists
    ok, msg = newsession.attach(name=None)
    assert ok
    assert "scratch" in msg or any("scratch" in " ".join(c) for c in calls)


def test_spawn_refuses_existing_name():
    def run_exists(argv, **kw):
        calls.append(list(argv))
        return FakeProc(rc=0)   # has-session: exists
    newsession.subprocess.run = run_exists
    name, msg = newsession.spawn(name="project", shell=False)
    assert "already exists" in msg
    assert "open project" in msg
    assert not any("new-session" in c for c in calls)


def test_attach_missing_session():
    ok, msg = newsession.attach(name="nope")
    assert not ok
    assert "no session named 'nope'" in msg


def test_attach_without_tmux_installed(monkeypatch):
    monkeypatch.setattr(newsession.shutil, "which", lambda _: None)
    ok, msg = newsession.attach(name="x")
    assert not ok and "tmux" in msg
    name, msg = newsession.spawn(name="x")
    assert name is None and "tmux" in msg


# ── login shell (2026-09-03: `clanker new` landed at a bare `$`) ─────────────
# tmux copies $SHELL into default-shell once at server start; the */3 cron
# keepalive restarted the server with SHELL=/bin/sh, so every new window was a
# login dash with no .bashrc (no claude on PATH) until the operator `su`-ed back
# into a bash. spawn() must heal the running server AND pin SHELL for a server
# it starts itself.

def _run_with_default_shell(value, rc=0):
    """A recorder whose tmux reports `value` for show-options default-shell."""
    def run(argv, **kw):
        calls.append(list(argv))
        run.kwargs[tuple(argv)] = kw
        if "has-session" in argv:
            return FakeProc(rc=1)
        if "show-options" in argv:
            return FakeProc(rc=rc, out=value)
        if "capture-pane" in argv:
            return FakeProc(rc=0, out="$ ")
        return FakeProc(rc=0)
    run.kwargs = {}
    return run


def test_login_shell_prefers_passwd_then_env_then_sh(monkeypatch):
    class PW:
        pw_shell = "/bin/from-passwd"
    monkeypatch.setattr(newsession.pwd, "getpwuid", lambda uid: PW())
    monkeypatch.setenv("SHELL", "/bin/from-env")
    usable = {"/bin/from-passwd", "/bin/from-env"}
    monkeypatch.setattr(newsession, "_usable_shell", lambda p: p in usable)
    assert newsession.login_shell() == "/bin/from-passwd"
    usable.discard("/bin/from-passwd")           # passwd shell missing/non-exec
    assert newsession.login_shell() == "/bin/from-env"
    usable.clear()
    assert newsession.login_shell() == "/bin/sh"
    # a relative/garbage $SHELL is never trusted
    monkeypatch.setattr(newsession, "_usable_shell",
                        lambda p: bool(p) and os.path.isabs(p) and p in {"/bin/from-passwd"})
    monkeypatch.setenv("SHELL", "bash")
    monkeypatch.setattr(newsession.pwd, "getpwuid", lambda uid: (_ for _ in ()).throw(KeyError(uid)))
    assert newsession.login_shell() == "/bin/sh"


def test_spawn_heals_wrong_default_shell_before_spawning(monkeypatch):
    monkeypatch.setattr(newsession, "login_shell", lambda: "/bin/bash")
    run = _run_with_default_shell("/bin/sh\n")
    monkeypatch.setattr(newsession.subprocess, "run", run)
    name, msg = newsession.spawn(name="proj", shell=True)
    assert name == "proj"
    heal = ["tmux", "set-option", "-g", "default-shell", "/bin/bash"]
    created = next(c for c in calls if "new-session" in c)
    assert heal in calls
    assert calls.index(heal) < calls.index(created)   # healed BEFORE the window exists
    # a server born from this call must not inherit a polluted SHELL either
    assert run.kwargs[tuple(created)]["env"]["SHELL"] == "/bin/bash"
    assert "default-shell was /bin/sh" in msg and "/bin/bash" in msg   # the heal is reported


def test_spawn_leaves_right_default_shell_alone(monkeypatch):
    monkeypatch.setattr(newsession, "login_shell", lambda: "/bin/bash")
    run = _run_with_default_shell("/bin/bash\n")
    monkeypatch.setattr(newsession.subprocess, "run", run)
    name, msg = newsession.spawn(name="proj", shell=True)
    assert name == "proj"
    assert not any("set-option" in c for c in calls)
    assert "default-shell" not in msg


def test_spawn_without_server_skips_heal_but_pins_shell(monkeypatch):
    monkeypatch.setattr(newsession, "login_shell", lambda: "/bin/bash")
    run = _run_with_default_shell("", rc=1)      # "no server running"
    monkeypatch.setattr(newsession.subprocess, "run", run)
    name, msg = newsession.spawn(name="proj", shell=True)
    assert name == "proj"
    assert not any("set-option" in c for c in calls)
    created = next(c for c in calls if "new-session" in c)
    assert run.kwargs[tuple(created)]["env"]["SHELL"] == "/bin/bash"


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs a real tmux")
def test_real_tmux_server_born_from_bare_env_is_healed(tmp_path, monkeypatch):
    """END-TO-END against a REAL, private tmux server. TMUX_TMPDIR isolates the
    socket and -f /dev/null skips ~/.tmux.conf (so continuum can neither restore
    the live fleet into this server nor overwrite the live resurrect save); the
    live server is never touched, and kill-server is refused unless the socket
    is under tmp_path. Reproduces 2026-09-03 exactly — a server started with
    SHELL=/bin/sh hands out /bin/sh — then proves one tmux_new_session() later
    the option is the login shell, the new pane's PROCESS is that shell, and
    even a plain `tmux new-session` from the bad env is right afterwards. The
    fake-run tests cannot prove tmux honours set-option -g for later windows;
    this does."""
    shell = newsession.login_shell()
    if shell == "/bin/sh":
        pytest.skip("login shell is /bin/sh — nothing to distinguish")
    # A unix socket path is capped near 108 bytes and tmux SILENTLY falls back
    # to the default socket (= the LIVE server) when TMUX_TMPDIR pushes it past
    # that (observed 2026-09-03 with a 121-byte path) — so use a short private
    # dir and refuse to run rather than risk touching the fleet.
    import tempfile
    sockdir = tempfile.mkdtemp(prefix="clanker-tmux-")
    if len(os.path.join(sockdir, f"tmux-{os.getuid()}", "default")) > 90:
        shutil.rmtree(sockdir, ignore_errors=True)
        pytest.skip("temp dir too long for a private tmux socket")
    monkeypatch.setenv("TMUX_TMPDIR", sockdir)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(newsession.subprocess, "run", _REAL_RUN)
    bad_env = {**os.environ, "SHELL": "/bin/sh"}

    def tmux(*args, env=None):
        return _REAL_RUN(["tmux", *args], capture_output=True, text=True,
                         timeout=10, env=env)

    def pane_cmd(session):
        for _ in range(40):                     # exec happens just after fork
            out = tmux("display-message", "-p", "-t", session,
                       "#{pane_current_command}").stdout.strip()
            if out and out != "tmux":
                return out
            time.sleep(0.05)
        return out

    r = tmux("-f", "/dev/null", "new-session", "-d", "-s", "born-bare",
             "-c", str(tmp_path), env=bad_env)
    assert r.returncode == 0, r.stderr
    try:
        sock = tmux("display-message", "-p", "#{socket_path}").stdout.strip()
        assert sock.startswith(str(sockdir)), sock          # private server only
        assert tmux("show-options", "-gv", "default-shell").stdout.strip() == "/bin/sh"
        assert pane_cmd("born-bare") == "sh"                 # the bug, reproduced

        r, healed = newsession.tmux_new_session("probe", str(tmp_path))
        assert r.returncode == 0, r.stderr
        assert healed == "/bin/sh"
        assert tmux("show-options", "-gv", "default-shell").stdout.strip() == shell
        assert pane_cmd("probe") == os.path.basename(shell)

        # the heal is server-wide: a plain tmux client from the bad env is fine now
        r = tmux("new-session", "-d", "-s", "plain", "-c", str(tmp_path), env=bad_env)
        assert r.returncode == 0, r.stderr
        assert pane_cmd("plain") == os.path.basename(shell)
        r, healed = newsession.tmux_new_session("probe2", str(tmp_path))
        assert r.returncode == 0 and healed is None          # already right: no-op
    finally:
        sock = tmux("display-message", "-p", "#{socket_path}").stdout.strip()
        if sock.startswith(str(sockdir)):
            tmux("kill-server")
        shutil.rmtree(sockdir, ignore_errors=True)
    assert tmux("has-session", "-t", "probe").returncode != 0   # private server gone
