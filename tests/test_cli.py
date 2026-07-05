"""Smoke tests for clanker CLI — each subcommand exits 0 with valid output."""

import subprocess
import json
import os

CLANKER = os.path.expanduser("~/projects/clanker/bin/clanker")
# Hermetic registry (2026-07-05) — same rationale as tests/test_all.py: pin
# registry-dependent behavior to the committed fixture, not the live registry.
FIXTURE_REGISTRY = os.path.join(os.path.dirname(__file__), "fixtures", "registry.yaml")


def run(args):
    result = subprocess.run(
        [CLANKER] + args,
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "CLANKER_REGISTRY": FIXTURE_REGISTRY},
    )
    return result


def test_version():
    r = run(["version"])
    assert r.returncode == 0
    assert "clanker v" in r.stdout


def test_doctor():
    # `doctor` exits non-zero when it finds issues (correct CLI behavior, real
    # since exit codes propagate) — just assert it ran and produced a report.
    r = run(["doctor"])
    assert r.returncode in (0, 1)
    assert "Registry" in r.stdout


def test_registry_list():
    r = run(["registry", "list"])
    assert r.returncode == 0
    assert "quanta-ai" in r.stdout
    assert "production" in r.stdout


def test_registry_archetype():
    r = run(["registry", "archetype", "zergrush"])
    assert r.returncode == 0
    assert "frontend" in r.stdout


def test_registry_hooks():
    r = run(["registry", "hooks", "quanta-ai"])
    assert r.returncode == 0
    assert "deploy-gate" in r.stdout


def test_sessions():
    r = run(["sessions", "--last", "30"])
    assert r.returncode == 0
    assert "Project" in r.stdout or "Date" in r.stdout


def test_sessions_json():
    r = run(["sessions", "--last", "30", "--json"])
    assert r.returncode == 0
    if r.stdout.strip():
        first_line = r.stdout.strip().split("\n")[0]
        data = json.loads(first_line)
        assert "session_id" in data


def test_analyze_daily():
    r = run(["analyze", "daily"])
    assert r.returncode == 0


def test_analyze_weekly():
    r = run(["analyze", "weekly"])
    assert r.returncode == 0


def test_analyze_errors():
    r = run(["analyze", "errors"])
    assert r.returncode == 0


def test_analyze_slow():
    r = run(["analyze", "slow"])
    assert r.returncode == 0


def test_alert_check():
    r = run(["alert", "check"])
    assert r.returncode == 0
    assert "Health Check" in r.stdout


def test_alert_list():
    r = run(["alert", "list"])
    assert r.returncode == 0


def test_alert_check_cron():
    r = run(["alert", "check", "--cron"])
    assert r.returncode == 0
    if r.stdout.strip():
        data = json.loads(r.stdout.strip())
        assert "checks" in data


def test_propose():
    """Propose should run without error (may or may not generate proposals)."""
    r = run(["propose"])
    assert r.returncode == 0


def test_review_noninteractive():
    """Review in non-interactive mode should list proposals and exit."""
    r = run(["review"])
    assert r.returncode == 0
