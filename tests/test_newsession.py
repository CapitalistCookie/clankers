"""Hermetic tests for lib/newsession.py (`clanker new` / `clanker open`).

subprocess.run and os.execvp are replaced by recorders — no real tmux is ever
invoked and nothing is spawned. The integration path (real tmux, fake claude)
was verified separately on a disposable container.
"""
import os
import sys

import pytest

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
