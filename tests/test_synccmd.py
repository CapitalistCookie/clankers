"""Hermetic tests for clanker sync (checksum parity, apply, pin rewrite).

All roots are temp dirs: repo hooks via a fake repo_root, installed side via a
fake claude dir. Real ~/.claude is never touched (git snapshot is best-effort
and no-ops outside a git repo).
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import synccmd  # noqa: E402


def _mk_repo(tmp):
    root = os.path.join(tmp, "repo")
    hooks = os.path.join(root, "hooks")
    os.makedirs(os.path.join(hooks, "harness"))
    for name in synccmd.REPO_RUN:
        with open(os.path.join(hooks, name), "w") as f:
            f.write(f"#!/bin/bash\necho {name}\n")
    with open(os.path.join(hooks, "context-gauge.py"), "w") as f:
        f.write("print('gauge')\n")
    with open(os.path.join(hooks, "harness", "generic-gate.sh"), "w") as f:
        f.write("#!/bin/bash\nexit 0\n")
    with open(os.path.join(hooks, "harness", "MANIFEST.md"), "w") as f:
        f.write("# manifest — not a hook\n")
    # lib modules ship to <claude>/hooks/lib (the dist hooks' import root).
    # modlike.py would EXIT 1 if apply mistook its "--selftest" string for a
    # real selftest and executed it — proves lib files skip the selftest path.
    os.makedirs(os.path.join(root, "lib"))
    with open(os.path.join(root, "lib", "handoff.py"), "w") as f:
        f.write("def generate_handoff(*a, **kw):\n    return None\n")
    with open(os.path.join(root, "lib", "modlike.py"), "w") as f:
        f.write("import sys\nif '--selftest' in sys.argv:\n    sys.exit(1)\nX = 1\n")
    return root


def test_check_reports_missing_then_apply_reaches_parity():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp)
        claude = os.path.join(tmp, "claude")
        os.makedirs(os.path.join(claude, "hooks"))
        drifted, missing, total = synccmd.check(repo, claude, quiet=True)
        assert not drifted
        assert len(missing) == total  # nothing installed yet
        assert total == len(synccmd.REPO_RUN) + 4  # + gauge + 1 harness + 2 lib

        rc = synccmd.apply(repo, claude)
        assert rc == 0  # also proves lib "--selftest" strings are NOT executed
        drifted, missing, _ = synccmd.check(repo, claude, quiet=True)
        assert not drifted and not missing
        # harness hook installed flat; repo-run under clanker-dist; manifest skipped
        assert os.path.exists(os.path.join(claude, "hooks", "generic-gate.sh"))
        # lib modules land at <claude>/hooks/lib — the exact path the dist
        # hooks resolve as $HOOK_DIR/../lib (dead 07-19→07-22, audit follow-up)
        assert os.path.exists(os.path.join(claude, "hooks", "lib", "handoff.py"))
        assert os.path.exists(
            os.path.join(claude, "hooks", "clanker-dist", "session-start.sh"))
        assert not os.path.exists(os.path.join(claude, "hooks", "MANIFEST.md"))


def test_drift_detected_after_repo_edit():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp)
        claude = os.path.join(tmp, "claude")
        synccmd.apply(repo, claude)
        with open(os.path.join(repo, "hooks", "session-end.sh"), "a") as f:
            f.write("echo changed\n")
        drifted, missing, _ = synccmd.check(repo, claude, quiet=True)
        assert ("repo-run", "session-end.sh") in drifted and not missing


def test_pin_rewrites_settings_and_validates(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp)
        claude = os.path.join(tmp, "claude")
        os.makedirs(claude)
        old_cmd = os.path.join(repo, "hooks", "session-start.sh")
        settings = {
            "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
                {"type": "command", "command": f"bash {old_cmd}"}]}]},
            "other": True,
        }
        with open(os.path.join(claude, "settings.json"), "w") as f:
            json.dump(settings, f)
        # pre-plant stale backups: pin must keep only the newest 3 (audit L6)
        for ts in ("20260101-000001", "20260101-000002", "20260101-000003"):
            with open(os.path.join(claude, f"settings.json.bak-{ts}"), "w") as f:
                f.write("{}")
        rc = synccmd.pin(repo, claude)
        assert rc == 0
        baks = sorted(fn for fn in os.listdir(claude)
                      if fn.startswith("settings.json.bak-"))
        assert len(baks) == 3 and "settings.json.bak-20260101-000001" not in baks
        cfg = json.load(open(os.path.join(claude, "settings.json")))
        cmd = cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert "clanker-dist/session-start.sh" in cmd and repo not in cmd
        assert cfg["other"] is True  # untouched keys survive
        # the pinned path exists
        assert os.path.exists(cmd.split()[-1])
        # a backup was left beside it
        assert any(fn.startswith("settings.json.bak-")
                   for fn in os.listdir(claude))


def test_selftest_failure_fails_apply():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp)
        claude = os.path.join(tmp, "claude")
        bad = os.path.join(repo, "hooks", "harness", "bad-hook.sh")
        with open(bad, "w") as f:
            f.write('#!/bin/bash\nif [ "${1:-}" = "--selftest" ]; then exit 1; fi\n')
        rc = synccmd.apply(repo, claude)
        assert rc == 1
