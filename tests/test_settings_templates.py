"""Archetype settings templates (lib/settings_templates.py, `clanker settings`).

Temp repos only; git runs with an empty global config (no templateDir hooks,
no excludes) and a fixed identity. The behaviour tests use one-key
templates in a temp dir (CLANKER_SETTINGS_TEMPLATES); the real
templates/settings files are checked for the connector key and the tool set
policy on their own.
"""

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CLANKER = os.path.join(REPO, "bin", "clanker")
sys.path.insert(0, os.path.join(REPO, "lib"))

import settings_templates as st  # noqa: E402

ARCHETYPES = ["research", "production", "tool", "infra", "frontend"]
FIXED_TEMPLATES = {"build": {}, **{a: {"disableClaudeAiConnectors": True} for a in ARCHETYPES}}


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    cfg = tmp_path / "gitconfig"
    cfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.invalid"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.invalid")):
        monkeypatch.setenv(k, v)


@pytest.fixture(autouse=True)
def fixed_templates(tmp_path, monkeypatch):
    """One-key templates for the behaviour tests, so they do not follow the shipped files."""
    d = tmp_path / "templates"
    d.mkdir()
    for name, body in FIXED_TEMPLATES.items():
        (d / f"{name}.json").write_text(json.dumps(body))
    monkeypatch.setenv("CLANKER_SETTINGS_TEMPLATES", str(d))


def repo(tmp_path, name, settings=None, git=True, text=None):
    path = tmp_path / name
    (path / ".claude").mkdir(parents=True)
    if git:
        subprocess.run(["git", "init", "-q", str(path)], check=True)
    if settings is not None or text is not None:
        (path / ".claude" / "settings.json").write_text(text if text is not None else json.dumps(settings, indent=2) + "\n")
    if git:
        (path / "README").write_text("r\n")
        subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    return str(path)


def git(path, *args):
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)


CFG = {"overrides": {"docs-user": "build"}}


def test_shipped_templates_carry_the_connector_key_and_tool_set_policy_except_build(monkeypatch):
    monkeypatch.delenv("CLANKER_SETTINGS_TEMPLATES", raising=False)
    assert set(ARCHETYPES) | {"build"} <= set(st.template_names())
    for a in ARCHETYPES:
        t = st.load_template(a)
        assert t["disableClaudeAiConnectors"] is True
        assert t["enableArtifact"] is False
        assert t["permissions"] == {"deny": ["ScheduleWakeup"]}
    assert not {"disableClaudeAiConnectors", "enableArtifact", "permissions"} & set(st.load_template("build"))


def test_with_key_inserts_one_line_and_keeps_every_other_byte():
    text = '{\n    "$comment": "x",\n    "hooks": {}\n}\n'
    out = st.with_key(text, "disableClaudeAiConnectors", True)
    assert out == '{\n    "disableClaudeAiConnectors": true,\n    "$comment": "x",\n    "hooks": {}\n}\n'
    assert st.with_key(out, "disableClaudeAiConnectors", True) == out
    assert json.loads(st.with_key(out, "disableClaudeAiConnectors", False))["disableClaudeAiConnectors"] is False
    assert json.loads(st.with_key('{"a":1}', "k", True)) == {"a": 1, "k": True}
    assert json.loads(st.with_key("{}\n", "k", True)) == {"k": True}
    with pytest.raises(ValueError):
        st.with_key("[1]", "k", True)


def test_check_repo_reports_missing_differs_extra_local_and_timeouts(tmp_path):
    p = repo(tmp_path, "a", {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "bash x.sh", "timeout": 120},
                                                           {"type": "command", "command": "bash ok.sh", "timeout": 30}]}]},
                             "env": {}})
    (tmp_path / "a" / ".claude" / "settings.local.json").write_text('{"disableClaudeAiConnectors": true}')
    r = st.check_repo("a", p, "tool", CFG)
    assert r["template"] == "tool" and r["tracked"] is True and r["status"] == "drift"
    assert r["missing"] == ["disableClaudeAiConnectors"] and r["local_only"] == ["disableClaudeAiConnectors"]
    assert r["extra"] == ["env", "hooks"] and r["timeouts"] == ["Stop 120s"]
    r = st.check_repo("a", p, "tool", CFG, {"hook_timeout_allow": ["x.sh"]})
    assert r["timeouts"] == []


def test_check_repo_ok_differs_none_and_errors(tmp_path):
    ok = repo(tmp_path, "ok", {"disableClaudeAiConnectors": True})
    assert st.check_repo("ok", ok, "research", CFG)["status"] == "ok"
    off = repo(tmp_path, "off", {"disableClaudeAiConnectors": False})
    assert st.check_repo("off", off, "research", CFG)["differs"] == ["disableClaudeAiConnectors"]
    none = repo(tmp_path, "none")
    r = st.check_repo("none", none, "infra", CFG)
    assert r["file"] is False and r["missing"] == ["disableClaudeAiConnectors"] and r["status"] == "drift"
    bad = repo(tmp_path, "bad", text="{oops")
    assert st.check_repo("bad", bad, "tool", CFG)["status"] == "error"
    assert st.check_repo("none", none, "?", CFG)["status"] == "no-template"
    docs = repo(tmp_path, "docs-user", {"hooks": {}})
    r = st.check_repo("docs-user", docs, "tool", CFG)
    assert r["template"] == "build" and r["status"] == "ok"


def test_apply_writes_refuses_and_is_idempotent(tmp_path, capsys):
    p = repo(tmp_path, "a", {"hooks": {}}, git=False)
    assert st.apply_key("a", p, "tool", "disableClaudeAiConnectors", CFG) == 0
    target = os.path.join(p, ".claude", "settings.json")
    assert json.loads(open(target).read()) == {"disableClaudeAiConnectors": True, "hooks": {}}
    assert st.apply_key("a", p, "tool", "disableClaudeAiConnectors", CFG) == 0
    assert "already true" in capsys.readouterr().out
    docs = repo(tmp_path, "docs-user", {"hooks": {}}, git=False)
    assert st.apply_key("docs-user", docs, "tool", "disableClaudeAiConnectors", CFG) == 1
    none = repo(tmp_path, "none", git=False)
    assert st.apply_key("none", none, "tool", "disableClaudeAiConnectors", CFG) == 1
    assert st.apply_key("none", none, "tool", "disableClaudeAiConnectors", CFG, create=True) == 0
    assert st.apply_key("none", none, "?", "disableClaudeAiConnectors", CFG) == 1


def test_apply_dry_run_writes_nothing(tmp_path):
    p = repo(tmp_path, "a", {"hooks": {}}, git=False)
    before = open(os.path.join(p, ".claude", "settings.json")).read()
    assert st.apply_key("a", p, "tool", "disableClaudeAiConnectors", CFG, dry_run=True) == 0
    assert open(os.path.join(p, ".claude", "settings.json")).read() == before


def test_apply_commit_commits_that_file_alone(tmp_path):
    p = repo(tmp_path, "a", {"hooks": {}})
    with open(os.path.join(p, "README"), "a") as f:
        f.write("unrelated work in progress\n")
    git(p, "add", "README")
    assert st.apply_key("a", p, "tool", "disableClaudeAiConnectors", CFG, commit=True) == 0
    assert git(p, "show", "--name-only", "--format=%s", "HEAD").stdout.split() [-1] == ".claude/settings.json"
    assert "disableClaudeAiConnectors true" in git(p, "log", "-1", "--format=%s").stdout
    assert "Co-Authored-By" not in git(p, "log", "-1", "--format=%B").stdout
    assert git(p, "status", "--porcelain").stdout.strip() == "M  README"


def test_apply_commit_refuses_a_dirty_file_and_restores_on_failure(tmp_path):
    p = repo(tmp_path, "a", {"hooks": {}})
    target = os.path.join(p, ".claude", "settings.json")
    with open(target, "a") as f:
        f.write("\n")
    assert st.apply_key("a", p, "tool", "disableClaudeAiConnectors", CFG, commit=True) == 1
    git(p, "checkout", "--", ".claude/settings.json")
    hook = os.path.join(p, ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\necho blocked >&2\nexit 1\n")
    os.chmod(hook, 0o755)
    before = open(target).read()
    assert st.apply_key("a", p, "tool", "disableClaudeAiConnectors", CFG, commit=True) == 1
    assert open(target).read() == before and git(p, "status", "--porcelain").stdout == ""


def test_cli_check_and_apply(tmp_path):
    a = repo(tmp_path, "alpha", {"hooks": {}})
    b = repo(tmp_path, "beta", {"disableClaudeAiConnectors": True})
    reg = tmp_path / "registry.yaml"
    reg.write_text(f"projects:\n  alpha:\n    archetype: tool\n    path: {a}\n"
                   f"  beta:\n    archetype: research\n    path: {b}\n")
    roots = tmp_path / "roots"
    roots.mkdir()
    env = {**os.environ, "CLANKER_REGISTRY": str(reg), "CLANKER_PROJECT_ROOTS": str(roots)}

    def run(*args):
        return subprocess.run([CLANKER, "settings", *args], env=env, capture_output=True, text=True, timeout=60)
    r = run("check", "--all", "--json")
    assert r.returncode == 1, r.stderr
    rows = {x["repo"]: x for x in json.loads(r.stdout)}
    assert rows["alpha"]["status"] == "drift" and rows["beta"]["status"] == "ok"
    assert run("check").returncode == 2
    r = run("apply", "alpha", "--key", "disableClaudeAiConnectors", "--commit")
    assert r.returncode == 0 and "committed" in r.stdout, r.stderr
    assert run("check", "alpha").returncode == 0
    assert run("apply", "nope", "--key", "disableClaudeAiConnectors").returncode == 1
