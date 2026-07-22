"""Resume-queue hooks — project-scoped clear + writer lock.

The 2026-07-22 host-OOM mass restart proved the old banner instruction
(global `: > queue` truncate) erases OTHER projects' pending entries when many
sessions resume at once. agent-resume-surface.sh now ships a flock'd
project-scoped --clear plus a --selftest covering display scoping,
prefix-collision, and clear survival rules; this wrapper keeps that selftest
inside the pytest gate (ci/fast + ci/full), and pins the writer/clear lock
protocol so the two sides can't silently diverge."""
import json
import os
import subprocess

HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
DETECT = os.path.join(HOOKS, "subagent-resume-detect.py")


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
    detect = open(DETECT).read()
    assert '"$Q.lock"' in surface and "flock -w" in surface
    assert 'str(QUEUE) + ".lock"' in detect and "fcntl.flock" in detect


def test_detector_skips_main_session_transcripts(tmp_path):
    """OOM-restart false-positive class (2026-07-22, twice in one morning):
    events carrying a MAIN session transcript — not agent-*.jsonl — queued the
    operator's root prompt as a 'killed subagent' while the session was in fact
    resuming fine. Non-agent transcripts must not queue; agent-* ones still do.
    CLANKER_RESUME_QUEUE overrides the queue path (same env var the surfacer
    honors), keeping this hermetic."""
    q = tmp_path / "q.jsonl"
    env = {**os.environ, "CLANKER_RESUME_QUEUE": str(q)}

    main_tp = tmp_path / "a0946a9b-main-session.jsonl"
    main_tp.write_text(json.dumps({"type": "user", "message": {
        "content": "please audit this system end to end today"}}) + "\nRate limited\n")
    ev = json.dumps({"transcript_path": str(main_tp), "cwd": str(tmp_path)})
    r = subprocess.run(["python3", DETECT], input=ev, capture_output=True,
                       text=True, env=env, timeout=30)
    assert r.returncode == 0
    assert not q.exists(), "main-session transcript must not be queued"

    agent_tp = tmp_path / "agent-ab12cd34.jsonl"
    agent_tp.write_text(json.dumps({"type": "user", "message": {
        "content": "subtask: fix the flaky auth test properly"}}) + "\nRate limited\n")
    ev = json.dumps({"transcript_path": str(agent_tp), "cwd": str(tmp_path)})
    r = subprocess.run(["python3", DETECT], input=ev, capture_output=True,
                       text=True, env=env, timeout=30)
    assert r.returncode == 0
    entry = json.loads(q.read_text().splitlines()[0])
    assert entry["status"] == "pending" and "flaky auth" in entry["prompt"]
