"""Hermetic tests for clanker sync (checksum parity, apply, pin rewrite).

All roots are temp dirs: repo hooks via a fake repo_root, installed side via a
fake claude dir. Real ~/.claude is never touched (git snapshot is best-effort
and no-ops outside a git repo).
"""

import json
import os
import shutil
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


def _state(claude):
    with open(os.path.join(claude, "hooks", synccmd.STATE_NAME)) as f:
        return json.load(f)["files"]


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
        assert len(_state(claude)) == total  # first apply creates the baseline
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


# ── hand-edit guard (2026-09-24) ────────────────────────────────────────────
# That night an operator rewire edited six installed hooks and archived nine;
# an apply from the old repo state would have reverted all of it. apply now
# refuses to overwrite an installed file that matches neither the repo copy
# nor the hash recorded at the last apply, unless --force.

GATE_REL = os.path.join("hooks", "generic-gate.sh")


def _gate_paths(repo, claude):
    return (os.path.join(repo, "hooks", "harness", "generic-gate.sh"),
            os.path.join(claude, GATE_REL))


def test_apply_identical_is_a_noop_without_snapshot(monkeypatch, capsys):
    """Identical: nothing is copied, and no git snapshot runs. A snapshot does
    `git add -A hooks settings.json` in ~/.claude, which would commit other
    sessions' pending edits for an apply that changes nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp)
        claude = os.path.join(tmp, "claude")
        assert synccmd.apply(repo, claude) == 0
        copies, snaps = [], []
        real_copy2 = shutil.copy2
        monkeypatch.setattr(synccmd.shutil, "copy2",
                            lambda s, d: (copies.append(d), real_copy2(s, d))[1])
        monkeypatch.setattr(synccmd, "_git_snapshot", lambda c, m: snaps.append(m))
        capsys.readouterr()
        assert synccmd.apply(repo, claude) == 0
        assert "nothing to install" in capsys.readouterr().out
        assert copies == [] and snaps == []
        src, _ = _gate_paths(repo, claude)
        assert _state(claude)[GATE_REL] == synccmd._sha(src)


def test_apply_updates_when_repo_changed_and_install_untouched(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp)
        claude = os.path.join(tmp, "claude")
        assert synccmd.apply(repo, claude) == 0
        src, dst = _gate_paths(repo, claude)
        with open(src, "a") as f:
            f.write("# the repo moved on\n")
        capsys.readouterr()
        assert synccmd.apply(repo, claude) == 0
        assert "installed  [harness ] generic-gate.sh" in capsys.readouterr().out
        assert synccmd._sha(dst) == synccmd._sha(src)
        assert _state(claude)[GATE_REL] == synccmd._sha(src)


def test_apply_refuses_hand_edited_install_and_installs_nothing(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp)
        claude = os.path.join(tmp, "claude")
        assert synccmd.apply(repo, claude) == 0
        src, dst = _gate_paths(repo, claude)
        applied = synccmd._sha(dst)
        with open(dst, "a") as f:
            f.write("# operator fix, made in place\n")
        with open(src, "a") as f:
            f.write("# an older idea in the repo\n")
        hand, repo_sha = synccmd._sha(dst), synccmd._sha(src)
        # a second, ordinary repo change must not ship either: all or nothing
        other_src = os.path.join(repo, "hooks", "session-end.sh")
        other_dst = os.path.join(claude, "hooks", "clanker-dist", "session-end.sh")
        other_before = synccmd._sha(other_dst)
        with open(other_src, "a") as f:
            f.write("echo newer\n")
        synccmd.check(repo, claude)
        assert "apply will refuse" in capsys.readouterr().out
        assert synccmd.apply(repo, claude) == 1
        err = capsys.readouterr().err
        assert synccmd._sha(dst) == hand                    # the fix survives
        assert synccmd._sha(other_dst) == other_before       # nothing shipped
        assert "REFUSED" in err and "--force" in err
        assert repo_sha in err and hand in err and applied in err   # all three hashes
        assert _state(claude)[GATE_REL] == applied           # record unchanged


def test_apply_force_overwrites_hand_edited_install(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp)
        claude = os.path.join(tmp, "claude")
        assert synccmd.apply(repo, claude) == 0
        src, dst = _gate_paths(repo, claude)
        with open(dst, "a") as f:
            f.write("# operator fix, made in place\n")
        assert synccmd.apply(repo, claude) == 1
        capsys.readouterr()
        assert synccmd.apply(repo, claude, force=True) == 0
        assert "--force: overwrote" in capsys.readouterr().out
        assert synccmd._sha(dst) == synccmd._sha(src)
        assert _state(claude)[GATE_REL] == synccmd._sha(src)
        assert synccmd.apply(repo, claude) == 0             # parity again


def test_apply_without_a_record_refuses_a_drifted_install(capsys):
    """The 2026-09-24 case: no state file yet, installed copies rewired by
    hand, the repo still on the old copies. No record = no proof that sync
    wrote the installed copy, so a drifted file counts as hand-edited."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp)
        claude = os.path.join(tmp, "claude")
        _, dst = _gate_paths(repo, claude)
        os.makedirs(os.path.dirname(dst))
        with open(dst, "w") as f:
            f.write("#!/bin/bash\n# the rewired installed copy\nexit 0\n")
        assert synccmd.apply(repo, claude) == 1
        assert "rewired installed copy" in open(dst).read()
        assert "(no record)" in capsys.readouterr().err
        assert not os.path.exists(
            os.path.join(claude, "hooks", "clanker-dist", "session-start.sh"))


def test_apply_refuses_to_reinstall_a_file_removed_after_apply(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp)
        claude = os.path.join(tmp, "claude")
        assert synccmd.apply(repo, claude) == 0
        _, dst = _gate_paths(repo, claude)
        os.rename(dst, os.path.join(tmp, "archived-generic-gate.sh"))
        drifted, missing, _ = synccmd.check(repo, claude, quiet=True)
        assert ("harness", dst, "not-installed") in missing
        capsys.readouterr()
        assert synccmd.apply(repo, claude) == 1
        assert "removed after the last apply" in capsys.readouterr().err
        assert not os.path.exists(dst)
        assert synccmd.apply(repo, claude, force=True) == 0
        assert os.path.exists(dst)


def test_record_baseline_records_only_pairs_in_parity():
    """For an install that reached parity by hand copies: record it, install
    nothing, and leave drifted files without a record."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp)
        claude = os.path.join(tmp, "claude")
        src, dst = _gate_paths(repo, claude)
        os.makedirs(os.path.join(claude, "hooks", "clanker-dist"))
        shutil.copy2(src, dst)
        drift = os.path.join(claude, "hooks", "clanker-dist", "session-end.sh")
        with open(drift, "w") as f:
            f.write("#!/bin/bash\necho edited by hand\n")
        assert synccmd.record_baseline(repo, claude) == 1
        assert _state(claude) == {GATE_REL: synccmd._sha(src)}
        assert open(drift).read() == "#!/bin/bash\necho edited by hand\n"
        assert not os.path.exists(os.path.join(claude, "hooks", "lib"))


def test_pin_aborts_when_apply_refuses():
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp)
        claude = os.path.join(tmp, "claude")
        dist = os.path.join(claude, "hooks", "clanker-dist")
        os.makedirs(dist)
        with open(os.path.join(dist, "session-start.sh"), "w") as f:
            f.write("#!/bin/bash\necho edited by hand\n")
        old_cmd = os.path.join(repo, "hooks", "session-start.sh")
        raw = json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": f"bash {old_cmd}"}]}]}})
        with open(os.path.join(claude, "settings.json"), "w") as f:
            f.write(raw)
        assert synccmd.pin(repo, claude) == 1
        assert open(os.path.join(claude, "settings.json")).read() == raw
