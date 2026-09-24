"""clanker skills audit / hide (lib/skills_audit.py) on a fixture box.

The fixture has user skills, two repos with project skills, a raw/skills
tracker log and a .claude.json skillUsage block, so every verdict, both
counters and the settings.local.json merge are checked without the live box.
Git runs with an empty global config, so the box's global ignore file cannot
hide the not-gitignored warning.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CLANKER = os.path.join(REPO, "bin", "clanker")
sys.path.insert(0, os.path.join(REPO, "lib"))

import skills_audit as sa  # noqa: E402

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


def skill(root, name, desc, fm_name=None):
    return write(os.path.join(root, name, "SKILL.md"),
                 f"---\nname: {fm_name or name}\ndescription: {desc}\n---\nbody\n")


@pytest.fixture
def box(tmp_path, monkeypatch):
    gitcfg = tmp_path / "gitconfig"
    gitcfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitcfg))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    claude = tmp_path / "home" / ".claude"
    alpha = tmp_path / "home" / "projects" / "alpha"
    beta = tmp_path / "home" / "projects" / "beta"
    data = tmp_path / "data"
    skill(str(claude / "skills"), "user-active", "used lately")
    skill(str(claude / "skills"), "user-never", "never used é")          # é = 2 bytes
    skill(str(claude / "skills" / ".trash"), "gone", "trash")
    skill(str(alpha / ".claude" / "skills"), "alpha-stale", "x" * 400)
    skill(str(alpha / ".claude" / "skills"), "dir-name", "matched by its front-matter name", fm_name="fm-name")
    skill(str(beta / ".claude" / "skills"), "beta-cc-only", "counted only by Claude Code")
    rows = [{"timestamp": "2026-09-20T10:00:00Z", "project": "global", "skill": "user-active"},
            {"timestamp": "2026-09-21T10:00:00Z", "project": "alpha", "skill": "user-active"},
            {"timestamp": "2026-05-01T10:00:00Z", "project": "alpha", "skill": "alpha-stale"},
            {"timestamp": "2026-09-01T10:00:00Z", "project": "alpha", "skill": "fm-name"}]
    write(str(data / "raw" / "skills" / "2026-09-21.jsonl"), "\n".join(json.dumps(r) for r in rows) + "\n")
    cc = {"skillUsage": {"user-active": {"usageCount": 5, "lastUsedAt": 1790000000000},
                         "beta-cc-only": {"usageCount": 3, "lastUsedAt": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)}}}
    write(str(claude / ".claude.json"), json.dumps(cc))
    registry = write(str(tmp_path / "registry.yaml"),
                     f"projects:\n  alpha:\n    archetype: tool\n    path: {alpha}\n"
                     f"  beta:\n    archetype: tool\n    path: {beta}\n")
    return {"claude": str(claude), "alpha": str(alpha), "beta": str(beta), "data": str(data),
            "claude_json": str(claude / ".claude.json"), "registry": registry, "tmp": tmp_path,
            "projects": {"alpha": str(alpha), "beta": str(beta)}}


def audit(box, **kw):
    return {(r["repo"], r["skill"]): r for r in
            sa.audit(box["claude"], box["projects"], box["data"], box["claude_json"], now=NOW, **kw)}


def test_inventory_covers_user_and_repo_skills_and_skips_dot_dirs(box):
    inv = sa.inventory(box["claude"], box["projects"])
    assert {(r["repo"], r["skill"]) for r in inv} == {
        ("(user)", "user-active"), ("(user)", "user-never"), ("alpha", "alpha-stale"),
        ("alpha", "dir-name"), ("beta", "beta-cc-only")}
    assert next(r for r in inv if r["skill"] == "user-never")["desc_bytes"] == len("never used é".encode())


def test_verdicts_and_both_counters(box):
    a = audit(box)
    assert a[("(user)", "user-active")]["verdict"] == "active"
    assert (a[("(user)", "user-active")]["tracker_uses"], a[("(user)", "user-active")]["cc_uses"]) == (2, 5)
    assert a[("(user)", "user-never")]["verdict"] == "never"
    assert a[("alpha", "alpha-stale")]["verdict"] == "stale" and a[("alpha", "alpha-stale")]["last_use"] == "2026-05-01"
    assert a[("alpha", "dir-name")]["tracker_uses"] == 1                   # joined by front-matter name
    assert a[("beta", "beta-cc-only")]["verdict"] == "stale" and a[("beta", "beta-cc-only")]["cc_uses"] == 3


def test_stale_days_moves_the_line(box):
    assert audit(box, stale_days=365)[("alpha", "alpha-stale")]["verdict"] == "active"


def test_render_summary(box):
    text = sa.render(sa.audit(box["claude"], box["projects"], box["data"], box["claude_json"], now=NOW))
    assert text.startswith("Skills: 5 in 3 scopes — never 1, stale (>90 d) 2, active 2")
    assert "VERDICT" in text and "alpha-stale" in text


def git_init(path):
    subprocess.run(["git", "init", "-q", path], check=True)


def test_hide_creates_merges_and_warns_when_not_gitignored(box, capsys):
    git_init(box["alpha"])
    target = os.path.join(box["alpha"], ".claude", "settings.local.json")
    write(target, json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "skillOverrides": {"keep": "name-only"}}))
    assert sa.hide(box["alpha"], ["alpha-stale", "user-never"]) == 0
    data = json.loads(open(target).read())
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    assert data["skillOverrides"] == {"keep": "name-only", "alpha-stale": "off", "user-never": "off"}
    err = capsys.readouterr().err
    assert "WARNING: .claude/settings.local.json is not gitignored" in err
    assert "user-never is not a skill in" in err


def test_hide_is_quiet_when_gitignored_and_supports_modes(box, capsys):
    git_init(box["beta"])
    write(os.path.join(box["beta"], ".gitignore"), ".claude/settings.local.json\n")
    assert sa.hide(box["beta"], ["beta-cc-only"], mode="user-invocable-only") == 0
    target = os.path.join(box["beta"], ".claude", "settings.local.json")
    assert json.loads(open(target).read()) == {"skillOverrides": {"beta-cc-only": "user-invocable-only"}}
    assert "WARNING" not in capsys.readouterr().err
    assert sa.hide(box["beta"], ["x"], mode="bogus") == 2


def test_hide_dry_run_and_invalid_json_write_nothing(box):
    target = os.path.join(box["alpha"], ".claude", "settings.local.json")
    assert sa.hide(box["alpha"], ["alpha-stale"], dry_run=True) == 0
    assert not os.path.exists(target)
    write(target, "{not json")
    assert sa.hide(box["alpha"], ["alpha-stale"]) == 1
    assert open(target).read() == "{not json"


def cli_env(box):
    roots = box["tmp"] / "roots"
    roots.mkdir(exist_ok=True)
    return {**os.environ, "CLAUDE_CONFIG_DIR": box["claude"], "CLANKER_REGISTRY": box["registry"],
            "CLANKER_DATA": box["data"], "CLANKER_PROJECT_ROOTS": str(roots)}


def run(args, box):
    return subprocess.run([CLANKER, "skills", *args], env=cli_env(box), capture_output=True, text=True, timeout=60)


def test_cli_audit_json_filters_and_table(box):
    r = run(["audit", "--json"], box)
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert len(rows) == 5 and {x["verdict"] for x in rows} == {"never", "stale", "active"}
    r = run(["audit", "alpha", "--verdict", "stale"], box)
    assert r.returncode == 0 and "alpha-stale" in r.stdout and "user-active" not in r.stdout
    r = run(["audit", "nope"], box)
    assert r.returncode == 1 and "unknown repo" in r.stderr


def test_cli_hide_by_registry_name(box):
    r = run(["hide", "alpha", "alpha-stale", "--mode", "name-only"], box)
    assert r.returncode == 0, r.stderr
    assert "hid alpha-stale (name-only)" in r.stdout
    target = os.path.join(box["alpha"], ".claude", "settings.local.json")
    assert json.loads(open(target).read()) == {"skillOverrides": {"alpha-stale": "name-only"}}
    assert run(["hide", "nope", "x"], box).returncode == 1


def test_cli_usage_report_still_default(box):
    r = run(["--last", "3650"], box)
    assert r.returncode == 0 and "Skill Usage" in r.stdout
