"""P8: manual proposal path + resolve verb + non-bare ledger except (audit L7).

Hermetic: propose.LEDGER_PATH monkeypatched to tmp; the CLI-level test rides
the conftest CLANKER_DATA tmp store."""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import propose  # noqa: E402

CLANKER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "bin", "clanker")


def _pin(tmp_path, monkeypatch):
    led = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(propose, "LEDGER_PATH", str(led))
    return led


def test_add_proposal_schema_and_duplicate_guard(tmp_path, monkeypatch, capsys):
    _pin(tmp_path, monkeypatch)
    pid = propose.add_proposal("clanker", "Do the thing properly", "less toil")
    assert pid and pid.startswith("prop-") and "-clanker-do-the-thing-properly" in pid
    p = propose._read_ledger()[pid]
    assert p["status"] == "pending" and p["expected_impact"] == "less toil"
    assert p["type"] == "manual" and p["source"] == "manual"
    # exact duplicate refused
    assert propose.add_proposal("clanker", "Do the thing properly", "less toil") is None
    assert "already exists" in capsys.readouterr().err


def test_resolve_by_substring_and_exit_codes(tmp_path, monkeypatch, capsys):
    _pin(tmp_path, monkeypatch)
    propose.add_proposal("clanker", "resume clear archive", "durable queue")
    propose.add_proposal("clanker", "resume detector guard", "fewer false queues")
    # ambiguous substring → 1, names both
    assert propose.resolve_proposal("resume") == 1
    assert "ambiguous" in capsys.readouterr().err
    # unique substring → implemented, last-write-wins
    assert propose.resolve_proposal("clear-archive", note="shipped in test") == 0
    props = propose._read_ledger()
    done = [p for p in props.values() if p["status"] == "implemented"]
    assert len(done) == 1
    assert done[0]["implemented_at"] and "shipped in test" in done[0]["notes"]
    # already-terminal proposals aren't matched by substring
    assert propose.resolve_proposal("clear-archive") == 1
    # unknown → 1
    assert propose.resolve_proposal("no-such-proposal") == 1
    # invalid status → 1
    assert propose.resolve_proposal("detector", status="bogus") == 1


def test_corrupt_ledger_lines_are_counted_not_swallowed(tmp_path, monkeypatch, capsys):
    led = _pin(tmp_path, monkeypatch)
    led.write_text(json.dumps({"id": "prop-x", "status": "pending"}) + "\n"
                   + "{ this is not json }\n"
                   + json.dumps({"no_id_key": True}) + "\n")
    props = propose._read_ledger()
    assert list(props) == ["prop-x"]                 # good row survives
    err = capsys.readouterr().err
    assert "2 corrupt ledger line(s)" in err         # audit L7: no silent loss


def test_cli_add_then_resolve_end_to_end():
    env = {**os.environ}
    r = subprocess.run([CLANKER, "propose", "--add", "--project", "p8proj",
                        "--desc", "cli end to end add", "--impact", "verifies wiring"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "FILED: " in r.stdout
    r = subprocess.run([CLANKER, "propose", "--resolve", "p8proj-cli-end-to-end-add",
                        "--note", "done"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "IMPLEMENTED: " in r.stdout
    # missing required flags → nonzero (exit-code honesty)
    r = subprocess.run([CLANKER, "propose", "--add", "--project", "p8proj"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 1 and "requires" in r.stderr
