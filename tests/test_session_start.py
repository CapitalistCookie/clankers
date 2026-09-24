"""hooks/session-start.sh: the cold-start brief contract (2026-09-24).

additionalContext is at most 1,200 bytes, shaped
    clanker: <project> · <archetype> · <branch>@<short-sha> · <N> dirty
    NOW: <STATUS.md `## NOW` section, ≤800 B at a line boundary>
    state: STATUS.md (<bytes> B) · index: <router file> · alerts: <N> open for this project (…)
    git: <git log --oneline -3>
The hook it replaced pasted `head -30 STATUS.md` (19.9 KB on one repo) and
spawned two jq per alert file (9.8 s per start over ~1,100 alerts).

Hermetic: HOME, CLANKER_DATA, CLANKER_REGISTRY and CLAUDE_ENV_FILE all point
into tmp_path; CLAUDE_PROJECT_DIR is dropped so the payload's cwd decides.
"""
import json
import os
import shutil
import subprocess
import time
import uuid
from types import SimpleNamespace

import pytest

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "hooks", "session-start.sh")
BUDGET = 1200
# The contract's acceptance case: a real STATUS.md whose `head -30` was 19.9 KB.
# Copied into tmp at test time, never committed; skipped where it doesn't exist.
REAL_STATUS = os.environ.get("CLANKER_TEST_REAL_STATUS",
                             os.path.expanduser("~/projects/hl-aster-arb/STATUS.md"))


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    *args], check=True, capture_output=True)


def _repo(path, subjects=("first",), add_all=False):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True,
                   capture_output=True)
    if add_all:
        _git(path, "add", "-A")
    for s in subjects:
        _git(path, "commit", "-q", "--allow-empty", "-m", s)
    return path


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    data = tmp_path / "data"
    (data / "alerts").mkdir(parents=True)
    reg = tmp_path / "registry.yaml"
    reg.write_text("defaults:\n  track_errors: true\n"
                   "projects:\n"
                   "  alpha:\n    archetype: research\n"
                   "    notes: a long note that wraps\n      path: not-a-key\n"
                   "  hl-aster-arb:\n    archetype: research\n"
                   "aliases:\n  Alpha-Old: alpha\n")
    e = {k: v for k, v in os.environ.items()
         if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE")}
    envfile = tmp_path / "claude-env"
    e.update(HOME=str(home), CLANKER_DATA=str(data), CLANKER_REGISTRY=str(reg),
             CLAUDE_ENV_FILE=str(envfile))
    return SimpleNamespace(home=home, data=data, reg=reg, env=e, envfile=envfile,
                           projects=home / "projects")


def _run(env, cwd, source="startup", sid=None):
    payload = json.dumps({"session_id": sid or f"ss-{uuid.uuid4().hex[:10]}",
                          "cwd": str(cwd), "source": source,
                          "hook_event_name": "SessionStart"})
    t0 = time.monotonic()
    r = subprocess.run(["bash", HOOK], input=payload, capture_output=True, text=True,
                       env=env.env, timeout=60)
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)                  # exactly ONE JSON document on stdout
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert len(ctx.encode("utf-8")) <= BUDGET, len(ctx.encode("utf-8"))
    return ctx, elapsed


def _big_now_status(n_lines=20, line_bytes=1100, heading="## NOW (2026-09-04)"):
    body = [f"- **entry {i}** " + "x" * (line_bytes - 16) for i in range(n_lines)]
    return "\n".join(["# STATUS — alpha", "", heading, *body, "", "## NEXT",
                      "1. the NEXT section must not leak into NOW", ""]) + "\n"


@pytest.mark.skipif(not os.path.isfile(REAL_STATUS),
                    reason="the real STATUS.md fixture source is not on this machine")
def test_real_oversized_status_is_capped(env):
    proj = env.projects / "hl-aster-arb"
    proj.mkdir()
    shutil.copy(REAL_STATUS, proj / "STATUS.md")
    _repo(proj, subjects=("add status",), add_all=True)
    ctx, _ = _run(env, proj)
    lines = ctx.splitlines()
    assert lines[0].startswith("clanker: hl-aster-arb · research · main@")
    assert lines[0].endswith(" · 0 dirty")
    assert lines[1].startswith("NOW: ")
    size = os.path.getsize(REAL_STATUS)
    assert (f"state: STATUS.md ({size} B) · index: none · "
            "alerts: 0 open for this project (`clanker alert list`)") in lines
    assert lines[-1].startswith("git: ") and "add status" in lines[-1]


def test_synthetic_oversized_now_is_capped_and_bounded(env):
    proj = env.projects / "alpha"
    proj.mkdir()
    (proj / "STATUS.md").write_text(_big_now_status())
    _repo(proj, add_all=True)
    ctx, _ = _run(env, proj)
    now = ctx.split("NOW: ", 1)[1].split("\nstate: ", 1)[0]
    assert now.startswith("(2026-09-04)\n- **entry 0** ")
    assert now.endswith(" …")                   # the 1.1 KB first entry is clipped
    assert len(now.encode()) <= 800
    assert "entry 1" not in now and "NEXT section" not in ctx


def test_no_status_but_index(env, tmp_path):
    proj = env.projects / "alpha"
    proj.mkdir()
    (proj / "STATE.md").write_text("# state\n")
    (proj / "INDEX.md").write_text("# index\n")      # INDEX.md outranks STATE.md
    ctx, _ = _run(env, proj)
    assert "NOW:" not in ctx
    lines = ctx.splitlines()
    assert lines[0] == "clanker: alpha · research"   # registered, not a git repo
    assert lines[1] == ("state: no STATUS.md · index: INDEX.md · "
                        "alerts: 0 open for this project (`clanker alert list`)")
    assert len(lines) == 2                            # no git line outside a repo


def test_unregistered_cwd_falls_back_and_exports_quoted(env, tmp_path):
    odd = tmp_path / "scratch dir"
    odd.mkdir()
    ctx, _ = _run(env, odd)
    assert ctx.splitlines()[0] == "clanker: unregistered · scratch dir"
    assert "state: no STATUS.md · index: none · alerts: 0 open" in ctx
    exports = env.envfile.read_text()
    assert "export CLANKER_PROJECT='scratch dir'\n" in exports
    assert "export CLANKER_ARCHETYPE=unknown\n" in exports
    assert "Clanker: project=" not in ctx               # the old fallback line is gone


def test_subdir_of_registered_project_resolves_to_its_root(env):
    proj = _repo(env.projects / "alpha", subjects=("one", "two", "three", "four"))
    (proj / "STATUS.md").write_text("# STATUS — alpha\n\n## NOW\n- shipping the brief\n")
    sub = proj / "lib" / "deep"
    sub.mkdir(parents=True)
    ctx, _ = _run(env, sub)
    lines = ctx.splitlines()
    assert lines[0].startswith("clanker: alpha · research · main@")
    assert lines[0].endswith(" · 1 dirty")             # untracked STATUS.md
    assert lines[1] == "NOW: - shipping the brief"
    assert lines[-3].startswith("git: ") and lines[-3].endswith(" four")
    assert [ln.split(" ", 1)[1] for ln in lines[-2:]] == ["three", "two"]
    assert "export CLANKER_PROJECT=alpha\n" in env.envfile.read_text()


def test_alert_count_is_one_pass_over_a_thousand_foreign_alerts(env):
    adir = env.data / "alerts"
    for i in range(1000):
        (adir / f"manual-{i:05d}.json").write_text(json.dumps(
            {"id": f"manual-{i:05d}", "severity": "info", "status": "active",
             "message": f"otherproj: overnight row {i}"}, indent=2))
    mine = [{"message": "alpha: suite red"},
            {"message": "alpha: MS-3 stopped", "status": "active"},
            {"message": "alpha ci/full RED at abc123", "project": "alpha"}]
    decoys = [{"message": "alpha-two: a different project"},
              {"message": "otherproj: blocked on alpha: docs"},
              {"message": "alpha: already handled", "status": "resolved"}]
    for i, a in enumerate(mine + decoys):
        (adir / f"x-{i}.json").write_text(json.dumps({"severity": "warning", **a}))
    (adir / "broken.json").write_text("{not json alpha:")
    proj = env.projects / "alpha"
    proj.mkdir()
    ctx, elapsed = _run(env, proj)
    assert "alerts: 3 open for this project (`clanker alert list`)" in ctx
    # the per-file jq loop took ~9.8 s over 1,112 files; one pass is milliseconds
    assert elapsed < 5.0, elapsed


def test_budget_cuts_now_first_then_git_to_one_line(env):
    long_subjects = [f"commit {c} " + "y" * 440 for c in "abc"]
    proj = _repo(env.projects / "alpha", subjects=long_subjects)
    (proj / "STATUS.md").write_text(
        "# STATUS — alpha\n\n## NOW (2026-09-20)\n"
        + "\n".join(f"- line {i} " + "z" * 290 for i in range(6)) + "\n")
    ctx, _ = _run(env, proj)
    git_block = ctx.split("\ngit: ", 1)[1]
    assert git_block.split(" ", 1)[1].startswith("commit c ")            # newest commit
    assert "\n" not in git_block                                         # git: one line
    now = ctx.split("NOW: ", 1)[1].split("\nstate: ", 1)[0]
    assert now.startswith("(2026-09-20)\n- line 0 ")     # NOW took the freed room back


def test_git_keeps_three_lines_when_now_can_shrink(env):
    proj = _repo(env.projects / "alpha", subjects=("s1", "s2", "s3"))
    (proj / "STATUS.md").write_text(_big_now_status(n_lines=8, line_bytes=150))
    ctx, _ = _run(env, proj)
    assert ctx.split("\ngit: ", 1)[1].count("\n") == 2   # all three commits
    assert "NOW: (2026-09-04)\n- **entry 0**" in ctx


def test_stacked_now_sections_newest_dated_wins(env):
    proj = env.projects / "alpha"
    proj.mkdir()
    (proj / "STATUS.md").write_text(
        "# STATUS — alpha\n\n"
        "## NOW (2026-09-07 03:55Z) — lane one\n- older\n\n"
        "## NOW (2026-09-07 04:45Z) — lane two\n- newest\n```\n## NOW (2026-09-30) fenced\n```\n\n"
        "## NOW (2026-09-06 14:00Z) — lane three\n- oldest\n")
    ctx, _ = _run(env, proj)
    assert "NOW: (2026-09-07 04:45Z) — lane two\n- newest\n```" in ctx
    assert "lane one" not in ctx and "lane three" not in ctx


def test_status_without_now_heading_uses_first_600_bytes(env):
    proj = env.projects / "alpha"
    proj.mkdir()
    entries = [f"**2026-09-{d:02d} — entry {d}** " + "w" * 180 for d in range(20, 0, -1)]
    (proj / "STATUS.md").write_text("# alpha — STATUS\n\n" + "\n\n".join(entries) + "\n")
    ctx, _ = _run(env, proj)
    now = ctx.split("NOW: ", 1)[1].split("\nstate: ", 1)[0]
    assert now.startswith("# alpha — STATUS\n**2026-09-20 — entry 20**")
    assert len(now.encode()) <= 600 and "entry 18" not in now


def test_clear_hands_previous_transcript_to_session_end(env, tmp_path):
    work = tmp_path / "work.dir"            # a dot: Claude Code's slug turns it into '-'
    work.mkdir()
    slug = "".join(c if c.isalnum() else "-" for c in str(work))
    tdir = env.home / ".claude" / "projects" / slug
    tdir.mkdir(parents=True)
    old_id, new_id = f"old-{uuid.uuid4().hex[:10]}", f"new-{uuid.uuid4().hex[:10]}"
    line = json.dumps({"type": "assistant", "timestamp": "2026-09-24T00:00:00Z",
                       "message": {"content": [{"type": "text", "text": "done"}]}})
    (tdir / f"{old_id}.jsonl").write_text(line + "\n")
    (tdir / f"{new_id}.jsonl").write_text(line + "\n")
    past = time.time() - 60
    os.utime(tdir / f"{old_id}.jsonl", (past, past))
    _run(env, work, source="clear", sid=new_id)
    sessions = env.data / "raw" / "sessions"
    deadline, rows = time.time() + 20, []
    while time.time() < deadline:
        rows = [json.loads(l) for f in sessions.glob("*.jsonl")
                for l in f.read_text().splitlines() if l.strip()]
        if any(r.get("session_id") == old_id for r in rows):
            break
        time.sleep(0.2)
    finals = [r for r in rows if r.get("session_id") == old_id]
    assert finals and finals[-1]["outcome"] != "open"       # session-end's full row
    stubs = [r for r in rows if r.get("session_id") == new_id]
    assert stubs and stubs[0]["outcome"] == "open" and stubs[0]["source"] == "clear"
