"""Scheduled-work inventory + decommission gate.

Regression anchor (2026-08-12): /etc/cron.d/yon-ci-nightly ran a 16-worker
pytest suite every 06:00 for 48 days after the yon repo's last commit and 38
days after the project was declared dead, because `clanker remove` cleaned only
clanker's own bookkeeping and nothing on the machine.

The single most dangerous behaviour here is OWNERSHIP MATCHING: a substring
match on "yon" also hits "yonmusic" and "yonlaptop-setup", both of which are
live projects. Those cases are asserted first and hardest.
"""
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from schedules import (disable_commands, for_project, owns,  # noqa: E402
                       paths_in, plan, scan, unowned)

# A fake operator home: the module's path-boundary logic is exercised on paths
# that never touch this machine (publint: no operator home in code), and HOME
# is pinned per test so `~` expands to it (unowned()'s default roots, owns()).
H = "/home/dev"


@pytest.fixture(autouse=True)
def _fake_home(monkeypatch):
    monkeypatch.setenv("HOME", H)


YON = f"{H}/yon"
YON_CI = (f"0 6 * * * user cd {H}/yon && CI_FULL_ALL=1 "
          f"live/ci/ci_full.sh nightly >> {H}/yon/.git/ci/nightly.log 2>&1")
YONMUSIC = (f"0 9 * * 1 {H}/projects/yonmusic/.venv/bin/python -u "
            f"{H}/projects/yonmusic/scripts/trend_monitor.py")


# ── ownership boundaries: the bug this module exists to prevent ──────────────
def test_owns_exact_path():
    assert owns(f"cd {H}/yon && ./x.sh", YON)


def test_owns_path_under_project():
    assert owns(f"python3 {H}/yon/scripts/toxicflow/shadow.py", YON)


def test_does_not_own_sibling_with_shared_prefix():
    """`yon` must NOT claim `yonmusic` — both are real, only one is dead."""
    assert not owns(YONMUSIC, YON)


def test_does_not_own_yonlaptop_setup():
    assert not owns(f"cd {H}/projects/yonlaptop-setup && make", YON)


def test_does_not_own_bare_name_mention():
    """The word 'yon' in prose is not ownership."""
    assert not owns("echo 'yon is retired' >> /tmp/notes.txt", YON)


def test_owns_tilde_path():
    assert owns("cd ~/yon && ./ci.sh", YON)


def test_owns_empty_project_path_is_false():
    assert not owns(f"cd {H}/yon", "")


def test_paths_in_extracts_absolute_paths():
    got = paths_in(f"cd {H}/yon && python3 /usr/bin/x.py")
    assert f"{H}/yon" in got and "/usr/bin/x.py" in got


# ── cron.d parsing ───────────────────────────────────────────────────────────
def _crond(tmp_path, files):
    d = tmp_path / "cron.d"
    d.mkdir()
    for name, body in files.items():
        (d / name).write_text(textwrap.dedent(body))
    return str(d)


def test_crond_active_entry_found(tmp_path):
    d = _crond(tmp_path, {"yon-ci-nightly": YON_CI + "\n"})
    items = scan(crond_dir=d, crontab_text="", include_systemd=False)
    assert len(items) == 1
    assert items[0]["active"] is True
    assert items[0]["source"] == "cron.d"
    assert items[0]["privileged"] is True


def test_crond_disabled_file_is_inactive(tmp_path):
    d = _crond(tmp_path, {"yon-ci.disabled-isd-cutover-2026-06-18": YON_CI + "\n"})
    items = scan(crond_dir=d, crontab_text="", include_systemd=False)
    assert len(items) == 1 and items[0]["active"] is False


def test_crond_skips_comments_and_blanks(tmp_path):
    d = _crond(tmp_path, {"x": "# a comment\n\n" + YON_CI + "\n# trailing\n"})
    items = scan(crond_dir=d, crontab_text="", include_systemd=False)
    assert len(items) == 1


def test_missing_crond_dir_degrades(tmp_path):
    items = scan(crond_dir=str(tmp_path / "nope"), crontab_text="",
                 include_systemd=False)
    assert items == []


def test_crontab_parsed_and_unprivileged():
    items = scan(crond_dir="/nonexistent", crontab_text=YON_CI + "\n",
                 include_systemd=False)
    assert len(items) == 1 and items[0]["privileged"] is False


def test_empty_crontab_degrades():
    assert scan(crond_dir="/nonexistent", crontab_text="",
                include_systemd=False) == []


def test_systemd_units_injectable():
    items = scan(crond_dir="/nonexistent", crontab_text="",
                 systemd_units=[("yon-x.timer", f"ExecStart={H}/yon/x.sh")])
    assert len(items) == 1 and items[0]["source"] == "systemd"


# ── project attribution ──────────────────────────────────────────────────────
def test_for_project_selects_only_the_dead_project(tmp_path):
    d = _crond(tmp_path, {"yon-ci-nightly": YON_CI + "\n",
                          "yonmusic-trend": YONMUSIC + "\n"})
    items = scan(crond_dir=d, crontab_text="", include_systemd=False)
    mine = for_project(items, YON)
    assert len(mine) == 1
    assert mine[0]["unit"] == "yon-ci-nightly"


# repos that "exist" for the audit tests
_REPOS = {f"{H}/yon", f"{H}/projects/yonmusic"}
_isdir = lambda p: p.endswith("/.git") and os.path.dirname(p) in _REPOS  # noqa: E731


def test_unowned_flags_dead_project_work(tmp_path):
    d = _crond(tmp_path, {"yon-ci-nightly": YON_CI + "\n"})
    items = scan(crond_dir=d, crontab_text="", include_systemd=False)
    got = unowned(items, known_paths=[f"{H}/projects/yonmusic"], isdir=_isdir)
    assert len(got) == 1 and got[0]["paths"] == [f"{H}/yon"]


def test_unowned_ignores_registered_projects(tmp_path):
    d = _crond(tmp_path, {"yonmusic-trend": YONMUSIC + "\n"})
    items = scan(crond_dir=d, crontab_text="", include_systemd=False)
    assert unowned(items, known_paths=[f"{H}/projects/yonmusic"],
                   isdir=_isdir) == []


def test_unowned_ignores_already_disabled(tmp_path):
    d = _crond(tmp_path, {"yon-ci.disabled-x-2026-01-01": YON_CI + "\n"})
    items = scan(crond_dir=d, crontab_text="", include_systemd=False)
    assert unowned(items, known_paths=[], isdir=_isdir) == []


def test_unowned_ignores_paths_outside_roots(tmp_path):
    d = _crond(tmp_path, {"sys": "0 1 * * * root /usr/sbin/logrotate /etc/x\n"})
    items = scan(crond_dir=d, crontab_text="", include_systemd=False)
    assert unowned(items, known_paths=[], isdir=_isdir) == []


def test_unowned_ignores_projectless_home_utilities(tmp_path):
    """~/bin/clanker and ~/bin/tmux-keepalive.sh belong to no repo — the audit
    must not bury the real finding under them (observed 2026-08-12: 21 hits,
    most of them noise)."""
    d = _crond(tmp_path, {
        "keepalive": f"*/3 * * * * user {H}/bin/tmux-keepalive.sh\n",
        "alerts": f"*/15 * * * * user {H}/bin/clanker alert check --cron\n",
        "backup": f"0 3 * * * user rsync -az h:/x {H}/backups/eigenstate-sqlite\n",
    })
    items = scan(crond_dir=d, crontab_text="", include_systemd=False)
    assert unowned(items, known_paths=[], isdir=_isdir) == []


def test_repo_root_of_finds_owning_repo():
    from schedules import repo_root_of
    assert repo_root_of(f"{H}/yon/scripts/x.py", (f"{H}",),
                        isdir=_isdir) == f"{H}/yon"


def test_repo_root_of_returns_none_for_projectless_path():
    from schedules import repo_root_of
    assert repo_root_of(f"{H}/bin/clanker", (f"{H}",),
                        isdir=_isdir) is None


# ── disable planning: never deletes, never runs ──────────────────────────────
def test_disable_command_for_crond_is_a_rename_not_a_delete(tmp_path):
    d = _crond(tmp_path, {"yon-ci-nightly": YON_CI + "\n"})
    it = scan(crond_dir=d, crontab_text="", include_systemd=False)[0]
    cmds = disable_commands(it, reason="dead-project", today="2026-08-12")
    assert len(cmds) == 1
    assert cmds[0].startswith("sudo mv ")
    assert ".disabled-dead-project-2026-08-12" in cmds[0]


@pytest.mark.parametrize("src,units", [
    ("cron.d", None),
    ("systemd", [("yon-x.timer", f"ExecStart={H}/yon/x.sh")]),
])
def test_no_plan_ever_contains_a_delete(tmp_path, src, units):
    if src == "cron.d":
        d = _crond(tmp_path, {"yon-ci-nightly": YON_CI + "\n"})
        items = scan(crond_dir=d, crontab_text="", include_systemd=False)
    else:
        items = scan(crond_dir="/nonexistent", crontab_text="", systemd_units=units)
    cmds, _ = plan(items, today="2026-08-12")
    blob = " ".join(cmds)
    for danger in (" rm ", "rm -", "unlink", "shred", "> /dev/null 2>&1 &"):
        assert danger not in blob, "plan must never delete: {}".format(blob)


def test_plan_emits_one_mv_per_file_not_per_line(tmp_path):
    body = YON_CI + "\n" + YON_CI.replace("0 6", "0 7") + "\n"
    d = _crond(tmp_path, {"yon-ci-nightly": body})
    items = scan(crond_dir=d, crontab_text="", include_systemd=False)
    assert len(items) == 2
    cmds, sudo = plan(items, today="2026-08-12")
    assert len([c for c in cmds if c.startswith("sudo mv")]) == 1
    assert sudo is True


def test_plan_for_user_crontab_needs_no_sudo():
    items = scan(crond_dir="/nonexistent", crontab_text=YON_CI + "\n",
                 include_systemd=False)
    cmds, sudo = plan(items, today="2026-08-12")
    assert sudo is False
    assert any("crontab -e" in c for c in cmds)


def test_systemd_disable_is_disable_not_mask_or_delete():
    items = scan(crond_dir="/nonexistent", crontab_text="",
                 systemd_units=[("yon-x.timer", f"ExecStart={H}/yon/x.sh")])
    cmds, sudo = plan(items, today="2026-08-12")
    assert cmds == ["sudo systemctl disable --now yon-x.timer"]
    assert sudo is True


# ── the decommission GATE on remove_project ──────────────────────────────────
def _fake_item(active=True):
    return {"source": "cron.d", "unit": "yon-ci-nightly",
            "path": "/etc/cron.d/yon-ci-nightly", "line_no": 1,
            "line": YON_CI, "active": active, "privileged": True}


def test_remove_refuses_while_project_owns_active_schedules(monkeypatch, capsys):
    import onboard
    import schedules as sch
    monkeypatch.setattr(sch, "scan", lambda *a, **k: [_fake_item(active=True)])
    monkeypatch.setattr(onboard, "remove_project",
                        onboard.remove_project)      # keep real fn
    import registry
    monkeypatch.setattr(registry.Registry, "get_path", lambda self, n: YON)
    ok = onboard.remove_project("yon")
    out = capsys.readouterr().out
    assert ok is False
    assert "still owns" in out
    assert "sudo mv /etc/cron.d/yon-ci-nightly" in out
    assert "--force-schedules" in out


def test_remove_ignores_already_disabled_schedules(monkeypatch):
    import onboard
    import schedules as sch
    import registry
    monkeypatch.setattr(sch, "scan", lambda *a, **k: [_fake_item(active=False)])
    monkeypatch.setattr(registry.Registry, "get_path", lambda self, n: YON)
    monkeypatch.setattr("registry.remove_entry", lambda n: False)
    ok = onboard.remove_project("yon")
    assert ok is True          # disabled work does not block retirement


def test_force_schedules_overrides_the_gate(monkeypatch):
    import onboard
    import schedules as sch
    import registry
    monkeypatch.setattr(sch, "scan", lambda *a, **k: [_fake_item(active=True)])
    monkeypatch.setattr(registry.Registry, "get_path", lambda self, n: YON)
    monkeypatch.setattr("registry.remove_entry", lambda n: False)
    ok = onboard.remove_project("yon", force_schedules=True)
    assert ok is True


def test_gate_fails_CLOSED_when_the_scan_errors(monkeypatch, capsys):
    """A broken inventory must not silently re-enable the old behaviour."""
    import onboard
    import schedules as sch
    import registry

    def boom(*a, **k):
        raise RuntimeError("cron unreadable")

    monkeypatch.setattr(sch, "scan", boom)
    monkeypatch.setattr(registry.Registry, "get_path", lambda self, n: YON)
    ok = onboard.remove_project("yon")
    out = capsys.readouterr().out
    assert ok is False
    assert "fails CLOSED" in out


# ── gc integration: orphans get an alert, clean runs dismiss it ──────────────
#
# HERMETIC OR NOT AT ALL. cleanup.DATA_DIR and alerts.ALERTS_DIR are captured at
# IMPORT time (repo law 9: no import-time env capture in lib code), so the
# session-wide CLANKER_DATA fixture — which is set after collection — does NOT
# reach them. An unpatched run_gc(dry_run=False) here runs the REAL garbage
# collector against /data/clanker: it gzip-archives sessions >90d and health
# >30d and DELETES alerts >7d with no backup. That happened once on 2026-08-12
# (6 live files archived early, all verified intact) before this fixture existed.
@pytest.fixture
def gc_sandbox(tmp_path, monkeypatch):
    import alerts
    import cleanup
    data = tmp_path / "clanker-data"
    for sub in ("alerts", "raw/sessions", "raw/health",
                "archive/sessions", "archive/health"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLANKER_DATA", str(data))
    monkeypatch.setattr(cleanup, "DATA_DIR", str(data))
    monkeypatch.setattr(alerts, "DATA_DIR", str(data))
    monkeypatch.setattr(alerts, "ALERTS_DIR", str(data / "alerts"))
    return data


def test_gc_sandbox_never_points_at_the_live_store(gc_sandbox):
    """Guard the guard: if this ever resolves to /data/clanker, stop."""
    import alerts
    import cleanup
    assert "/data/clanker" not in cleanup.DATA_DIR
    assert "/data/clanker" not in alerts.ALERTS_DIR
    assert str(gc_sandbox) in cleanup.DATA_DIR


def test_gc_raises_alert_for_orphaned_schedules(gc_sandbox, monkeypatch):
    import cleanup
    import schedules as sch
    monkeypatch.setattr(sch, "scan", lambda *a, **k: [_fake_item(active=True)])
    monkeypatch.setattr(sch, "unowned",
                        lambda items, known_paths, **k:
                        [dict(items[0], paths=[f"{H}/yon"])])
    res = cleanup.run_gc(dry_run=False)
    assert res.get("schedules_orphaned") == 1
    assert (gc_sandbox / "alerts" / "schedules-orphaned.json").exists()


def test_gc_dismisses_alert_when_no_orphans(gc_sandbox, monkeypatch):
    import cleanup
    import schedules as sch
    stale = gc_sandbox / "alerts" / "schedules-orphaned.json"
    stale.write_text('{"id": "schedules-orphaned", "severity": "warning"}')
    monkeypatch.setattr(sch, "scan", lambda *a, **k: [])
    monkeypatch.setattr(sch, "unowned", lambda *a, **k: [])
    res = cleanup.run_gc(dry_run=False)
    assert res.get("schedules_orphaned") == 0
    assert not stale.exists()


def test_gc_survives_a_broken_schedules_scan(gc_sandbox, monkeypatch):
    """gc must never crash the weekly cron on our own bug."""
    import cleanup
    import schedules as sch

    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(sch, "scan", boom)
    res = cleanup.run_gc(dry_run=False)
    assert str(res.get("schedules_orphaned", "")).startswith("error:")


# ── real-world regression: the actual 2026-08-12 incident ────────────────────
def test_real_yon_fleet_is_attributed_correctly(tmp_path):
    """All eight real yon cron.d names, plus the two live look-alike projects."""
    d = _crond(tmp_path, {
        "yon-ci-nightly": YON_CI + "\n",
        "yon-fut-l2l3": f"0 9 * * 2-6 user cd {H}/yon && python3 -u scripts/futures_micro/nightly.py\n",
        "yon-capture-audit": f"30 13 * * * user {H}/bin/capture-freshness-audit.sh\n",
        "yon-marker-proof.disabled-isd-cutover-2026-06-18": "39 13 * * 1-5 user /usr/local/sbin/yon-marker-proof.sh\n",
        "yonmusic-trend": YONMUSIC + "\n",
        "yonlaptop": f"0 3 * * * user cd {H}/projects/yonlaptop-setup && ./sync.sh\n",
    })
    items = scan(crond_dir=d, crontab_text="", include_systemd=False)
    mine = for_project(items, YON)
    units = sorted(i["unit"] for i in mine)
    # ci-nightly and fut-l2l3 reference {H}/yon; capture-audit references
    # only {H}/bin/...; the disabled one still ATTRIBUTES (it is yon's)
    assert "yon-ci-nightly" in units
    assert "yon-fut-l2l3" in units
    assert "yonmusic-trend" not in units
    assert "yonlaptop" not in units
    # and the sibling projects survive an unowned() sweep that knows them
    # isdir is injected: the original relied on the operator's real ~/yon/.git
    got = unowned(items, known_paths=[f"{H}/projects/yonmusic",
                                      f"{H}/projects/yonlaptop-setup"],
                  isdir=_isdir)
    flagged = sorted(i["unit"] for i in got)
    assert "yonmusic-trend" not in flagged
    assert "yonlaptop" not in flagged
    assert "yon-ci-nightly" in flagged
