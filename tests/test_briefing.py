"""Briefing git-section resilience (M2 regression, 2026-07-22).

A repo with no upstream must still get its Git section. Before the fix,
`git rev-list --count @{upstream}..HEAD` exited nonzero, and the bare `except:`
around the whole git block discarded branch, recent commits, AND dirty state —
precisely the fresh/local-only repos where a cold-start session most needs it.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from briefing import generate_briefing  # noqa: E402

# Neutralize the operator's global git config (init.templateDir installs localci
# hooks) so test repos don't inherit hooks / identity / templates.
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", repo, *args], check=True, env=_GIT_ENV,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    p = str(path)
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    (path / "f.txt").write_text("hi")
    _git(p, "add", "f.txt")
    _git(p, "commit", "-qm", "initial commit")
    return p


def test_git_section_survives_no_upstream(tmp_path):
    repo = _make_repo(tmp_path / "noupstream")
    out = generate_briefing("noupstream", project_path=repo)
    assert out is not None
    assert "Git:" in out                 # section present, not swallowed
    assert "initial commit" in out       # recent commits survived
    assert "unpushed" not in out         # no upstream -> no count, but section stays


def test_git_section_counts_unpushed_with_upstream(tmp_path):
    upstream = _make_repo(tmp_path / "upstream")
    clone = str(tmp_path / "clone")
    subprocess.run(
        ["git", "clone", "-q", upstream, clone], check=True, env=_GIT_ENV,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    (tmp_path / "clone" / "g.txt").write_text("more")
    _git(clone, "add", "g.txt")
    _git(clone, "commit", "-qm", "ahead by one")
    out = generate_briefing("clone", project_path=clone)
    assert out is not None
    assert "Git:" in out
    assert "1 unpushed" in out
