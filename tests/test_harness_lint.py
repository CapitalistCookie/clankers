"""Harness lint (lib/harness_lint.py, `clanker doctor --harness`).

Every check runs against a fixture tree: a fake ~/.claude, three fake repos, a
fake registry with a `harness_lint:` block and a fake $CLANKER_DATA. Each check
must flag its planted fault and pass its clean twin. Nothing reads the live box.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CLANKER = os.path.join(REPO, "bin", "clanker")
sys.path.insert(0, os.path.join(REPO, "lib"))

import harness_lint as hl  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)
    return path


def skill(path, desc):
    return write(path, f"---\nname: s\ndescription: {desc}\n---\nbody\n")


@pytest.fixture
def box(tmp_path):
    home = str(tmp_path / "home")
    claude = os.path.join(home, ".claude")
    alpha = os.path.join(home, "projects", "alpha")
    beta = os.path.join(home, "projects", "beta")
    gamma = os.path.join(home, "work", "gamma")
    data = str(tmp_path / "data")
    ok_hook = write(os.path.join(claude, "hooks", "ok.sh"), "#!/bin/bash\n# alpha in a comment only\nexit 0\n", 0o755)
    write(os.path.join(claude, "hooks", "noexec.sh"), "#!/bin/bash\nexit 0\n", 0o644)
    write(os.path.join(claude, "hooks", "refs.sh"), "#!/bin/bash\ncd ~/projects/beta && make\n", 0o755)
    write(os.path.join(claude, "hooks", "clanker-note.sh"), "#!/bin/bash\necho clanker\n", 0o755)
    write(os.path.join(claude, "hooks", "old.sh.bak-1"), "alpha beta gamma\n")
    write(os.path.join(claude, "statusline.sh"), "#!/bin/bash\necho gamma\n", 0o755)
    write(os.path.join(claude, "CLAUDE.md"), "Rules. See ~/projects/.registry.yaml.\n" + "x" * 7000 + "\n")
    write(os.path.join(claude, "agents", "a.md"), "An agent for gamma work.\n<!-- beta -->\n")
    skill(os.path.join(claude, "skills", "long", "SKILL.md"), "d" * 301)
    skill(os.path.join(claude, "skills", "short", "SKILL.md"), "short")
    skill(os.path.join(claude, "skills", ".trash", "x", "SKILL.md"), "t" * 900)
    write(os.path.join(claude, "rules", "global.md"), "no front matter\n")
    settings = {
        "env": {"PWB_RISK": "1", "FOO_ALPHA_BAR": "1", "CLAUDE_CODE_X": "1"},
        "permissions": {"allow": ["Bash(make -C ~/projects/alpha:*)"]},
        "statusLine": {"type": "command", "command": f"bash {claude}/statusline.sh"},
        "hooks": {
            "PostToolUse": [{"matcher": "Edit", "hooks": [
                {"type": "command", "command": f"bash {claude}/hooks/allowed-long.sh", "timeout": 150},
                {"type": "command", "command": f"bash {ok_hook}", "timeout": 90}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": f"bash {claude}/hooks/gone.sh", "timeout": 5},
                {"type": "command", "command": f"{claude}/hooks/noexec.sh", "timeout": 5},
                {"type": "command", "command": f"python3 -u {ok_hook}", "timeout": 5},
                {"type": "command", "command": "echo hello", "timeout": 5},
                {"type": "command", "command": "bash \"$CLAUDE_PROJECT_DIR/x.sh\"", "timeout": 5}]}],
        },
    }
    write(os.path.join(claude, "settings.json"), json.dumps(settings))
    write(os.path.join(claude, "hooks", "allowed-long.sh"), "#!/bin/bash\n", 0o755)
    # alpha: small CLAUDE.md, STATUS.md without NOW, project hooks, rules
    write(os.path.join(alpha, "CLAUDE.md"), "small\n")
    write(os.path.join(alpha, "STATUS.md"), "# STATUS\n\n## NEXT\n- a\n")
    write(os.path.join(alpha, "src", "a.py"), "print(1)\n")
    write(os.path.join(alpha, "hooks", "present.sh"), "#!/bin/bash\n", 0o755)
    write(os.path.join(alpha, ".claude", "settings.json"), json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "bash \"${CLAUDE_PROJECT_DIR:-.}/hooks/present.sh\""},
        {"type": "command", "command": "bash \"$CLAUDE_PROJECT_DIR/hooks/absent.sh\""},
        {"type": "command", "command": "cd hooks && bash present.sh 2>/dev/null; echo done"}]}]}}))
    write(os.path.join(alpha, ".claude", "rules", "unscoped.md"), "---\nname: r\n---\nbody\n")
    write(os.path.join(alpha, ".claude", "rules", "scoped.md"),
          "---\npaths:\n  - \"src/**\"\n  - \"nope/**\"\n  - \"*.py\"\n---\nbody\n")
    skill(os.path.join(alpha, ".claude", "skills", "big", "SKILL.md"), "b" * 400)
    # beta: CLAUDE.md over the limit, NOW over 800 B; stacked NOW headings
    write(os.path.join(beta, "CLAUDE.md"), "y" * 6145)
    write(os.path.join(beta, "STATUS.md"),
          "# S\n\n## NOW (2026-09-01)\nold short\n\n## NOW (2026-09-20)\n" + ("- line of text\n" * 70) + "\n## NEXT\n- b\n")
    # gamma: clean STATUS.md
    write(os.path.join(gamma, "STATUS.md"), "## NOW (2026-09-24)\n- ok\n\n## NEXT\n")
    # data dir: 201 alerts, hook-error rows (2 recent, 1 old, 1 undated)
    for i in range(201):
        write(os.path.join(data, "alerts", f"a{i}.json"), "{}")
    rows = [{"ts": (NOW - timedelta(hours=1)).isoformat(), "hook": "stop-dispatch"},
            {"ts": (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), "hook": "stop-dispatch"},
            {"ts": (NOW - timedelta(days=3)).isoformat(), "hook": "old"},
            {"hook": "undated"}]
    write(os.path.join(data, "raw", "health", "hook-errors-2026-09-24.jsonl"),
          "\n".join(json.dumps(r) for r in rows) + "\n")
    registry = write(str(tmp_path / "registry.yaml"),
                     "projects:\n"
                     f"  alpha:\n    archetype: tool\n    path: {alpha}\n"
                     f"  beta:\n    archetype: tool\n    path: {beta}\n"
                     f"  gamma:\n    archetype: tool\n    path: {gamma}\n"
                     "harness_lint:\n  hook_timeout_allow: [allowed-long.sh]\n")
    projects = {"alpha": alpha, "beta": beta, "gamma": gamma, "clanker": hl.REPO_ROOT}
    return {"home": home, "claude": claude, "data": data, "registry": registry, "projects": projects,
            "alpha": alpha, "beta": beta, "gamma": gamma, "tmp": tmp_path}


def lint(box, **kw):
    return hl.run(claude_dir=box["claude"], registry_path=box["registry"], data_dir=box["data"],
                  projects=box["projects"], home=box["home"], now=NOW, **kw)


def by_check(result, cid):
    return [f for f in result["findings"] if f.check == cid]


def rel(f, box):
    return os.path.relpath(f.path, box["home"])


def test_a_names_and_paths_in_global_files_skip_comments_backups_and_self(box):
    r = lint(box)
    a = {rel(f, box): f.message for f in by_check(r, "a")}
    assert a[".claude/hooks/refs.sh"] == "names: beta; paths: ~/projects/beta"
    assert a[".claude/statusline.sh"] == "names: gamma"
    assert a[".claude/agents/a.md"] == "names: gamma"          # the HTML comment is skipped
    assert a[".claude/settings.json"] == "names: alpha; paths: ~/projects/alpha"
    assert ".claude/hooks/ok.sh" not in a                       # alpha only in a comment
    assert ".claude/hooks/clanker-note.sh" not in a             # the harness names itself
    assert not any(".bak" in k for k in a)
    assert ".claude/CLAUDE.md" not in a                         # ~/projects/.registry.yaml is a file


def test_a_name_allow_from_the_registry_config(box):
    with open(box["registry"], "a") as f:
        f.write("  name_allow: [beta, gamma, alpha]\n")
    assert by_check(lint(box), "a") == []


def test_b_skill_descriptions_over_300_chars(box):
    b = sorted(rel(f, box) for f in by_check(lint(box), "b"))
    assert b == [".claude/skills/long/SKILL.md", "projects/alpha/.claude/skills/big/SKILL.md"]


def test_c_claude_md_over_6144_bytes(box):
    c = {rel(f, box): f.message for f in by_check(lint(box), "c")}
    assert set(c) == {".claude/CLAUDE.md", "projects/beta/CLAUDE.md"}
    assert c["projects/beta/CLAUDE.md"] == "6,145 B (> 6,144)"


def test_d_status_now_missing_and_oversized_latest_dated_heading(box):
    d = {rel(f, box): f.message for f in by_check(lint(box), "d")}
    assert d["projects/alpha/STATUS.md"] == "no `## NOW` section"
    assert d["projects/beta/STATUS.md"].startswith("## NOW is 1,0")
    assert "work/gamma/STATUS.md" not in d


def test_e_hook_timeouts_over_60_with_allowlist(box):
    e = [f.message for f in by_check(lint(box), "e")]
    assert e == ["PostToolUse[Edit] timeout 90 s (> 60): bash ~/.claude/hooks/ok.sh"]


def test_f_hook_scripts_missing_or_not_executable(box):
    f = sorted(x.message for x in by_check(lint(box), "f"))
    assert f == ["PreToolUse[Bash]: ~/.claude/hooks/gone.sh is missing",
                 "PreToolUse[Bash]: ~/.claude/hooks/noexec.sh is not executable",
                 "Stop: ~/projects/alpha/hooks/absent.sh is missing"]


def test_hook_scripts_parser():
    assert hl.hook_scripts('bash "${CLAUDE_PROJECT_DIR:-.}/h.sh" x', "/r", "/h") == [("/r/h.sh", False)]
    assert hl.hook_scripts("cd /w && python3 -u s.py 2>/dev/null; echo ok", None, "/h") == [("/w/s.py", False)]
    assert hl.hook_scripts("FOO=1 /x/run.sh > /dev/null", None, "/h") == [("/x/run.sh", True)]
    assert hl.hook_scripts("python3 -m pkg.mod", "/r", "/h") == []
    assert hl.hook_scripts("bash $CLAUDE_PROJECT_DIR/x.sh", None, "/h") == []
    assert hl.hook_scripts("jq . | tee x", None, "/h") == []


def test_g_rules_without_paths_and_dead_globs(box):
    g = {rel(f, box): f.message for f in by_check(lint(box), "g")}
    assert g["projects/alpha/.claude/rules/unscoped.md"].startswith("no `paths:`")
    assert g["projects/alpha/.claude/rules/scoped.md"] == "dead glob: nope/**"
    assert g[".claude/rules/global.md"].startswith("no `paths:`")


def test_glob_regex():
    assert hl.glob_regex("src/**").match("src/a/b.py")
    assert hl.glob_regex("**/*.md").match("a.md") and hl.glob_regex("**/*.md").match("x/y/z.md")
    assert hl.glob_regex("{a,b}/*.py").match("b/c.py") and not hl.glob_regex("{a,b}/*.py").match("c/c.py")
    assert hl.glob_matches("install.*", ["x/install.sh"])
    assert not hl.glob_matches("docs/**", ["src/docs.py"])


def test_h_alerts_dir_over_200(box):
    [h] = by_check(lint(box), "h")
    assert h.message == "201 files (> 200)"


def test_i_project_env_keys_in_global_settings(box):
    i = sorted(f.message for f in by_check(lint(box), "i"))
    assert i == ["env FOO_ALPHA_BAR: names the project alpha", "env PWB_RISK: a project-specific prefix"]


def test_j_hook_errors_last_24h_and_absent_log(box):
    [j] = by_check(lint(box), "j")
    assert j.message == "2 hook error(s) in the last 24 h (stop-dispatch×2)"
    os.remove(os.path.join(box["data"], "raw", "health", "hook-errors-2026-09-24.jsonl"))
    r = lint(box)
    assert by_check(r, "j") == [] and r["notes"]["j"] == "no hook-error log yet"


def test_render_json_and_summary(box):
    r = lint(box)
    text = hl.render(r, home=box["home"])
    assert text.startswith(f"Harness lint: {len(r['findings'])} finding(s)")
    assert "~/.claude/hooks/refs.sh" in text
    data = json.loads(hl.to_json(r))
    assert data["total"] == len(r["findings"]) and data["counts"]["alerts-dir"] == 1
    ok, line = hl.summary_line(r)
    assert not ok and "run: clanker doctor --harness" in line


def test_a_broken_check_is_reported_not_hidden(box, monkeypatch):
    monkeypatch.setattr(hl, "check_alerts", lambda d: 1 / 0)
    r = lint(box)
    assert "h" in r["errors"] and by_check(r, "b")
    assert hl.summary_line(r)[0] is False


def cli_env(box):
    roots = box["tmp"] / "empty-roots"
    roots.mkdir(exist_ok=True)
    return {**os.environ, "HOME": box["home"], "CLAUDE_CONFIG_DIR": box["claude"],
            "CLANKER_REGISTRY": box["registry"], "CLANKER_DATA": box["data"],
            "CLANKER_PROJECT_ROOTS": str(roots)}


def test_cli_doctor_harness_exits_1_with_table_and_json(box):
    r = subprocess.run([CLANKER, "doctor", "--harness"], env=cli_env(box), capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 1, r.stderr
    assert "global-project-ref" in r.stdout and "~/.claude/hooks/refs.sh" in r.stdout
    r = subprocess.run([CLANKER, "doctor", "--harness", "--json"], env=cli_env(box),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and json.loads(r.stdout)["total"] > 0


def test_cli_doctor_harness_clean_tree_exits_0(tmp_path):
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text("{}")
    reg = tmp_path / "registry.yaml"
    reg.write_text("projects: {}\n")
    roots = tmp_path / "roots"
    roots.mkdir()
    env = {**os.environ, "HOME": str(home), "CLAUDE_CONFIG_DIR": str(claude), "CLANKER_REGISTRY": str(reg),
           "CLANKER_DATA": str(tmp_path / "data"), "CLANKER_PROJECT_ROOTS": str(roots)}
    r = subprocess.run([CLANKER, "doctor", "--harness"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("Harness lint: 0 finding(s)")


def test_cli_json_without_harness_is_a_usage_error():
    r = subprocess.run([CLANKER, "doctor", "--json"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "--harness" in r.stderr
