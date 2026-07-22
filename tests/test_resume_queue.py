"""Resume-queue hooks — project-scoped clear + writer lock.

The 2026-07-22 host-OOM mass restart proved the old banner instruction
(global `: > queue` truncate) erases OTHER projects' pending entries when many
sessions resume at once. agent-resume-surface.sh now ships a flock'd
project-scoped --clear plus a --selftest covering display scoping,
prefix-collision, and clear survival rules; this wrapper keeps that selftest
inside the pytest gate (ci/fast + ci/full), and pins the writer/clear lock
protocol so the two sides can't silently diverge."""
import os
import subprocess

HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")


def test_agent_resume_surface_selftest_passes():
    r = subprocess.run(["bash", os.path.join(HOOKS, "agent-resume-surface.sh"), "--selftest"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout and "FAIL" not in r.stdout


def test_writer_and_clear_share_the_same_lock_protocol():
    """Both sides must flock <queue>.lock: the surfacer via flock(1) on $Q.lock,
    the detector via fcntl.flock on str(QUEUE) + ".lock". If either side renames
    its lock file the mutual exclusion silently evaporates."""
    surface = open(os.path.join(HOOKS, "agent-resume-surface.sh")).read()
    detect = open(os.path.join(HOOKS, "subagent-resume-detect.py")).read()
    assert '"$Q.lock"' in surface and "flock -w" in surface
    assert 'str(QUEUE) + ".lock"' in detect and "fcntl.flock" in detect
