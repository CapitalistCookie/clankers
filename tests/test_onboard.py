"""Hermetic tests for lib/onboard.py.

detect_archetype and the *_detect_* / _read_* helpers are pure (they only read a
project dir), so they are tested directly on synthetic temp trees.

init_project / remove_project are NOT fully exercised: both write under the REAL
~/.claude/projects/<slug> (settings_dir is built from os.path.expanduser at call
time, and os.makedirs runs unconditionally even for hook-less archetypes), and
remove_project additionally invokes real tmux + the real ~/.tmux-startup.sh via
tmux_manager.remove_session. Neither is redirectable without a global
os.path.expanduser monkeypatch, so per task guidance the happy paths are SKIPPED
(see test_init_project_full_scaffold_skipped). init_project's early not-a-dir
guard IS hermetic and is covered.
"""
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "lib"))
import onboard  # noqa: E402


# ── detect_archetype ────────────────────────────────────────────────────────

def test_detect_frontend_react(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^18"}}))
    arch, scores = onboard.detect_archetype(str(tmp_path))
    assert arch == "frontend"
    assert scores["frontend"] >= 5   # package.json (2) + react (3)


def test_detect_production_docker_compose(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    arch, _ = onboard.detect_archetype(str(tmp_path))
    assert arch == "production"


def test_detect_research_parquet(tmp_path):
    (tmp_path / "prices.parquet").write_text("x")
    arch, _ = onboard.detect_archetype(str(tmp_path))
    assert arch == "research"


def test_detect_tool_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    arch, _ = onboard.detect_archetype(str(tmp_path))
    assert arch == "tool"


def test_detect_infra_terraform(tmp_path):
    (tmp_path / "main.tf").write_text("resource {}\n")
    arch, _ = onboard.detect_archetype(str(tmp_path))
    assert arch == "infra"


def test_detect_empty_dir_defaults_to_tool(tmp_path):
    arch, scores = onboard.detect_archetype(str(tmp_path))
    assert arch == "tool"
    assert all(v == 0 for v in scores.values())


def test_detect_nonexistent_dir_defaults_to_tool(tmp_path):
    arch, _ = onboard.detect_archetype(str(tmp_path / "does-not-exist"))
    assert arch == "tool"


# ── pure detection helpers ──────────────────────────────────────────────────

def test_detect_tech_stack_python_docker_framework(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname='x'\nfastapi = '*'\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
    stack = onboard._detect_tech_stack(str(tmp_path))
    assert "Python" in stack["languages"]
    assert "Docker" in stack["tools"]
    assert "fastapi" in stack["frameworks"]


def test_detect_commands_prefers_pytest_dir(tmp_path):
    (tmp_path / "tests").mkdir()
    cmds = onboard._detect_commands(str(tmp_path), "tool")
    assert cmds.get("test") == "pytest tests/ -v"


def test_detect_commands_npm_scripts(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "jest", "build": "webpack"}}))
    cmds = onboard._detect_commands(str(tmp_path), "frontend")
    assert cmds["test"] == "npm test"
    assert cmds["build"] == "npm run build"


def test_read_project_description_first_paragraph(tmp_path):
    (tmp_path / "README.md").write_text(
        "# Title\n\nThis is the project description line.\n\nSecond paragraph.\n")
    desc = onboard._read_project_description(str(tmp_path))
    assert "project description" in desc


def test_read_project_description_absent(tmp_path):
    assert onboard._read_project_description(str(tmp_path)) is None


# ── init_project: hermetic guard only ───────────────────────────────────────

def test_init_project_missing_dir_returns_false(tmp_path, capsys):
    # project_path provided + not a dir -> returns before any filesystem writes.
    ok = onboard.init_project("ghost", project_path=str(tmp_path / "nope"))
    assert ok is False
    assert "not found" in capsys.readouterr().out.lower()


@pytest.mark.skip(reason="init_project/remove_project write to the REAL "
                         "~/.claude/projects/<slug> (os.makedirs on an expanduser path "
                         "runs even for hook-less archetypes) and remove_project invokes "
                         "real tmux + ~/.tmux-startup.sh — not hermetically redirectable "
                         "without a global os.path.expanduser monkeypatch.")
def test_init_project_full_scaffold_skipped():
    pass
