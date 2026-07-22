"""Hermetic tests for lib/tmux_manager.py.

NEVER touches real tmux or the real ~/.tmux-startup.sh:
  - STARTUP_SCRIPT (a module constant, not env-driven) is monkeypatched to a temp file.
  - subprocess.run is replaced by a recorder that returns canned returncodes, so
    tmux is never actually invoked.

The behaviours under test are the two the 2026-07-05 fleet-regression postmortem
turned into invariants: add_session UPSERTS the boot entry (never skips a stale
one), remove_session deletes only its own line, and write preserves the rest.
"""
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "lib"))
import tmux_manager  # noqa: E402

calls = []


class FakeProc:
    def __init__(self, rc=0):
        self.returncode = rc


def _fake_run(argv, **kw):
    calls.append(list(argv))
    if "has-session" in argv:
        return FakeProc(1)   # session does not exist yet -> add_session creates
    return FakeProc(0)


@pytest.fixture
def startup(tmp_path, monkeypatch):
    calls.clear()
    path = tmp_path / "tmux-startup.sh"
    monkeypatch.setattr(tmux_manager, "STARTUP_SCRIPT", str(path))
    monkeypatch.setattr(tmux_manager.subprocess, "run", _fake_run)
    return path


def test_add_session_creates_and_writes_boot_entry(startup):
    tmux_manager.add_session("alpha", "/proj/alpha")
    text = startup.read_text()
    assert '"alpha:/proj/alpha"' in text
    assert any("has-session" in c for c in calls)
    assert any("new-session" in c for c in calls)   # did not exist -> created


def test_add_session_upserts_boot_entry(startup):
    tmux_manager.add_session("alpha", "/proj/one")
    tmux_manager.add_session("alpha", "/proj/two")
    text = startup.read_text()
    # the stale path is gone and only the new mapping remains (root cause #1 fix).
    assert '"alpha:/proj/two"' in text
    assert '"alpha:/proj/one"' not in text
    assert text.count('"alpha:') == 1


def test_add_session_existing_registers_boot_without_creating(startup, monkeypatch):
    def run_exists(argv, **kw):
        calls.append(list(argv))
        return FakeProc(0)   # has-session -> already exists
    monkeypatch.setattr(tmux_manager.subprocess, "run", run_exists)

    tmux_manager.add_session("beta", "/proj/beta")
    assert '"beta:/proj/beta"' in startup.read_text()   # still registered for boot
    assert not any("new-session" in c for c in calls)    # but not recreated


def test_add_session_defaults_path_to_projects_dir(startup):
    tmux_manager.add_session("gamma")
    expected = os.path.abspath(os.path.expanduser("~/projects/gamma"))
    assert f'"gamma:{expected}"' in startup.read_text()


def test_remove_session_deletes_entry_and_preserves_others(startup):
    tmux_manager.write_startup({"alpha": "/p/alpha", "beta": "/p/beta", "gamma": "/p/gamma"})
    calls.clear()
    tmux_manager.remove_session("beta")
    text = startup.read_text()
    assert '"beta:' not in text
    assert '"alpha:/p/alpha"' in text      # unrelated lines preserved
    assert '"gamma:/p/gamma"' in text
    assert any("kill-session" in c and "beta" in c for c in calls)


def test_remove_session_not_in_startup(startup, capsys):
    tmux_manager.write_startup({"alpha": "/p/alpha"})
    calls.clear()
    tmux_manager.remove_session("ghost")
    out = capsys.readouterr().out
    assert "Not found in startup script" in out
    assert any("kill-session" in c for c in calls)         # kill still attempted
    assert '"alpha:/p/alpha"' in startup.read_text()       # untouched


def test_write_and_read_roundtrip(startup):
    tmux_manager.write_startup({"z": "/p/z", "a": "/p/a"})
    assert tmux_manager.startup_entries() == {"z": "/p/z", "a": "/p/a"}
    text = startup.read_text()
    assert text.startswith("#!/bin/bash")
    assert "tmux new-session" in text  # footer boot loop present


# ── ensure_boot_entry (P3, audit M1): boot-map upsert with NO tmux calls ─────

def test_ensure_boot_entry_writes_and_is_idempotent(startup):
    assert tmux_manager.ensure_boot_entry("delta", "/proj/delta") is True
    assert '"delta:/proj/delta"' in startup.read_text()
    assert not calls                                    # never touched tmux
    # unchanged mapping -> no rewrite (returns False, file untouched)
    before = startup.read_text()
    assert tmux_manager.ensure_boot_entry("delta", "/proj/delta") is False
    assert startup.read_text() == before


def test_ensure_boot_entry_upserts_and_preserves_others(startup):
    tmux_manager.write_startup({"alpha": "/p/alpha", "delta": "/p/old"})
    assert tmux_manager.ensure_boot_entry("delta", "/p/new") is True
    text = startup.read_text()
    assert '"delta:/p/new"' in text and '"delta:/p/old"' not in text
    assert '"alpha:/p/alpha"' in text


def test_work_registers_boot_entry_end_to_end(tmp_path):
    """`clanker work X --no-attach` must land X in the boot map (M1: sessions
    that aren't in the map die with the box — proven by the 07-22 OOM), and
    --no-boot must opt out. Hermetic: HOME→tmp (so ~/.tmux-startup.sh is the
    tmp copy), a fake tmux on PATH (has-session=absent, everything else ok),
    and a tmp registry with an explicit project path."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proj = tmp_path / "bootproj"
    proj.mkdir()
    reg = tmp_path / "registry.yaml"
    reg.write_text("projects:\n  bootproj:\n    archetype: tool\n"
                   f"    path: {proj}\n")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "tmux").write_text('#!/bin/bash\ncase "$1" in\n'
                                  '  has-session) exit 1;;\n  *) exit 0;;\nesac\n')
    (fakebin / "tmux").chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path), "CLANKER_REGISTRY": str(reg),
           "PATH": f"{fakebin}:{os.environ['PATH']}"}

    r = subprocess.run([os.path.join(repo, "bin", "clanker"), "work", "bootproj",
                        "--shell", "--no-attach"],
                       capture_output=True, text=True, env=env, timeout=30)
    boot = tmp_path / ".tmux-startup.sh"
    assert r.returncode == 0, r.stdout + r.stderr
    assert boot.exists(), "work did not write the boot map"
    assert f'"bootproj:{proj}"' in boot.read_text()

    boot.unlink()
    r = subprocess.run([os.path.join(repo, "bin", "clanker"), "work", "bootproj",
                        "--shell", "--no-attach", "--no-boot"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not boot.exists(), "--no-boot still wrote the boot map"
