"""gc and the memory commands after auto-memory went off (2026-09-24): gc runs
no memory-lint, writes nothing to a memory dir and raises no memory-doctor
alert; `clanker memory doctor` says the lint is retired and exits 1; `memory
new` runs no lint. The weekly digest still lists top-N orphans (P5d), and the
alert-expiry tests keep their 2026-09-24 contract.

Hermetic: every import-time-captured path (cleanup.DATA_DIR, alerts.ALERTS_DIR,
memorycmd.GLOBAL_MEM, memoryns.CLAUDE_PROJECTS) is monkeypatched; HOME→tmp so
a planted fake lint would show if anything ran it; registry+roots pinned to
tmp; schedules.scan is stubbed, so no test reads the machine's crontab."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import analyze      # noqa: E402
import cleanup      # noqa: E402
import alerts       # noqa: E402
import memorycmd    # noqa: E402
import memoryns     # noqa: E402
import schedules    # noqa: E402


@pytest.fixture
def gc_env(tmp_path, monkeypatch):
    """Global namespace + one registered project namespace, fake lint on HOME."""
    monkeypatch.setattr(cleanup, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(alerts, "ALERTS_DIR", str(tmp_path / "data" / "alerts"))
    gmem = tmp_path / "gmem"
    gmem.mkdir()
    (gmem / "MEMORY.md").write_text("# global router\n")
    monkeypatch.setattr(memorycmd, "GLOBAL_MEM", str(gmem))
    router_calls = []
    monkeypatch.setattr(memorycmd, "router_gen", lambda: router_calls.append(1) or 0)
    monkeypatch.setattr(memoryns, "CLAUDE_PROJECTS", str(tmp_path / "claude-projects"))
    monkeypatch.setattr(schedules, "scan", lambda *a, **k: [])

    proj = tmp_path / "projA"
    proj.mkdir()
    ns_mem = os.path.join(memoryns.ns_dir(str(proj)), "memory")
    os.makedirs(ns_mem)
    with open(os.path.join(ns_mem, "MEMORY.md"), "w") as f:
        f.write("# projA router\n")

    reg = tmp_path / "registry.yaml"
    reg.write_text(f"projects:\n  projA:\n    archetype: tool\n    path: {proj}\n")
    monkeypatch.setenv("CLANKER_REGISTRY", str(reg))
    monkeypatch.setenv("CLANKER_PROJECT_ROOTS", str(tmp_path / "no-discovery"))

    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    return {"lint": hooks / "memory-lint.sh", "alerts": tmp_path / "data" / "alerts",
            "ns_mem": ns_mem, "gmem": gmem, "router_calls": router_calls,
            "ran": tmp_path / "lint-ran"}


def _write_lint(env):
    """A fake memory-lint that leaves a mark and fails, if anything runs it."""
    env["lint"].write_text(f'#!/bin/bash\ntouch "{env["ran"]}"\n'
                           'echo "MEMORY.md VIOLATION (line 1)" >&2; exit 1\n')


def test_gc_runs_no_memory_lint_and_writes_no_memory_file(gc_env):
    _write_lint(gc_env)
    before = sorted(os.listdir(gc_env["gmem"])), sorted(os.listdir(gc_env["ns_mem"]))
    results = cleanup.run_gc()
    assert results["memory_doctor"] == ("skipped: auto-memory is off since 2026-08-08 "
                                        "and memory-lint is retired")
    assert "memory_namespaces_swept" not in results
    assert not gc_env["ran"].exists(), "gc ran a memory lint"
    assert gc_env["router_calls"] == [], "gc regenerated ROUTER-AUTO.md in the memory dir"
    assert (sorted(os.listdir(gc_env["gmem"])), sorted(os.listdir(gc_env["ns_mem"]))) == before
    assert not (gc_env["alerts"] / "memory-doctor.json").exists()
    assert cleanup.run_gc(dry_run=True)["memory_doctor"].startswith("skipped")


def test_gc_leaves_an_old_memory_doctor_alert_to_expiry(gc_env):
    """No sweep dismisses or re-raises memory-doctor now: a leftover alert
    expires 7 days after its newest raise, like any other."""
    adir = gc_env["alerts"]
    os.makedirs(adir, exist_ok=True)
    _put_alert(adir, "memory-doctor", {"id": "memory-doctor", "timestamp": _iso_ago(1)})
    cleanup.run_gc()
    assert (adir / "memory-doctor.json").exists()
    _put_alert(adir, "memory-doctor", {"id": "memory-doctor", "timestamp": _iso_ago(8)})
    assert cleanup.run_gc()["alerts_expired"] == 1
    assert not (adir / "memory-doctor.json").exists()


def test_memory_doctor_is_retired_and_exits_1(gc_env, capsys, monkeypatch):
    _write_lint(gc_env)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    assert memorycmd.doctor() == 1
    assert memorycmd.doctor(project="projA") == 1
    err = capsys.readouterr().err
    assert "memory doctor: nothing checked — memory-lint is retired" in err
    assert calls == [] and not gc_env["ran"].exists()


def test_memory_new_scaffolds_without_a_lint_run(gc_env, capsys, monkeypatch):
    _write_lint(gc_env)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    assert memorycmd.new("a-fact", "project", "what the fact is") == 0
    assert (gc_env["gmem"] / "a-fact.md").read_text().startswith("---\nname: a-fact\n")
    assert "- [a-fact](a-fact.md) — what the fact is" in (gc_env["gmem"] / "MEMORY.md").read_text()
    out = capsys.readouterr()
    assert "memory new: no lint run — memory-lint is retired" in out.err
    assert "OTHER lint violations" not in out.err
    assert calls == [] and not gc_env["ran"].exists()


def test_orphans_top_parses_index_and_sorts_by_size(tmp_path, monkeypatch):
    gmem = tmp_path / "gmem"
    gmem.mkdir()
    (gmem / "big.md").write_text("x" * 4096)
    (gmem / "small.md").write_text("y" * 100)
    (gmem / "INDEX_ALL.md").write_text(
        "# index\n\n## ORPHANS (not referenced from any router index — 3)\n\n"
        "- small.md\n- big.md\n- missing.md\n\n## NEXT SECTION\n- not-an-orphan.md\n")
    monkeypatch.setattr(memorycmd, "GLOBAL_MEM", str(gmem))
    total, top = memorycmd.orphans_top(2)
    assert total == 3
    assert top[0][0] == "big.md" and top[0][1] == 4.0        # biggest first, KB
    assert len(top) == 2                                     # capped at n
    assert all(name != "not-an-orphan.md" for name, _ in top)


def test_weekly_digest_prints_memory_debt_even_with_no_sessions(monkeypatch, capsys):
    monkeypatch.setattr(analyze, "load_sessions", lambda **kw: [])
    monkeypatch.setattr(memorycmd, "orphans_top",
                        lambda n=10: (182, [("huge.md", 96.0), ("old.md", 2.5)]))
    analyze.run_analysis("weekly")
    out = capsys.readouterr().out
    assert "Memory debt" in out and "182" in out
    assert "huge.md" in out and "96.0KB" in out
    # daily mode must NOT carry the section
    analyze.run_analysis("daily")
    assert "Memory debt" not in capsys.readouterr().out


def _iso_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _put_alert(adir, name, alert, mtime_days_ago=0.0, raw=None):
    p = adir / f"{name}.json"
    p.write_text(raw if raw is not None else json.dumps(alert))
    t = time.time() - mtime_days_ago * 86400
    os.utime(p, (t, t))
    return p


def test_gc_expires_alerts_on_their_newest_raise(gc_env):
    """2026-09-24: gc expired on mtime alone while the escalation pass rewrote
    every alert every 15 minutes, so nothing ever expired. Then it keyed on
    first_seen, which expired standing alerts their producers re-raise every
    15 minutes. Expiry now keys on the newest raise: timestamp, else ts, else
    first_seen; mtime only when none exists. `created` is a birth time."""
    adir = gc_env["alerts"]
    os.makedirs(adir, exist_ok=True)
    put, iso = (lambda *a, **k: _put_alert(adir, *a, **k)), _iso_ago

    put("standing-reraised", {"first_seen": iso(20), "timestamp": iso(0.01)})
    put("stale-raise", {"first_seen": iso(20), "timestamp": iso(8)})
    put("timestamp-beats-ts-and-first", {"timestamp": iso(1), "ts": time.time() - 30 * 86400,
                                         "first_seen": iso(30)}, 30)
    put("ts-beats-first", {"ts": time.time() - 86400, "first_seen": iso(30)}, 30)
    put("old-first-fresh-mtime", {"first_seen": iso(10)})
    put("fresh-first-old-mtime", {"first_seen": iso(1)}, 30)
    put("created-is-not-a-raise", {"created": iso(9)})          # falls through to mtime
    put("ts-epoch-old", {"ts": time.time() - 8 * 86400})
    put("no-fields-old-mtime", {"message": "x"}, 10)
    put("no-fields-fresh", {"message": "x"}, 1)
    put("corrupt-old", None, 10, raw="{nope")
    expire = {"stale-raise", "old-first-fresh-mtime", "ts-epoch-old",
              "no-fields-old-mtime", "corrupt-old"}
    keep = {"standing-reraised", "timestamp-beats-ts-and-first", "ts-beats-first",
            "fresh-first-old-mtime", "created-is-not-a-raise", "no-fields-fresh"}

    dry = cleanup.run_gc(dry_run=True)
    assert dry["alerts_expired"] == len(expire)
    assert {p.stem for p in adir.glob("*.json")} >= expire | keep   # dry run deletes nothing

    results = cleanup.run_gc()
    left = {p.stem for p in adir.glob("*.json")}
    assert results["alerts_expired"] == len(expire)
    assert keep <= left and not (left & expire)

    # the escalation clock is still first_seen: 20 ignored days, not 0
    alerts._escalate_ignored(days=999)
    standing = json.loads((adir / "standing-reraised.json").read_text())
    assert standing["ignored_days"] == 20


def test_gc_judges_its_own_weekly_raise_after_raising_it(gc_env, monkeypatch):
    """gc re-raises schedules-orphaned once a week, in a step before the
    expiry step: the week-old raise must not expire first and come back as a
    new alert with first_seen reset. (This test used memory-doctor until that
    raise went away on 2026-09-24.)"""
    rogue = [{"active": True, "paths": ["/srv/unregistered-repo"], "line": "x"}]
    monkeypatch.setattr(schedules, "scan", lambda *a, **k: rogue)
    monkeypatch.setattr(schedules, "unowned", lambda items, known_paths=None, **k: list(items))
    adir = gc_env["alerts"]
    os.makedirs(adir, exist_ok=True)
    born = _iso_ago(30)
    _put_alert(adir, "schedules-orphaned", {"id": "schedules-orphaned", "severity": "warning",
                                            "first_seen": born, "timestamp": _iso_ago(7.01),
                                            "message": "1 active scheduled item"}, 7.01)
    results = cleanup.run_gc()
    assert results["alerts_expired"] == 0 and results["schedules_orphaned"] == 1
    a = json.loads((adir / "schedules-orphaned.json").read_text())
    assert a["first_seen"] == born                 # the same alert, not a reborn one
    assert a["timestamp"] > _iso_ago(0.01)         # raised by this run


def test_gc_archives_hook_error_logs_by_the_day_at_the_end_of_the_name(gc_env, tmp_path):
    """raw/health holds the alert-check cron's <day>.jsonl and, since
    2026-09-24, the hooks' hook-errors-<day>.jsonl. Both archive after 30
    days; the old f[:10] test never matched the second kind."""
    import gzip
    health = tmp_path / "data" / "raw" / "health"
    health.mkdir(parents=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    old = ["2026-01-01.jsonl", "hook-errors-2026-01-01.jsonl"]
    keep = [f"{today}.jsonl", f"hook-errors-{today}.jsonl", "notes.jsonl", "2026-01-01.txt"]
    for name in old + keep:
        (health / name).write_text('{"row": "%s"}\n' % name)
    assert cleanup.run_gc(dry_run=True)["health_archived"] == 2
    assert sorted(p.name for p in health.iterdir()) == sorted(old + keep)   # dry run moves nothing
    assert cleanup.run_gc()["health_archived"] == 2
    assert sorted(p.name for p in health.iterdir()) == sorted(keep)
    archive = tmp_path / "data" / "archive" / "health"
    for name in old:
        with gzip.open(archive / (name + ".gz"), "rt") as f:
            assert f.read() == '{"row": "%s"}\n' % name
