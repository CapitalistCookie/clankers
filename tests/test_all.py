"""Comprehensive tests for all clanker commands — Tiers 1, 2, and 3."""

import subprocess
import json
import os
import tarfile

CLANKER = os.path.expanduser("~/projects/clanker/bin/clanker")
# Hermetic registry (2026-07-05): the suite used to read the LIVE ~/projects/.clanker.yaml
# and broke whenever real projects came or went (quanta-ai's removal). Registry-dependent
# behavior is now pinned to a committed fixture.
FIXTURE_REGISTRY = os.path.join(os.path.dirname(__file__), "fixtures", "registry.yaml")

def run(args):
    env = {**os.environ, "CLANKER_REGISTRY": FIXTURE_REGISTRY}
    return subprocess.run([CLANKER] + args, capture_output=True, text=True, timeout=30, env=env)

# ─── Tier 1 ───────────────────────────────────────────────────────────────────

def test_version():
    r = run(["version"])
    assert r.returncode == 0
    assert "clanker v" in r.stdout

def test_doctor():
    r = run(["doctor"])
    assert "Registry" in r.stdout

def test_doctor_fleet():
    # Fixture registry: quanta-ai has no path on disk -> fleet must report drift
    # and exit nonzero; every registered project gets a row.
    r = run(["doctor", "--fleet"])
    assert r.returncode != 0
    assert "quanta-ai" in r.stdout
    assert "zergrush" in r.stdout
    assert "meet contract" in r.stdout

def test_registry_list():
    r = run(["registry", "list"])
    assert r.returncode == 0
    assert "quanta-ai" in r.stdout

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

def test_sessions_json():
    r = run(["sessions", "--last", "30", "--json"])
    assert r.returncode == 0
    if r.stdout.strip():
        first = json.loads(r.stdout.strip().split("\n")[0])
        assert "session_id" in first

def test_sessions_project_filter():
    r = run(["sessions", "--last", "30", "--project", "zergrush"])
    assert r.returncode == 0

def test_analyze_daily():
    r = run(["analyze", "daily"])
    assert r.returncode == 0

def test_analyze_weekly():
    r = run(["analyze", "weekly"])
    assert r.returncode == 0
    # "Report" with data; "No sessions" on a fresh/empty data dir — both valid.
    assert "Report" in r.stdout or "No sessions" in r.stdout

def test_analyze_errors():
    r = run(["analyze", "errors"])
    assert r.returncode == 0

def test_analyze_slow():
    r = run(["analyze", "slow"])
    assert r.returncode == 0

def test_analyze_by_tool():
    r = run(["analyze", "weekly", "--by", "tool"])
    assert r.returncode == 0

def test_alert_check():
    r = run(["alert", "check"])
    assert r.returncode == 0
    assert "Health Check" in r.stdout

def test_alert_check_cron():
    r = run(["alert", "check", "--cron"])
    assert r.returncode == 0
    if r.stdout.strip():
        data = json.loads(r.stdout.strip())
        assert "checks" in data

def test_alert_list():
    r = run(["alert", "list"])
    assert r.returncode == 0

def test_propose():
    r = run(["propose"])
    assert r.returncode == 0

def test_review_noninteractive():
    r = run(["review"])
    assert r.returncode == 0

# ─── Tier 2 ───────────────────────────────────────────────────────────────────

def test_compile_dry_run():
    r = run(["compile", "--dry-run"])
    assert r.returncode == 0
    assert "article" in r.stdout.lower()

def test_compile():
    r = run(["compile"])
    assert r.returncode == 0

def test_lint_wiki():
    r = run(["lint-wiki"])
    assert r.returncode == 0

def test_init_detect():
    r = run(["init", "zergrush", "--detect"])
    assert r.returncode == 0
    assert "Detected" in r.stdout or "frontend" in r.stdout.lower()

def test_briefing():
    r = run(["briefing", "zergrush"])
    assert r.returncode == 0

def test_briefing_unknown():
    # Iron-law exit-code honesty (2026-07-19): an unknown project is a FAILURE,
    # not "graceful handling" — the error goes to stderr and the exit is nonzero.
    r = run(["briefing", "nonexistent"])
    assert r.returncode != 0
    assert "No briefing data" in r.stderr

# ─── Tier 3 ───────────────────────────────────────────────────────────────────

def test_audit():
    r = run(["audit"])
    assert r.returncode == 0

def test_gc_dry_run():
    r = run(["gc", "--dry-run"])
    assert r.returncode == 0

def test_resources():
    r = run(["resources"])
    assert r.returncode == 0
    assert "RAM" in r.stdout or "Load" in r.stdout

def test_temporal():
    r = run(["temporal", "--last", "30"])
    assert r.returncode == 0

def test_model_versions():
    r = run(["model-versions", "--last", "30"])
    assert r.returncode == 0

def test_snapshot():
    r = run(["snapshot"])
    assert r.returncode == 0
    assert "hooks" in r.stdout.lower() or "Harness" in r.stdout

def test_skills():
    r = run(["skills"])
    assert r.returncode == 0

def test_export(tmp_path):
    # Unique per-test output path (pytest tmp_path) — a fixed /tmp path is a
    # shared resource: the post-commit FULL suite and a pre-push FAST suite both
    # run `pytest tests/` and so both run this test; with a shared path each
    # run's os.remove() races the other's os.path.exists(), an order/timing-
    # dependent flake that intermittently blocked every push.
    out = tmp_path / "clanker-export.tar.gz"
    r = run(["export", "--output", str(out)])
    assert r.returncode == 0
    assert out.exists()
    assert tarfile.is_tarfile(out)  # genuinely a valid tarball, not just a touched file

# ─── New Tier 3 Commands ─────────────────────────────────────────────────────

def test_cross_project():
    r = run(["cross-project", "--last", "30"])
    assert r.returncode == 0

def test_cross_project_suggest():
    r = run(["cross-project", "--suggest", "--last", "30"])
    assert r.returncode == 0

def test_contracts_scan():
    # Target a project whose path exists on disk (the suite already assumes
    # ~/projects/clanker at CLANKER above) — contracts now exits 1 on a missing
    # path instead of silently scanning nothing (exit-code honesty, 2026-07-19).
    r = run(["contracts", "clanker"])
    assert r.returncode == 0

def test_contracts_check():
    r = run(["contracts", "clanker", "--check"])
    assert r.returncode == 0

def test_contracts_unknown_path():
    r = run(["contracts", "quanta-ai"])  # fixture project with no path on disk
    assert r.returncode != 0

def test_context():
    r = run(["context", "--last", "30"])
    assert r.returncode == 0

def test_context_project():
    r = run(["context", "--project", "zergrush", "--last", "30"])
    assert r.returncode == 0

def test_plugins_list():
    r = run(["plugins", "list"])
    assert r.returncode == 0

def test_plugins_run_analyzers():
    r = run(["plugins", "run-analyzers"])
    assert r.returncode == 0

def test_decompose():
    r = run(["decompose", "refactor the entire billing system from scratch"])
    assert r.returncode == 0

def test_decompose_simple():
    r = run(["decompose", "fix typo"])
    assert r.returncode == 0

def test_regression_check():
    r = run(["regression", "check"])
    assert r.returncode == 0

def test_regression_anomalies():
    r = run(["regression", "anomalies"])
    assert r.returncode == 0

# ─── Universal (Platform Independence) ───────────────────────────────────────

def test_ingest_session_start():
    r = run(["ingest", "--event", "session_start", "--agent", "test", "--project", "test-project"])
    assert r.returncode == 0
    assert len(r.stdout.strip()) > 0  # returns session ID

def test_ingest_session_end():
    r = run(["ingest", "--event", "session_end", "--agent", "test", "--project", "test-project",
             "--duration", "60", "--outcome", "commit", "--summary", "test session"])
    assert r.returncode == 0

def test_ingest_missing_fields():
    """Ingest with no event should read stdin — empty stdin should error gracefully."""
    r = subprocess.run([CLANKER, "ingest"], capture_output=True, text=True, timeout=5,
                       input="")
    assert r.returncode == 1  # error on empty stdin

def test_ingest_json_stdin():
    event = json.dumps({
        "event": "session_end",
        "timestamp": "2026-04-07T00:00:00Z",
        "agent": "test-stdin",
        "project": "test-project"
    })
    r = subprocess.run([CLANKER, "ingest"], capture_output=True, text=True, timeout=5,
                       input=event)
    assert r.returncode == 0

def test_wrap_simple_command():
    r = run(["wrap", "echo", "hello"])
    assert r.returncode == 0

def test_events_module():
    """Test the events module directly."""
    r = subprocess.run(["python3", "-c", """
import sys
sys.path.insert(0, 'lib')
from events import validate_event, create_event, generate_session_id

# Test create_event
e = create_event("session_start", "test-agent", "test-project")
assert e["event"] == "session_start"
assert e["agent"] == "test-agent"
assert "session_id" in e

# Test validation
valid, errors, _ = validate_event({"event": "session_end", "timestamp": "now", "agent": "x", "project": "y"})
assert valid

# Test missing required
valid, errors, _ = validate_event({"event": "session_end"})
assert not valid

print("events module OK")
"""], capture_output=True, text=True, cwd=os.path.dirname(CLANKER) + "/..")
    assert r.returncode == 0
    assert "OK" in r.stdout
