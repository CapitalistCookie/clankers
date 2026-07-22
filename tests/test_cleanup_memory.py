"""P5 (b–d) memory-debt burndown: gc sweeps registered project namespaces (not
just global), a failing memory doctor raises a real ALERT (not a cron-stdout
whisper), and the weekly digest lists top-N orphans for triage.

Hermetic: every import-time-captured path (cleanup.DATA_DIR, alerts.ALERTS_DIR,
memorycmd.GLOBAL_MEM, memoryns.CLAUDE_PROJECTS) is monkeypatched; HOME→tmp so
the swept lint script is a planted fake; registry+roots pinned to tmp."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import analyze      # noqa: E402
import cleanup      # noqa: E402
import alerts       # noqa: E402
import memorycmd    # noqa: E402
import memoryns     # noqa: E402


@pytest.fixture
def gc_env(tmp_path, monkeypatch):
    """Global namespace + one registered project namespace, fake lint on HOME."""
    monkeypatch.setattr(cleanup, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(alerts, "ALERTS_DIR", str(tmp_path / "data" / "alerts"))
    gmem = tmp_path / "gmem"
    gmem.mkdir()
    (gmem / "MEMORY.md").write_text("# global router\n")
    monkeypatch.setattr(memorycmd, "GLOBAL_MEM", str(gmem))
    monkeypatch.setattr(memorycmd, "router_gen", lambda: 0)
    monkeypatch.setattr(memoryns, "CLAUDE_PROJECTS", str(tmp_path / "claude-projects"))

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
            "ns_mem": ns_mem}


def _write_lint(path, fail_pattern=""):
    body = "#!/bin/bash\n"
    if fail_pattern:
        body += (f'case "$2" in\n  *{fail_pattern}*) '
                 'echo "MEMORY.md VIOLATION (line 1)"; exit 1;;\nesac\n')
    body += "exit 0\n"
    path.write_text(body)


def test_gc_sweeps_project_namespaces_and_alerts_on_fail(gc_env):
    _write_lint(gc_env["lint"], fail_pattern="projA")
    results = cleanup.run_gc()
    assert results["memory_namespaces_swept"] == 2          # global + projA (P5c)
    assert results["memory_doctor"] == "FAIL: projA"
    alert_file = gc_env["alerts"] / "memory-doctor.json"     # P5b: a real alert
    assert alert_file.exists(), "failing doctor did not raise an alert"
    a = json.loads(alert_file.read_text())
    assert a["severity"] == "warning" and "projA" in a["message"]
    assert "VIOLATION" in a["details"]["projA"]


def test_gc_pass_dismisses_stale_doctor_alert(gc_env):
    _write_lint(gc_env["lint"])                              # everything passes
    os.makedirs(gc_env["alerts"], exist_ok=True)
    stale = gc_env["alerts"] / "memory-doctor.json"
    stale.write_text(json.dumps({"id": "memory-doctor", "severity": "warning"}))
    results = cleanup.run_gc()
    assert results["memory_doctor"] == "pass"
    assert not stale.exists(), "healthy sweep must dismiss the standing alert"


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
