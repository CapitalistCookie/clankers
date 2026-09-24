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
    def __init__(self, rc=0, out=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def _tmux_resolve(target, alive):
    """Resolve a -t target the way tmux does: `=name` matches exactly; a bare
    name matches exactly, else by a unique prefix. None when nothing matches."""
    s = target.split(":", 1)[0]
    if s.startswith("="):
        return s[1:] if s[1:] in alive else None
    if s in alive:
        return s
    hits = [n for n in alive if n.startswith(s)]
    return hits[0] if len(hits) == 1 else None


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


def test_add_session_heals_default_shell_before_creating(startup, monkeypatch):
    """2026-09-03: a server born from cron hands out /bin/sh; add_session must
    reset default-shell to the login shell BEFORE the window is spawned."""
    import newsession
    monkeypatch.setattr(newsession, "login_shell", lambda: "/bin/bash")

    def run(argv, **kw):
        calls.append(list(argv))
        if "has-session" in argv:
            return FakeProc(1)
        if "show-options" in argv:
            return FakeProc(0, "/bin/sh\n")
        return FakeProc(0)
    monkeypatch.setattr(tmux_manager.subprocess, "run", run)

    tmux_manager.add_session("delta", "/proj/delta")
    heal = ["tmux", "set-option", "-g", "default-shell", "/bin/bash"]
    created = next(c for c in calls if "new-session" in c)
    assert heal in calls and calls.index(heal) < calls.index(created)
    assert '"delta:/proj/delta"' in startup.read_text()


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
    assert any("kill-session" in c and "=beta" in c for c in calls)


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


# ── resurrect (P10): registry∪map merge + relaunch-missing via the boot script ─

@pytest.fixture
def resurrect_env(startup, tmp_path, monkeypatch):
    """Registry: one mapped project (stale map path), one registered-but-unmapped
    project, one ghost path. Boot map: the stale entry + a non-registry manual
    entry."""
    proj = tmp_path / "regproj"
    proj.mkdir()
    newproj = tmp_path / "newproj"
    newproj.mkdir()
    reg = tmp_path / "registry.yaml"
    reg.write_text("projects:\n"
                   f"  regproj:\n    archetype: tool\n    path: {proj}\n"
                   f"  newproj:\n    archetype: tool\n    path: {newproj}\n"
                   "  ghostproj:\n    archetype: tool\n    path: /nonexistent/ghost\n")
    monkeypatch.setenv("CLANKER_REGISTRY", str(reg))
    # Pin discovery roots too: Registry.projects unions the yaml with repos
    # auto-discovered under CLANKER_PROJECT_ROOTS (default ~/projects), and its
    # 300s in-process cache means an earlier suite test can hand THIS test the
    # live box's project list (observed: sync-registry pulled 33 real repos).
    monkeypatch.setenv("CLANKER_PROJECT_ROOTS", str(tmp_path / "no-discovery"))
    tmux_manager.write_startup({"regproj": "/stale/old-path", "manual": "/p/manual"})
    calls.clear()
    return proj


def test_resurrect_refreshes_map_and_relaunches_missing(resurrect_env, startup, monkeypatch):
    states = [set(), {"regproj", "manual"}]          # before: all dead → after: all alive
    monkeypatch.setattr(tmux_manager, "_alive_sessions", lambda: states.pop(0))
    rc = tmux_manager.resurrect()
    text = startup.read_text()
    assert rc == 0
    assert f'"regproj:{resurrect_env}"' in text       # registry refreshed the stale path
    assert '"regproj:/stale/old-path"' not in text
    assert '"manual:/p/manual"' in text               # non-registry entry survives
    assert "newproj" not in text     # default NEVER grows the fleet (07-22 dry-run receipt)
    assert "ghostproj" not in text
    assert ["bash", tmux_manager.STARTUP_SCRIPT] in calls   # boot script executed


def test_resurrect_sync_registry_adds_unmapped_projects(resurrect_env, startup, monkeypatch):
    states = [set(), {"regproj", "manual", "newproj"}]
    monkeypatch.setattr(tmux_manager, "_alive_sessions", lambda: states.pop(0))
    assert tmux_manager.resurrect(sync_registry=True) == 0
    text = startup.read_text()
    assert '"newproj:' in text                        # opt-in union adds it
    assert "ghostproj" not in text                    # missing path still never mapped


def test_resurrect_dry_run_touches_nothing(resurrect_env, startup, monkeypatch):
    monkeypatch.setattr(tmux_manager, "_alive_sessions", lambda: set())
    before = startup.read_text()
    assert tmux_manager.resurrect(dry_run=True) == 0
    assert startup.read_text() == before
    assert ["bash", tmux_manager.STARTUP_SCRIPT] not in calls


def test_resurrect_nonzero_when_sessions_stay_dead(resurrect_env, monkeypatch, capsys):
    states = [set(), {"manual"}]                      # regproj never comes back
    monkeypatch.setattr(tmux_manager, "_alive_sessions", lambda: states.pop(0))
    assert tmux_manager.resurrect() == 1              # exit-code honesty (law 2)
    assert "regproj" in capsys.readouterr().err


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
           "CLANKER_PROJECT_ROOTS": str(tmp_path),   # no live ~/projects scan
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


# ── 2026-09-24: boot is claude-free — mapped sessions come back as PLAIN SHELLS ─

def test_boot_script_never_launches_claude(startup):
    """Operator rule 2026-09-24: boot, fleet-heal and resurrect recreate missing
    mapped sessions as plain shells; Claude starts only on demand (`clanker
    work` / `clanker tmux add`). No executable line may type or launch it."""
    tmux_manager.write_startup({"alpha": "/p/alpha", "beta": "/p/beta"})
    code = [ln for ln in startup.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    body = "\n".join(code)
    assert "tmux new-session -d" in body
    for banned in ("claude", "send-keys", "--resume", "accept-trust", "LAUNCH"):
        assert banned not in body, f"boot script still carries {banned!r}"


def test_boot_script_creates_only_missing_sessions_as_shells(tmp_path, monkeypatch):
    """Execute the GENERATED script against a fake tmux that resolves targets the
    way tmux does (`=name` exact, a bare name by prefix): the missing sessions
    are created at their dir — including `web` while `web-2` runs, which a bare
    `-t web` would have prefix-matched — the live one is left alone, and no key
    is ever sent."""
    import subprocess
    script = tmp_path / "tmux-startup.sh"
    monkeypatch.setattr(tmux_manager, "STARTUP_SCRIPT", str(script))
    tmux_manager.write_startup({"alive": str(tmp_path), "gone": str(tmp_path),
                                "web": str(tmp_path)})
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    log = tmp_path / "tmux.log"
    (fakebin / "tmux").write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> "{log}"\n'
        'if [ "$1" = has-session ]; then\n'
        '  for s in alive web-2; do\n'
        '    case "$3" in "=$s") exit 0;; =*) ;; *) case "$s" in "$3"*) exit 0;; esac;; esac\n'
        '  done\n'
        '  exit 1\n'
        'fi\n'
        "exit 0\n")
    (fakebin / "sleep").write_text("#!/bin/bash\nexit 0\n")
    for f in ("tmux", "sleep"):
        (fakebin / f).chmod(0o755)
    env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                       env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    lines = log.read_text().splitlines()
    assert f"new-session -d -s gone -c {tmp_path} -x 220 -y 50" in lines
    assert f"new-session -d -s web -c {tmp_path} -x 220 -y 50" in lines
    assert "has-session -t =web" in lines
    assert not any("-s alive" in ln for ln in lines)
    assert not any(ln.startswith("send-keys") for ln in lines)


# ── 2026-09-24: exact targets in add_session ──────────────────────────────────

def test_add_session_creates_clanker_while_clanker_41_runs(startup, monkeypatch, capsys):
    """A bare `has-session -t clanker` prefix-matches a running `clanker-41`
    (tmux resolves an exact name first, then a unique prefix), so add_session
    skipped creating `clanker`. The `=name` target matches exactly, and the
    keys go to `=clanker:`, never to `clanker-41`."""
    alive = ["clanker-41"]

    def run(argv, **kw):
        calls.append(list(argv))
        hit = _tmux_resolve(argv[argv.index("-t") + 1], alive) if "-t" in argv else None
        if "has-session" in argv:
            return FakeProc(0 if hit else 1)
        if "new-session" in argv:
            alive.append(argv[argv.index("-s") + 1])
        return FakeProc(0)

    monkeypatch.setattr(tmux_manager.subprocess, "run", run)
    tmux_manager.add_session("clanker", "/proj/clanker")
    assert "Created tmux session: clanker" in capsys.readouterr().out
    assert alive == ["clanker-41", "clanker"]
    assert ["tmux", "has-session", "-t", "=clanker"] in calls
    keys = [c for c in calls if "send-keys" in c]
    assert len(keys) == 2 and all(c[c.index("-t") + 1] == "=clanker:" for c in keys)


def test_remove_session_never_kills_a_prefix_match(startup, monkeypatch):
    """A bare `kill-session -t clanker` prefix-matches: with no `clanker`
    running, `clanker tmux remove clanker` killed `clanker-41`. The `=name`
    target matches exactly, so a missing name kills nothing."""
    alive = ["clanker-41"]

    def run(argv, **kw):
        calls.append(list(argv))
        if "kill-session" in argv:
            hit = _tmux_resolve(argv[argv.index("-t") + 1], alive)
            if not hit:
                return FakeProc(1)
            alive.remove(hit)
        return FakeProc(0)

    monkeypatch.setattr(tmux_manager.subprocess, "run", run)
    tmux_manager.write_startup({"clanker": "/p/clanker", "clanker-41": "/p/c41"})
    tmux_manager.remove_session("clanker")
    assert alive == ["clanker-41"]
    assert ["tmux", "kill-session", "-t", "=clanker"] in calls
    text = startup.read_text()
    assert '"clanker:' not in text and '"clanker-41:/p/c41"' in text
