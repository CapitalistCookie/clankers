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


# ── 2026-09-07: the workspace-trust dialog (2.1.263 highlights "No, exit" first) ──
DIALOG_NO_FIRST = ("Quick safety check: Is this a project you created or one you trust?\n"
                   " ❯ No, exit\n   Yes, I trust this folder\n Enter to confirm")
DIALOG_YES_ON = ("Quick safety check: Is this a project you created or one you trust?\n"
                 "   No, exit\n ❯ Yes, I trust this folder\n Enter to confirm")
DIALOG_OLD = "Do you trust the files in this folder?\n ❯ Yes, proceed\n   No, exit"


def test_trust_cursor_reads_the_layout():
    assert newsession._trust_cursor("$ ") is None
    assert newsession._trust_cursor(DIALOG_NO_FIRST) == "down"
    assert newsession._trust_cursor(DIALOG_YES_ON) == "yes"
    assert newsession._trust_cursor(DIALOG_OLD) == "yes"
    assert newsession._trust_cursor("Quick safety check\n   (rendering)") == "unknown"


def test_accept_trust_moves_cursor_to_yes_before_enter(monkeypatch):
    monkeypatch.setattr(newsession.time, "sleep", lambda s: None)
    state = {"screen": DIALOG_NO_FIRST}
    keys = []

    def run(argv, **kw):
        if "capture-pane" in argv:
            return FakeProc(0, state["screen"])
        if "send-keys" in argv:
            keys.append(argv[-1])
            if argv[-1] == "Down":
                state["screen"] = DIALOG_YES_ON
            if argv[-1] == "Enter":
                state["screen"] = "❯ "
        return FakeProc(0)
    monkeypatch.setattr(newsession.subprocess, "run", run)
    assert newsession.accept_trust_prompt("s", timeout=5) is True
    assert keys == ["Down", "Enter"]


def test_accept_trust_never_confirms_while_cursor_is_on_no(monkeypatch):
    """Mutation check for the old behaviour (a bare Enter = 'No, exit')."""
    monkeypatch.setattr(newsession.time, "sleep", lambda s: None)
    keys = []

    def run(argv, **kw):
        if "capture-pane" in argv:
            return FakeProc(0, DIALOG_NO_FIRST)      # the cursor never moves
        if "send-keys" in argv:
            keys.append(argv[-1])
        return FakeProc(0)
    monkeypatch.setattr(newsession.subprocess, "run", run)
    assert newsession.accept_trust_prompt("s", timeout=0.4) is False
    assert "Enter" not in keys and "Down" in keys


def test_accept_trust_prompts_skips_registered_and_missing_panes(monkeypatch, tmp_path):
    monkeypatch.setattr(newsession.time, "sleep", lambda s: None)
    monkeypatch.setenv("CLANKER_CLAUDE_SESSIONS_DIR", str(tmp_path))
    (tmp_path / "1.json").write_text('{"pid": 1, "tmux": "up:@1.%1", "status": "idle"}')
    screens = {"parked": DIALOG_YES_ON}
    keys = []

    def run(argv, **kw):
        name = argv[argv.index("-t") + 1] if "-t" in argv else None
        if "capture-pane" in argv:
            return FakeProc(0, screens[name]) if name in screens else FakeProc(1)
        if "send-keys" in argv:
            keys.append((name, argv[-1]))
            screens["parked"] = "❯ "
        return FakeProc(0)
    monkeypatch.setattr(newsession.subprocess, "run", run)
    assert newsession.accept_trust_prompts(["up", "parked", "gone"], timeout=5) == ["parked"]
    assert keys == [("parked", "Enter")]


# ── 2026-09-24: `work` starts claude in a mapped session boot left as a shell ──

def _run_existing(pane_cmd):
    def run(argv, **kw):
        calls.append(list(argv))
        if "has-session" in argv:
            return FakeProc(rc=0)                        # the session exists
        if "display-message" in argv:
            return FakeProc(rc=0, out=pane_cmd + "\n")   # its active pane runs this
        if "capture-pane" in argv:
            return FakeProc(rc=0, out="$ ")
        return FakeProc(rc=0)
    return run


def test_work_starts_claude_in_existing_plain_shell_session(monkeypatch):
    """Boot and fleet-heal recreate mapped sessions as plain shells (Claude only
    on demand), so the `work` path must start Claude in one: cd-prefixed,
    resumed when asked, exact-match target, trust dialog still handled."""
    trusted = []
    monkeypatch.setattr(newsession.subprocess, "run", _run_existing("bash"))
    monkeypatch.setattr(newsession, "accept_trust_prompt", lambda n: trusted.append(n))
    name, msg = newsession.spawn(name="proj", cwd="/tmp/proj", resume="abc-123",
                                 start_claude_in_shell=True)
    assert name == "proj" and "started claude" in msg
    typed = [c for c in calls if "send-keys" in c and "-l" in c]
    assert len(typed) == 1
    assert typed[0][-1] == newsession.launch_cmd("/tmp/proj", resume="abc-123")
    assert "=proj:" in typed[0]
    assert trusted == ["proj"]
    assert not any("new-session" in c for c in calls)


def test_work_never_types_into_an_existing_busy_session(monkeypatch):
    monkeypatch.setattr(newsession.subprocess, "run", _run_existing("claude"))
    name, msg = newsession.spawn(name="proj", cwd="/tmp/proj", start_claude_in_shell=True)
    assert "already exists" in msg
    assert not any("send-keys" in c for c in calls)


def test_new_on_existing_plain_shell_session_only_points_at_it(monkeypatch):
    """`clanker new <name>` keeps its contract: an existing name is never typed into."""
    monkeypatch.setattr(newsession.subprocess, "run", _run_existing("bash"))
    name, msg = newsession.spawn(name="proj", shell=False)
    assert "already exists" in msg
    assert not any("send-keys" in c for c in calls)


def test_work_cli_starts_claude_in_existing_plain_shell_end_to_end(tmp_path):
    """The real `clanker work X` entry point against a fake tmux whose session X
    exists as a bare shell: Claude is typed in exactly once (cd-prefixed,
    exact-match target) and the trust dialog is confirmed on Yes."""
    proj = tmp_path / "bootproj"
    proj.mkdir()
    reg = tmp_path / "registry.yaml"
    reg.write_text("projects:\n  bootproj:\n    archetype: tool\n"
                   f"    path: {proj}\n")
    log, state = tmp_path / "tmux.log", tmp_path / "capture.n"
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "tmux").write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> "{log}"\n'
        'case "$1" in\n'
        "  has-session) exit 0;;\n"
        "  display-message) echo bash;;\n"
        "  capture-pane)\n"
        f'    n=$(cat "{state}" 2>/dev/null || echo 0); echo $((n+1)) > "{state}"\n'
        "    if [ \"$n\" = 0 ]; then echo '$ '; else\n"
        "      printf 'Do you trust the files in this folder?\\n❯ Yes, I trust this folder\\n  No, exit\\n'; fi;;\n"
        "esac\nexit 0\n")
    (fakebin / "tmux").chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path), "CLANKER_REGISTRY": str(reg),
           "CLANKER_PROJECT_ROOTS": str(tmp_path), "CLANKER_DATA": str(tmp_path / "data"),
           "PATH": f"{fakebin}:{os.environ['PATH']}"}
    env.pop("TMUX", None)
    r = _REAL_RUN([os.path.join(_REPO, "bin", "clanker"), "work", "bootproj", "--no-attach"],
                  capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "started claude in existing session 'bootproj'" in r.stdout
    lines = log.read_text().splitlines()
    typed = [ln for ln in lines if ln.startswith("send-keys") and " -l -- " in ln]
    assert len(typed) == 1 and "-t =bootproj:" in typed[0]
    assert typed[0].endswith(f"cd {proj} && {newsession.LAUNCH}")
    assert sum(1 for ln in lines if ln.startswith("send-keys") and ln.endswith("Enter")) == 2
    assert not any(ln.startswith("new-session") for ln in lines)


# ── 2026-09-24: exact session targets — `clanker` must never be `clanker-41` ──

def _tmux_like_run(alive):
    """subprocess.run stand-in that resolves `-t` like tmux: `=name` matches
    exactly; a bare name matches exactly, else by a unique prefix. Logs a
    RESOLVED row for each target it resolves."""
    def resolve(target):
        s = target.split(":", 1)[0]
        if s.startswith("="):
            return s[1:] if s[1:] in alive else None
        if s in alive:
            return s
        hits = [n for n in alive if n.startswith(s)]
        return hits[0] if len(hits) == 1 else None

    def run(argv, **kw):
        calls.append(list(argv))
        hit = resolve(argv[argv.index("-t") + 1]) if "-t" in argv else None
        if hit:
            calls.append(["RESOLVED", argv[1], hit])
        if "has-session" in argv:
            return FakeProc(rc=0 if hit else 1)
        if "new-session" in argv:
            alive.append(argv[argv.index("-s") + 1])
        return FakeProc(rc=0)
    return run


def test_session_exists_matches_the_exact_name_only(monkeypatch):
    monkeypatch.setattr(newsession.subprocess, "run", _tmux_like_run(["clanker-41"]))
    assert newsession._session_exists("clanker") is False
    assert newsession._session_exists("clanker-41") is True
    assert ["tmux", "has-session", "-t", "=clanker"] in calls
    assert newsession._unique_name("clanker") == "clanker"


def test_work_clanker_never_resolves_to_clanker_41_end_to_end(tmp_path):
    """The real `clanker work clanker` entry point against a fake tmux that
    resolves targets like tmux, with only `clanker-41` running. A bare
    `has-session -t clanker` prefix-matched `clanker-41`: work reported the
    session as present and never created `clanker`. Now `clanker` is created
    and no command resolves to `clanker-41`."""
    proj = tmp_path / "clanker"
    proj.mkdir()
    reg = tmp_path / "registry.yaml"
    reg.write_text("projects:\n  clanker:\n    archetype: tool\n"
                   f"    path: {proj}\n")
    log, alive, cap = tmp_path / "tmux.log", tmp_path / "alive", tmp_path / "capture.n"
    alive.write_text("clanker-41\n")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "tmux").write_text(
        "#!/bin/bash\n"
        f'log="{log}"; alive="{alive}"; cap="{cap}"\n'
        'echo "$*" >> "$log"\n'
        't=""; prev=""; for a in "$@"; do [ "$prev" = "-t" ] && t="$a"; prev="$a"; done\n'
        'resolve() {  # like tmux: =name exact; a bare name exact, else unique prefix\n'
        '  local s="${1%%:*}" names m\n'
        '  names=$(cat "$alive")\n'
        '  case "$s" in =*) grep -qxF -- "${s#=}" <<<"$names" && echo "${s#=}"; return;; esac\n'
        '  if grep -qxF -- "$s" <<<"$names"; then echo "$s"; return; fi\n'
        '  m=$(while read -r n; do case "$n" in "$s"*) echo "$n";; esac; done <<<"$names")\n'
        '  [ "$(grep -c . <<<"$m")" = 1 ] && echo "$m"\n'
        '}\n'
        'r=""; [ -n "$t" ] && r=$(resolve "$t")\n'
        '[ -n "$r" ] && echo "RESOLVED $1 $r" >> "$log"\n'
        'case "$1" in\n'
        '  has-session) [ -n "$r" ] || exit 1;;\n'
        '  new-session) prev=""; for a in "$@"; do [ "$prev" = "-s" ] && echo "$a" >> "$alive"; prev="$a"; done;;\n'
        '  display-message) [ -n "$r" ] || exit 1; echo bash;;\n'
        '  capture-pane)\n'
        '    [ -n "$r" ] || exit 1\n'
        '    n=$(cat "$cap" 2>/dev/null || echo 0); echo $((n+1)) > "$cap"\n'
        "    if [ \"$n\" = 0 ]; then echo '$ '; else\n"
        "      printf 'Do you trust the files in this folder?\\n❯ Yes, I trust this folder\\n  No, exit\\n'; fi;;\n"
        'esac\n'
        'exit 0\n')
    (fakebin / "tmux").chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path), "CLANKER_REGISTRY": str(reg),
           "CLANKER_PROJECT_ROOTS": str(tmp_path), "CLANKER_DATA": str(tmp_path / "data"),
           "PATH": f"{fakebin}:{os.environ['PATH']}"}
    env.pop("TMUX", None)
    r = _REAL_RUN([os.path.join(_REPO, "bin", "clanker"), "work", "clanker", "--no-attach"],
                  capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "session 'clanker'" in r.stdout and "already exists" not in r.stdout
    lines = log.read_text().splitlines()
    assert "has-session -t =clanker" in lines
    assert any(ln.startswith("new-session -d -s clanker ") for ln in lines)
    assert not [ln for ln in lines if ln.startswith("RESOLVED") and ln.endswith(" clanker-41")]
    assert alive.read_text().split() == ["clanker-41", "clanker"]
