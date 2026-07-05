"""Hermetic tests for lib/projects.py — the cwd→project resolver + repo scan.

Everything runs in temp dirs with real `git init` (no host repos, no network).
"""
import os
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "lib"))
import projects  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """A projects container with two repos, one extra repo elsewhere, and a
    non-repo dir — wired through CLANKER_PROJECT_ROOTS."""
    container = tmp_path / "projects"
    container.mkdir()
    elsewhere = tmp_path / "work"
    elsewhere.mkdir()

    for base, name in [(container, "alpha"), (container, "beta"), (elsewhere, "gamma")]:
        d = base / name
        d.mkdir()
        _git(str(d), "init", "-q")
        _git(str(d), "config", "user.email", "t@t")
        _git(str(d), "config", "user.name", "t")
        (d / "f.txt").write_text("x")
        _git(str(d), "add", "-A")
        _git(str(d), "commit", "-qm", "init")
    (container / "not_a_repo").mkdir()

    monkeypatch.setenv("CLANKER_PROJECT_ROOTS", f"{container}:{elsewhere}")
    monkeypatch.setenv("CLANKER_REGISTRY", str(tmp_path / "no-such-registry.yaml"))
    monkeypatch.setattr(projects, "project_roots",
                        lambda: [str(container), str(elsewhere)])
    projects.reset_caches()
    return {"container": container, "elsewhere": elsewhere, "tmp": tmp_path}


def test_resolve_repo_by_subdir(roots):
    repo = roots["container"] / "alpha"
    sub = repo / "deep" / "nested"
    sub.mkdir(parents=True)
    assert projects.resolve_project(str(repo)) == "alpha"
    assert projects.resolve_project(str(sub)) == "alpha"


def test_resolve_repo_outside_container(roots):
    # A repo not under ~/projects still resolves to its own name (git-first).
    assert projects.resolve_project(str(roots["elsewhere"] / "gamma")) == "gamma"


def test_worktree_collapses_to_parent(roots):
    repo = roots["container"] / "alpha"
    wt = roots["tmp"] / "alpha-feature"
    _git(str(repo), "worktree", "add", "-q", str(wt))
    # A linked worktree attributes to the MAIN repo, not "alpha-feature".
    assert projects.resolve_project(str(wt)) == "alpha"


def test_symlink_to_repo_collapses_and_is_not_a_phantom(roots):
    # A convenience symlink to a repo (placed in a scanned root) resolves to the
    # canonical repo AND is not listed as a separate scanned project.
    link = roots["elsewhere"] / "alpha_alias"
    os.symlink(str(roots["container"] / "alpha"), str(link))
    projects.reset_caches()
    assert projects.resolve_project(str(link)) == "alpha"
    found = projects.scan_projects()
    assert "alpha_alias" not in found
    assert "alpha" in found


def test_non_repo_under_container_uses_dir_name(roots):
    assert projects.resolve_project(str(roots["container"] / "not_a_repo")) == "not_a_repo"


def test_unrelated_path_is_global(roots):
    assert projects.resolve_project(str(roots["tmp"] / "nowhere")) == "global"
    assert projects.resolve_project("") == "global"


def test_home_root_does_not_make_every_subdir_a_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(home)) if p.startswith("~") else p)
    monkeypatch.setattr(projects, "project_roots", lambda: [str(home)])
    projects.reset_caches()
    # A plain (non-git) dir under $HOME is NOT a project.
    assert projects.resolve_project(str(home / ".config")) == "global"


def test_scan_finds_repos_not_plain_dirs(roots):
    found = projects.scan_projects()
    assert set(found) == {"alpha", "beta", "gamma"}
    assert "not_a_repo" not in found
    assert found["alpha"] == str(roots["container"] / "alpha")


# ── normalization (resolution-layer canonicalization) ──

def test_normalize_dead_worktree_fragment_folds_to_parent(monkeypatch):
    monkeypatch.setattr(projects, "_live_repos",
                        lambda: frozenset({"hftlogger", "hftlogger-rust", "infra"}))
    monkeypatch.setattr(projects, "_aliases", lambda: {})
    # `<repo>-<suffix>` whose worktree dir is gone folds to the LONGEST repo prefix.
    assert projects._normalize("infra-pricechange") == "infra"
    assert projects._normalize("hftlogger-rust-r7-t4") == "hftlogger-rust"  # longest


def test_normalize_case_insensitive_and_alias(monkeypatch):
    monkeypatch.setattr(projects, "_live_repos",
                        lambda: frozenset({"zergrush", "eigenstate"}))
    monkeypatch.setattr(projects, "_aliases", lambda: {"Eigenstate-V2": "eigenstate"})
    assert projects._normalize("Zergrush") == "zergrush"        # case-fold to live repo
    assert projects._normalize("Eigenstate-V2") == "eigenstate"  # explicit alias
    assert projects._normalize("global") == "global"
    assert projects._normalize("totally-unknown") == "totally-unknown"  # left alone


def test_normalize_does_not_overfold_a_real_sibling_repo(monkeypatch):
    # polymarket-protocol is its OWN repo — must not fold into polymarket.
    monkeypatch.setattr(projects, "_live_repos",
                        lambda: frozenset({"polymarket", "polymarket-protocol"}))
    monkeypatch.setattr(projects, "_aliases", lambda: {})
    assert projects._normalize("polymarket-protocol") == "polymarket-protocol"
