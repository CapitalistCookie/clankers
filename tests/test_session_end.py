"""End-to-end tests for hooks/session-end.sh (P6, audit M4): the SessionEnd
record must say WHY a session ended — end_reason (the hook input's own reason),
failure_reason (limit/API-error signature in the transcript tail, from the
catalog inlined from the retired subagent auto-resume detector) and
last_assistant_line. The handoff writer was removed on 2026-09-24.

Hermetic: HOME→tmp, CLANKER_DATA→conftest tmp, unique session ids (the
hook's /tmp dedup marker is per-session-id). The memory-autocommit block that
cd'd to $HOME/.claude is gone (2026-09-24: the memory dir is disabled)."""
import json
import os
import subprocess
import uuid

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "hooks", "session-end.sh")


def _sid(tag):
    """Unique per RUN: the hook's /tmp/clanker-session-<id> dedup marker has a
    60s TTL shared across pytest invocations — a fixed id makes back-to-back
    runs (fast suite then full suite) silently skip the record write."""
    return f"p6-{tag}-{uuid.uuid4().hex[:10]}"


def _transcript_lines(final_error=None):
    lines = [
        json.dumps({"type": "user", "timestamp": "2026-07-22T05:00:00Z",
                    "message": {"role": "user", "content": "fix the flaky auth test"}}),
        json.dumps({"type": "assistant", "timestamp": "2026-07-22T05:01:00Z",
                    "message": {"model": "claude-fable-5",
                                "usage": {"input_tokens": 100, "output_tokens": 50},
                                "content": [{"type": "text",
                                             "text": "Working on the auth test now."}]}}),
        json.dumps({"type": "assistant", "timestamp": "2026-07-22T05:10:00Z",
                    "message": {"content": [{"type": "text",
                                             "text": "Fixed the auth test and pushed."}]}}),
    ]
    if final_error:
        lines.append(json.dumps({"type": "system", "timestamp": "2026-07-22T05:11:00Z",
                                 "content": final_error}))
    return "\n".join(lines) + "\n"


def _run_hook(tmp_path, session_id, transcript_text, cwd, reason="prompt_input_exit"):
    tp = tmp_path / f"{session_id}.jsonl"
    tp.write_text(transcript_text)
    payload = json.dumps({"session_id": session_id, "transcript_path": str(tp),
                          "cwd": str(cwd), "reason": reason})
    env = {**os.environ, "HOME": str(tmp_path)}
    r = subprocess.run(["bash", HOOK], input=payload, capture_output=True,
                       text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    day_dir = os.path.join(os.environ["CLANKER_DATA"], "raw", "sessions")
    rows = []
    for fn in os.listdir(day_dir):
        if fn.endswith(".jsonl"):
            with open(os.path.join(day_dir, fn)) as f:
                rows += [json.loads(l) for l in f if l.strip()]
    mine = [x for x in rows if x.get("session_id") == session_id]
    assert mine, f"no record written for {session_id}"
    return mine[-1]   # last-write-wins, same as analyze.load_sessions dedup


def test_limit_kill_yields_failure_reason_and_last_line(tmp_path):
    rec = _run_hook(tmp_path, _sid("limit"),
                    _transcript_lines(final_error="API error — Rate limited, retry later"),
                    cwd=tmp_path)
    assert rec["failure_reason"] == "Rate limited"   # matched from LIMIT_SIGNS
    assert rec["end_reason"] == "prompt_input_exit"
    assert rec["last_assistant_line"] == "Fixed the auth test and pushed."
    assert rec["tokens"]["input"] == 100             # existing fields intact


def test_clean_session_has_null_failure_reason(tmp_path):
    rec = _run_hook(tmp_path, _sid("clean"), _transcript_lines(), cwd=tmp_path,
                    reason="clear")
    assert rec["failure_reason"] is None
    assert rec["end_reason"] == "clear"


# ── P7: heartbeat stub (SessionStart) + duration cap-at-write ────────────────

START_HOOK = os.path.join(os.path.dirname(HOOK), "session-start.sh")


def _read_rows(session_id):
    day_dir = os.path.join(os.environ["CLANKER_DATA"], "raw", "sessions")
    rows = []
    for fn in sorted(os.listdir(day_dir)):
        if fn.endswith(".jsonl"):
            with open(os.path.join(day_dir, fn)) as f:
                rows += [json.loads(l) for l in f if l.strip()]
    return [x for x in rows if x.get("session_id") == session_id]


def test_session_start_writes_open_stub(tmp_path):
    """A session must EXIST in telemetry the moment it starts (07-20/21: ~49
    live sessions, zero rows). The stub is minimal and marked outcome=open."""
    sid = _sid("stub")
    payload = json.dumps({"session_id": sid, "cwd": str(tmp_path), "source": "startup"})
    env = {**os.environ, "HOME": str(tmp_path)}
    r = subprocess.run(["bash", START_HOOK], input=payload, capture_output=True,
                       text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    rows = _read_rows(sid)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "open"
    assert rows[0]["source"] == "startup"
    assert rows[0]["cwd"] == str(tmp_path)
    assert "duration_s" not in rows[0]   # stubs carry no final numbers


def test_lifecycle_stub_then_final_row(tmp_path):
    """SessionStart stub + SessionEnd final row for the same id: the final row
    is appended AFTER the stub, so consumers' last-write-wins dedup
    (analyze.load_sessions) sees the completed record."""
    sid = _sid("lifecycle")
    payload = json.dumps({"session_id": sid, "cwd": str(tmp_path), "source": "startup"})
    env = {**os.environ, "HOME": str(tmp_path)}
    subprocess.run(["bash", START_HOOK], input=payload, capture_output=True,
                   text=True, env=env, timeout=60)
    _run_hook(tmp_path, sid, _transcript_lines(), cwd=tmp_path)  # asserts 1 final row
    rows = _read_rows(sid)
    assert len(rows) == 2
    assert rows[0]["outcome"] == "open" and rows[-1]["outcome"] != "open"


def test_duration_capped_at_write_wall_clock_raw(tmp_path):
    """19-day OOM rows: duration_s is work-time (capped 8h AT WRITE, audit M4 —
    'one future consumer will forget'), wall_clock_s keeps the raw span."""
    lines = [
        json.dumps({"type": "user", "timestamp": "2026-07-03T05:00:00Z",
                    "message": {"role": "user", "content": "long-lived session"}}),
        json.dumps({"type": "assistant", "timestamp": "2026-07-22T05:00:00Z",
                    "message": {"content": [{"type": "text", "text": "still here"}]}}),
    ]
    rec = _run_hook(tmp_path, _sid("cap"), "\n".join(lines) + "\n", cwd=tmp_path)
    assert rec["duration_s"] == 28800                    # capped
    assert rec["wall_clock_s"] == 19 * 24 * 3600         # raw span preserved
    # short session: both equal, uncapped
    rec2 = _run_hook(tmp_path, _sid("short"), _transcript_lines(), cwd=tmp_path)
    assert rec2["duration_s"] == rec2["wall_clock_s"] == 600


def test_no_handoff_and_no_lib_or_sibling_file(tmp_path):
    """The handoff writer imported lib/handoff.py from a lib directory that
    never existed on the installed side (dead since 07-19) and is removed. The
    hook reads no other file of the repo, so `clanker sync` ships it alone."""
    name = f"p9repo-{uuid.uuid4().hex[:8]}"
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
                   check=True)
    rec = _run_hook(tmp_path, _sid("nohandoff"), _transcript_lines(), cwd=repo)
    assert rec["project"] == name                    # an unregistered repo, named by git
    handoff = os.path.join(os.environ["CLANKER_DATA"], "wiki", "projects",
                           f"{name}-handoff.md")
    assert not os.path.exists(handoff)
    src = open(HOOK).read()
    for gone in ("../lib", "CLANKER_LIB", "HOOK_DIR", "handoff import", "generate_handoff",
                 "subagent-resume-detect", "importlib"):
        assert gone not in src, gone


# ── Telemetry truth + nested tag + registry attribution (2026-09-24) ─────────

def _asst(mid, model, usage, block, entrypoint="cli"):
    """One transcript line of an assistant message: Claude Code writes a line
    per content block, every line carrying the whole message's usage."""
    return json.dumps({"type": "assistant", "timestamp": "2026-09-24T00:00:00Z",
                       "entrypoint": entrypoint, "uuid": uuid.uuid4().hex,
                       "requestId": "req_" + mid,
                       "message": {"id": mid, "model": model, "role": "assistant",
                                   "usage": usage, "content": [block]}})


def _txt(s):
    return {"type": "text", "text": s}


def _usage(i, o, cr, cc, split=None):
    u = {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr,
         "cache_creation_input_tokens": cc}
    if split:
        u["cache_creation"] = {"ephemeral_5m_input_tokens": split.get("5m", 0),
                               "ephemeral_1h_input_tokens": split.get("1h", 0)}
    return u


def _hook_env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_ENTRYPOINT", "CLANKER_INJECT_NESTED", "CLAUDE_PROJECT_DIR",
                        "CLAUDE_ENV_FILE", "CLANKER_REGISTRY", "CLANKER_PROJECT_ROOTS")}
    env["HOME"] = str(tmp_path)
    env.update(extra)
    return env


def _end(tmp_path, sid, transcript_text, cwd, env, subagents=None):
    """Run session-end.sh on a transcript (plus files under its subagents dir)
    and return the row it wrote."""
    tp = tmp_path / f"{sid}.jsonl"
    tp.write_text(transcript_text)
    for rel, text in (subagents or {}).items():
        p = tmp_path / sid / "subagents" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    payload = json.dumps({"session_id": sid, "transcript_path": str(tp), "cwd": str(cwd),
                          "reason": "other"})
    r = subprocess.run(["bash", HOOK], input=payload, capture_output=True, text=True,
                       env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    rows = _read_rows(sid)
    assert rows, f"no row for {sid}: {r.stderr}"
    return rows[-1]


def test_usage_counts_once_per_message_and_subagents_apart(tmp_path):
    """Two content-block lines per message used to count every token twice; a
    message is now counted once, priced at its own model's rate, and subagent
    transcripts (subagents/**/agent-*.jsonl) land in their own fields."""
    A = _usage(10_000, 200_000, 1_000_000, 3_000_000, {"1h": 3_000_000})
    B = _usage(20_000, 300_000, 5_000_000, 400_000, {"5m": 400_000})
    C = _usage(5_000, 100_000, 2_000_000, 1_000_000)             # no split: 5-minute
    main = [
        _asst("msg_A", "claude-fable-5-1", A, {"type": "thinking", "thinking": ""}),
        _asst("msg_A", "claude-fable-5-1", A, _txt("reading the repo")),
        _asst("msg_B", "claude-fable-5-1", B, _txt("running the suite")),
        _asst("msg_B", "claude-fable-5-1", B, {"type": "tool_use", "id": "toolu_1",
                                               "name": "Bash", "input": {"command": "ls"}}),
        *[_asst("msg_C", "claude-opus-5-5", C, _txt(f"part {n}")) for n in range(5)],
        json.dumps({"type": "assistant", "timestamp": "2026-09-24T00:00:01Z",
                    "message": {"id": "msg_syn", "model": "<synthetic>",
                                "usage": _usage(0, 0, 0, 0), "content": [_txt("No response requested.")]}}),
    ]
    s1 = [_asst("msg_S1", "claude-opus-5-5", _usage(200, o, 0, 3_711_700, {"5m": 3_711_700}), _txt("x"))
          for o in (700, 700, 119_000)]                          # partial output until the last line
    s2 = [_asst("msg_S2", "claude-sonnet-5", _usage(10_000, 100_000, 1_000_000, 200_000,
                                                    {"1h": 200_000}), _txt("y"))] * 2
    decoy = [_asst("msg_J", "claude-fable-5-1", _usage(9, 9, 9, 9), _txt("journal"))]
    rec = _end(tmp_path, _sid("dedupe"), "\n".join(main) + "\n", tmp_path, _hook_env(tmp_path),
               subagents={"agent-a1.jsonl": "\n".join(s1) + "\n",
                          "agent-a1.meta.json": "{}",
                          "workflows/wf_1/agent-a2.jsonl": "\n".join(s2) + "\n",
                          "workflows/wf_1/journal.jsonl": "\n".join(decoy) + "\n"})
    assert rec["tokens"] == {"input": 35_000, "output": 600_000,
                             "cache_read": 8_000_000, "cache_create": 4_400_000}
    assert rec["api_calls"] == 3
    # A 70.35 (1h writes at $20) + B 21.45 (5m at $12.50) + C 7.42 (Opus 5.5 rates)
    assert rec["estimated_cost_usd"] == 99.22
    assert rec["model"] == "claude-fable-5-1"          # 2 calls beat 1 call on 5 lines
    assert rec["first_call_ctx"] == 4_010_000 and rec["peak_ctx"] == 5_420_000
    assert rec["subagent_tokens"] == {"input": 10_200, "output": 219_000,
                                      "cache_read": 1_000_000, "cache_create": 3_911_700}
    assert rec["subagent_api_calls"] == 2
    assert rec["subagent_cost_usd"] == 22.96           # S1 20.9393 + S2 2.02
    assert rec["tool_uses"] == {"Bash": 1} and rec["subagent_count"] == 0


def _exec_block(start_marker, end_marker, hook=HOOK):
    """The hook's own python between two markers, executed into a namespace."""
    text = open(hook).read()
    body = text[text.index(start_marker):text.index(end_marker)]
    ns = {}
    exec(compile("import itertools, os, re, sys\n" + body, hook, "exec"), ns)
    return ns


def test_price_table_is_the_documented_one():
    """$/MTok (input, output, cache read, 5-minute write, 1-hour write) from the
    claude-api skill bundled with Claude Code 2.1.280 (shared/models.md,
    model-migration.md, prompt-caching.md): writes 1.25x / 2x input; reads
    0.1x input except Fable 5.1 ($0.25) and Opus 5.5 ($0.20)."""
    price_for = _exec_block("# ---- pricing", "# ---- main transcript")["price_for"]
    assert price_for("claude-fable-5-1") == (10.0, 50.0, 0.25, 12.50, 20.0)
    assert price_for("claude-fable-5") == (10.0, 50.0, 1.00, 12.50, 20.0)
    assert price_for("claude-opus-5-5") == (4.0, 20.0, 0.20, 5.00, 8.0)
    assert price_for("claude-opus-5-5[1m]") == (4.0, 20.0, 0.20, 5.00, 8.0)
    assert price_for("claude-opus-5") == (5.0, 25.0, 0.50, 6.25, 10.0)
    assert price_for("claude-opus-4-8") == (5.0, 25.0, 0.50, 6.25, 10.0)
    assert price_for("claude-sonnet-5") == (2.0, 10.0, 0.20, 2.50, 4.0)
    assert price_for("claude-haiku-4-5-20251001") == (1.0, 5.0, 0.10, 1.25, 2.0)
    assert price_for("us.anthropic.claude-sonnet-4-6") == (3.0, 15.0, 0.30, 3.75, 6.0)
    assert price_for("claude-fable-6") == price_for("claude-fable-5-1")    # family
    assert price_for("something-else") == price_for("claude-sonnet-5")


def test_nested_tag_from_env_or_transcript(tmp_path):
    """Every scripted `claude -p` runs with CLAUDE_CODE_ENTRYPOINT=sdk-cli and
    writes that entrypoint on its transcript lines; either marks the row."""
    def rec(env_entry, line_entry):
        line = json.loads(_asst("msg_n", "claude-sonnet-5", _usage(1, 1, 0, 0), _txt("ok")))
        if line_entry is None:
            line.pop("entrypoint")
        else:
            line["entrypoint"] = line_entry
        extra = {"CLAUDE_CODE_ENTRYPOINT": env_entry} if env_entry else {}
        return _end(tmp_path, _sid("nested"), json.dumps(line) + "\n", tmp_path,
                    _hook_env(tmp_path, **extra))
    r = rec("sdk-cli", "cli")
    assert r["nested"] is True and r["entrypoint"] == "cli"
    r = rec("cli", "sdk-cli")
    assert r["nested"] is True and r["entrypoint"] == "sdk-cli"
    r = rec("cli", "cli")
    assert r["nested"] is False and r["entrypoint"] == "cli"
    r = rec(None, None)
    assert r["nested"] is False and r["entrypoint"] is None


def test_resolution_block_is_identical_in_both_hooks():
    start = open(START_HOOK).read()
    end = open(HOOK).read()

    def block(t):
        a = t.index("# ---- project resolution ---")
        b = t.index("# ---- end of project resolution ---")
        return t[a:b]
    assert block(start) == block(end)


def _git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def test_start_and_end_name_the_same_project_the_registry_way(tmp_path):
    """211 of 268 "global" rows were one registered repo living on a data
    volume behind a ~/projects symlink: the hook's lib import never resolved
    in the installed copy. Both hooks now resolve like the registry does."""
    home = tmp_path / "home"
    root = home / "projects"
    root.mkdir(parents=True)
    build = _git_init(tmp_path / "vol" / "build" / "bigrepo")          # the data-volume repo
    (root / "bigrepo").symlink_to(build)
    _git_init(root / "newrepo")                                        # discovered only
    outer = _git_init(home / "outer")                                  # a repo holding a registered subdir
    (outer / "sub" / "inner").mkdir(parents=True)
    oldname = _git_init(tmp_path / "elsewhere" / "Old-Name")           # aliased git repo
    (root / "plain" / "deep").mkdir(parents=True)                      # non-git dir under the root
    loose = tmp_path / "loose"
    loose.mkdir()
    reg = tmp_path / "registry.yaml"
    reg.write_text("projects:\n"
                   "  bigrepo:\n    archetype: tool\n"
                   f"  inner:\n    archetype: research\n    path: {outer / 'sub' / 'inner'}\n"
                   "aliases:\n  Old-Name: bigrepo\n"
                   "  mapping-by-mistake:\n    archetype: tool\n")
    env = _hook_env(tmp_path, HOME=str(home), CLANKER_REGISTRY=str(reg),
                    CLANKER_PROJECT_ROOTS=str(root))
    cases = {build / "src": "bigrepo", root / "bigrepo": "bigrepo", root / "newrepo": "newrepo",
             outer / "sub" / "inner": "inner", outer: "outer", oldname: "bigrepo",
             root / "plain" / "deep": "plain", loose: "global"}
    (build / "src").mkdir()
    for cwd, want in cases.items():
        sid = _sid("attr")
        subprocess.run(["bash", START_HOOK], env=env, capture_output=True, text=True, timeout=60,
                       input=json.dumps({"session_id": sid, "cwd": str(cwd), "source": "startup"}))
        line = _asst("msg_r", "claude-sonnet-5", _usage(1, 1, 0, 0), _txt("ok"))
        final = _end(tmp_path, sid, line + "\n", cwd, env)
        rows = _read_rows(sid)
        assert rows[0]["outcome"] == "open", rows
        assert (rows[0]["project"], final["project"]) == (want, want), (str(cwd), rows)


# The detector's LIMIT_SIGNS, which the installed copy lost when the detector
# was retired: its fallback held only the first group. The second group was
# silently dropped (a tail with only these signatures got failure_reason None).
FALLBACK_SIGNS = ('"error":"rate_limit"', "hit your session limit", "usage limit",
                  "Rate limited", "Overloaded", '"status":429', '"status":529')
RESTORED_SIGNS = ('"apiErrorStatus":429', "rate_limit_error", "overloaded_error",
                  "temporarily limiting", "not your usage limit", '"apiErrorStatus":529',
                  '"apiErrorStatus":503', '"status":503', "overloaded", "service_unavailable")


def test_failure_reason_uses_the_full_inlined_catalog(tmp_path):
    """Run the hook copied ALONE into a directory (the installed layout, with
    no detector beside it): every restored signature is still detected."""
    import shutil
    alone = tmp_path / "dist"
    alone.mkdir()
    hook = alone / "session-end.sh"
    shutil.copy(HOOK, hook)
    for sign in RESTORED_SIGNS:
        sid = _sid("sign")
        if sign.startswith('"'):        # a key of the compact-JSON line itself
            tail = '{"type":"system","timestamp":"2026-09-24T00:00:01Z",' + sign + "}"
        else:                           # rendered error text
            tail = json.dumps({"type": "system", "timestamp": "2026-09-24T00:00:01Z",
                               "content": f"API Error: {sign}"}, separators=(",", ":"))
        json.loads(tail)
        tp = tmp_path / f"{sid}.jsonl"
        tp.write_text(_transcript_lines() + tail + "\n")
        r = subprocess.run(["bash", str(hook)], capture_output=True, text=True, timeout=60,
                           env=_hook_env(tmp_path),
                           input=json.dumps({"session_id": sid, "transcript_path": str(tp),
                                             "cwd": str(tmp_path), "reason": "other"}))
        assert r.returncode == 0, r.stderr
        got = _read_rows(sid)[-1]["failure_reason"]
        # "not your usage limit" contains "usage limit", which comes first in
        # the catalog's order, so that is the signature reported for it
        assert got == ("usage limit" if sign == "not your usage limit" else sign), (sign, got)
        assert got not in FALLBACK_SIGNS or sign == "not your usage limit"
