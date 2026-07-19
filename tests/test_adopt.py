"""Hermetic tests for lib/adopt.py — the project-contract executable spec.

`project_checks()` is the machine-readable definition of "in the system"; every
row is asserted here against a real temp git repo built to trip (or satisfy) that
one contract clause. `doctor_fleet()` / `adopt()` are exercised the same way.

Isolation:
  - CLANKER_REGISTRY  -> a temp fixture yaml written per test (never the live one).
  - CLANKER_PROJECT_ROOTS -> an EMPTY temp dir, so Registry.projects' auto-discovery
    (scan_projects) finds nothing and the fleet view contains only fixture projects.
No real tmux, no network, no writes outside tmp_path.
"""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "lib"))
import adopt  # noqa: E402
import registry  # noqa: E402

# The line-limit row's label carries a real U+2264 — assertions must match byte-for-byte.
CLAUDE_ROW = "CLAUDE.md ≤150 lines"


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True)


def _make_repo(base, name, *, git=True, claude_lines=10, state=True, ci=True,
               remote=True, secret=False, files=None):
    """Build a temp repo tuned to satisfy/violate individual contract rows."""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    if git:
        _git(str(d), "init", "-q", "-b", "main")
        _git(str(d), "config", "user.email", "t@t")
        _git(str(d), "config", "user.name", "t")
        if remote:
            _git(str(d), "remote", "add", "origin", f"git@example.com:acme/{name}.git")
    if claude_lines:
        (d / "CLAUDE.md").write_text("".join(f"line {i}\n" for i in range(claude_lines)))
    if state:
        (d / "STATUS.md").write_text("# STATUS\n")
    if ci:
        (d / "ci").mkdir(exist_ok=True)
        (d / "ci" / "fast.sh").write_text("#!/usr/bin/env bash\necho ok\n")
    if secret:
        # synthetic fixture in the allowlisted glpat-A{20} shape (.gitleaks.toml)
        (d / "leak.md").write_text("gitlab_token: glpat-" + "A" * 20 + "\n")
    for rel, content in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


def _write_registry(path, projects):
    with open(path, "w") as f:
        yaml.safe_dump({"projects": projects}, f)


def _rows(checks):
    """[(label, ok, detail)] -> {label: (ok, detail)} (labels are unique per run)."""
    return {label: (ok, detail) for label, ok, detail in checks}


@pytest.fixture
def env(tmp_path, monkeypatch):
    roots = tmp_path / "empty_roots"
    roots.mkdir()
    repos = tmp_path / "repos"
    repos.mkdir()
    reg_path = tmp_path / "registry.yaml"
    monkeypatch.setenv("CLANKER_PROJECT_ROOTS", str(roots))
    monkeypatch.setenv("CLANKER_REGISTRY", str(reg_path))
    return SimpleNamespace(tmp=tmp_path, roots=roots, repos=repos, reg_path=reg_path)


# ── project_checks: core contract rows ──────────────────────────────────────

def test_full_contract_met(env):
    repo = _make_repo(env.repos, "alpha")
    _write_registry(env.reg_path, {"alpha": {"archetype": "tool", "path": str(repo)}})
    checks = adopt.project_checks("alpha")
    r = _rows(checks)
    assert r["path exists"][0]
    assert r["registered"][0]
    assert r["git repo"][0]
    assert r[CLAUDE_ROW][0]
    assert r["state file"][0]
    assert r["ci/fast.sh or .localci"][0]
    assert r["remote"][0]
    assert r["no plaintext secrets"][0]
    assert all(ok for _, ok, _ in checks)
    assert adopt.doctor_project("alpha") == 0


def test_path_missing_returns_single_row(env):
    missing = env.repos / "ghost"  # never created
    _write_registry(env.reg_path, {"ghost": {"archetype": "tool", "path": str(missing)}})
    checks = adopt.project_checks("ghost")
    assert len(checks) == 1
    label, ok, _ = checks[0]
    assert label == "path exists" and ok is False
    assert adopt.doctor_project("ghost") == 1


def test_unregistered_row(env, monkeypatch):
    # A path on disk that the registry does NOT know about. get_path is pinned so
    # the (registered=False) branch is reachable without pointing at real ~/projects.
    repo = _make_repo(env.repos, "orphan")
    _write_registry(env.reg_path, {})  # empty registry
    monkeypatch.setattr(registry.Registry, "get_path", lambda self, n: str(repo))
    r = _rows(adopt.project_checks("orphan"))
    assert r["path exists"][0] is True
    assert r["registered"][0] is False
    assert "clanker adopt" in r["registered"][1]


def test_non_git_repo(env):
    repo = _make_repo(env.repos, "nogit", git=False)
    _write_registry(env.reg_path, {"nogit": {"archetype": "tool", "path": str(repo)}})
    r = _rows(adopt.project_checks("nogit"))
    assert r["git repo"][0] is False
    # remote / secret rows are git-gated: absent for a non-git tree.
    assert "remote" not in r
    assert "no plaintext secrets" not in r


def test_claude_md_missing(env):
    repo = _make_repo(env.repos, "b1", claude_lines=0)
    _write_registry(env.reg_path, {"b1": {"archetype": "tool", "path": str(repo)}})
    r = _rows(adopt.project_checks("b1"))
    assert r[CLAUDE_ROW] == (False, "missing")


def test_claude_md_too_long(env):
    repo = _make_repo(env.repos, "b2", claude_lines=151)
    _write_registry(env.reg_path, {"b2": {"archetype": "tool", "path": str(repo)}})
    r = _rows(adopt.project_checks("b2"))
    assert r[CLAUDE_ROW] == (False, "151 lines")


def test_claude_md_ok_at_boundary(env):
    repo = _make_repo(env.repos, "b3", claude_lines=150)
    _write_registry(env.reg_path, {"b3": {"archetype": "tool", "path": str(repo)}})
    r = _rows(adopt.project_checks("b3"))
    assert r[CLAUDE_ROW] == (True, "150 lines")


def test_state_file_absent(env):
    repo = _make_repo(env.repos, "s1", state=False)
    _write_registry(env.reg_path, {"s1": {"archetype": "tool", "path": str(repo)}})
    r = _rows(adopt.project_checks("s1"))
    assert r["state file"][0] is False
    assert "none" in r["state file"][1]


def test_state_file_handoff_variant_counts(env):
    repo = _make_repo(env.repos, "s2", state=False, files={"docs/HANDOFF.md": "x\n"})
    _write_registry(env.reg_path, {"s2": {"archetype": "tool", "path": str(repo)}})
    r = _rows(adopt.project_checks("s2"))
    assert r["state file"][0] is True
    assert "docs/HANDOFF.md" in r["state file"][1]


def test_ci_localci_variant(env):
    repo = _make_repo(env.repos, "c1", ci=False, files={".localci": "\n"})
    _write_registry(env.reg_path, {"c1": {"archetype": "tool", "path": str(repo)}})
    r = _rows(adopt.project_checks("c1"))
    assert r["ci/fast.sh or .localci"][0] is True


def test_ci_absent(env):
    repo = _make_repo(env.repos, "c2", ci=False)
    _write_registry(env.reg_path, {"c2": {"archetype": "tool", "path": str(repo)}})
    r = _rows(adopt.project_checks("c2"))
    assert r["ci/fast.sh or .localci"][0] is False


def test_remote_absent(env):
    repo = _make_repo(env.repos, "rm1", remote=False)
    _write_registry(env.reg_path, {"rm1": {"archetype": "tool", "path": str(repo)}})
    r = _rows(adopt.project_checks("rm1"))
    assert r["remote"][0] is False
    assert "DR risk" in r["remote"][1]


def test_plaintext_secret_detected(env):
    repo = _make_repo(env.repos, "sec1", secret=True)
    _write_registry(env.reg_path, {"sec1": {"archetype": "tool", "path": str(repo)}})
    r = _rows(adopt.project_checks("sec1", include_secrets=True))
    assert r["no plaintext secrets"][0] is False


def test_clean_repo_has_no_secret_hit(env):
    repo = _make_repo(env.repos, "sec2")
    _write_registry(env.reg_path, {"sec2": {"archetype": "tool", "path": str(repo)}})
    r = _rows(adopt.project_checks("sec2", include_secrets=True))
    assert r["no plaintext secrets"][0] is True


def test_include_secrets_false_omits_secret_row(env):
    repo = _make_repo(env.repos, "sec3", secret=True)
    _write_registry(env.reg_path, {"sec3": {"archetype": "tool", "path": str(repo)}})
    r = _rows(adopt.project_checks("sec3", include_secrets=False))
    assert "no plaintext secrets" not in r  # skipped for the fleet-speed path


# ── project_checks: archetype-specific rows ─────────────────────────────────

def test_archetype_research_missing_spec(env):
    repo = _make_repo(env.repos, "r1")
    _write_registry(env.reg_path, {"r1": {"archetype": "research", "path": str(repo)}})
    r = _rows(adopt.project_checks("r1"))
    assert r["research: _spec/ governance dir"][0] is False
    # the prereg row only appears once a _spec/ dir exists.
    assert "research: prereg.yml present" not in r


def test_archetype_research_spec_without_prereg(env):
    repo = _make_repo(env.repos, "r2", files={"r2_spec/README.md": "x\n"})
    _write_registry(env.reg_path, {"r2": {"archetype": "research", "path": str(repo)}})
    r = _rows(adopt.project_checks("r2"))
    assert r["research: _spec/ governance dir"][0] is True
    assert r["research: prereg.yml present"][0] is False


def test_archetype_research_full(env):
    repo = _make_repo(env.repos, "r3", files={"r3_spec/prereg.yml": "prereg: 1\n"})
    _write_registry(env.reg_path, {"r3": {"archetype": "research", "path": str(repo)}})
    r = _rows(adopt.project_checks("r3"))
    assert r["research: _spec/ governance dir"][0] is True
    assert r["research: prereg.yml present"][0] is True


def test_archetype_infra_no_backup(env):
    repo = _make_repo(env.repos, "in1")  # STATUS.md exists but never says 'backup'
    _write_registry(env.reg_path, {"in1": {"archetype": "infra", "path": str(repo)}})
    r = _rows(adopt.project_checks("in1"))
    assert r["infra: backup tracked in STATUS"][0] is False


def test_archetype_infra_with_backup(env):
    repo = _make_repo(env.repos, "in2", state=False,
                      files={"STATUS.md": "# STATUS\nnightly Backup to B2 offsite\n"})
    _write_registry(env.reg_path, {"in2": {"archetype": "infra", "path": str(repo)}})
    r = _rows(adopt.project_checks("in2"))
    assert r["infra: backup tracked in STATUS"][0] is True  # case-insensitive match


def test_archetype_production_needs_full_ci(env):
    repo = _make_repo(env.repos, "p1")  # ci/fast.sh only
    _write_registry(env.reg_path, {"p1": {"archetype": "production", "path": str(repo)}})
    r = _rows(adopt.project_checks("p1"))
    assert r["deployable: ci/full.sh"][0] is False


def test_archetype_frontend_with_full_ci(env):
    repo = _make_repo(env.repos, "fe1", files={"ci/full.sh": "#!/usr/bin/env bash\n"})
    _write_registry(env.reg_path, {"fe1": {"archetype": "frontend", "path": str(repo)}})
    r = _rows(adopt.project_checks("fe1"))
    assert r["deployable: ci/full.sh"][0] is True


# ── doctor_fleet ────────────────────────────────────────────────────────────

def test_doctor_fleet_all_met(env, capsys):
    repo = _make_repo(env.repos, "alpha")
    _write_registry(env.reg_path, {"alpha": {"archetype": "tool", "path": str(repo)}})
    rc = adopt.doctor_fleet(include_secrets=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out
    assert "meet contract" in out


def test_doctor_fleet_reports_drift(env, capsys):
    good = _make_repo(env.repos, "alpha")
    _write_registry(env.reg_path, {
        "alpha": {"archetype": "tool", "path": str(good)},
        "ghost": {"archetype": "tool", "path": str(env.repos / "ghost-missing")},
    })
    rc = adopt.doctor_fleet(include_secrets=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "ghost" in out
    assert "drifted" in out


# ── adopt(): register-idempotency + scaffold-only-missing ────────────────────

def test_adopt_registers_and_scaffolds(env, capsys):
    repo = _make_repo(env.repos, "newproj", claude_lines=0, state=False, ci=False)
    _write_registry(env.reg_path, {})
    rc = adopt.adopt(str(repo))
    out = capsys.readouterr().out

    data = yaml.safe_load(open(env.reg_path))
    assert "newproj" in data["projects"]
    assert data["projects"]["newproj"]["path"] == str(repo)
    assert data["projects"]["newproj"]["archetype"] == "tool"
    # scaffolded every missing contract file (never the .git-only originals)
    assert (repo / "CLAUDE.md").exists()
    assert (repo / "STATUS.md").exists()
    assert (repo / "ci" / "fast.sh").exists()
    assert os.access(repo / "ci" / "fast.sh", os.X_OK)  # CI_STUB gets +x
    assert (repo / ".claude" / "skills").is_dir()
    assert "registered 'newproj'" in out
    assert rc == 0  # fully converged: remote present + stubs satisfy the contract


def test_adopt_registration_is_idempotent(env, capsys):
    repo = _make_repo(env.repos, "dup")
    _write_registry(env.reg_path, {})
    adopt.adopt(str(repo))
    capsys.readouterr()  # drop first-run output
    adopt.adopt(str(repo))
    out = capsys.readouterr().out
    assert "already registered" in out
    data = yaml.safe_load(open(env.reg_path))
    assert list(data["projects"]) == ["dup"]  # not duplicated / no extra entries


def test_adopt_scaffolds_only_missing(env):
    repo = _make_repo(env.repos, "partial", state=False, ci=False)
    (repo / "CLAUDE.md").write_text("# Custom CLAUDE\nhand-written, do not clobber\n")
    _write_registry(env.reg_path, {})
    adopt.adopt(str(repo))
    # existing CLAUDE.md preserved verbatim; only the genuinely-missing pieces made.
    assert (repo / "CLAUDE.md").read_text() == "# Custom CLAUDE\nhand-written, do not clobber\n"
    assert "Seeded by `clanker adopt`" not in (repo / "CLAUDE.md").read_text()
    assert (repo / "STATUS.md").exists()
    assert (repo / "ci" / "fast.sh").exists()


def test_adopt_missing_dir_without_new_refuses(env, capsys):
    missing = env.repos / "does-not-exist"
    _write_registry(env.reg_path, {})
    rc = adopt.adopt(str(missing))
    err = capsys.readouterr().err
    assert rc == 1
    assert "no such directory" in err
    data = yaml.safe_load(open(env.reg_path)) or {}
    assert not data.get("projects")  # nothing registered on the refusal path


def test_adopt_new_creates_and_git_inits(env):
    target = env.repos / "fromscratch"  # does not exist yet
    _write_registry(env.reg_path, {})
    adopt.adopt(str(target), new=True)
    assert (target / ".git").is_dir()          # created + git init'd
    assert (target / "CLAUDE.md").exists()      # scaffolded
    assert (target / "STATUS.md").exists()
    data = yaml.safe_load(open(env.reg_path))
    assert "fromscratch" in data["projects"]
