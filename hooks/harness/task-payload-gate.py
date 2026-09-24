#!/usr/bin/env python3
"""PreToolUse gate on TaskCreate/TaskUpdate (GLOBAL) — caps what the task
registry costs in CONTEXT, not on disk.

WHY (measured 2026-08-08, fableNQkronos):
the harness rebuilds its periodic reminder attachment from the ENTIRE list —
`task_reminder` from the task registry, `todo_reminder` from the TodoWrite
list — with no size cap, no filtering, and completed entries still included.
Every byte therefore costs once PER REMINDER, not once. Session a44e7293 fired
129 reminders against a registry that had grown to 53.4 KB: 5.33 MB of
transcript, 29.6% of the whole session, ~1.33M tokens spent restating a list.

It also defeats compaction: a 17.4 KB compact summary was followed 59 lines
later by a 35.0 KB reminder — the task list outweighed the compacted summary of
everything else 2.4:1, so the context freed by compacting was immediately
refilled by the largest single object in the session.

Root cause of the blowout: 81% of live tasks were already `completed` and still
shipping, and descriptions were 90% of the payload (56,800 B of descriptions vs
6,216 B of subjects; p50 401 B, max 2,710 B).

WHAT IT BLOCKS (exit 2; the message goes to the model):
  1. MALFORMED — subject, description or activeForm is present but is not a
                 string (null counts as absent). The gate cannot measure it,
                 so it fails CLOSED.
  2. LENGTH    — subject > 80 chars, description > 400 chars.
  3. BACKSTOP  — a tool_input that serialises to > 4096 B, whatever the field.
  4. BUDGET    — TaskCreate, or a TaskUpdate that sets subject, description,
                 activeForm or metadata, while the LIVE task list already holds
                 > 6144 B of task JSON.
  5. ID CAP    — TaskCreate after 25 successful TaskCreate calls in this
                 session. Deleting tasks never lowers the count. A new session
                 id (for example after a resume) starts again at 0.
A status-only TaskUpdate (status, owner, blocks) is never blocked by 4 or 5,
so an over-budget list can always be drained with status="deleted". This gate
NEVER deletes anything (standing rule: never delete without confirmation).
settings.json routes only TaskCreate|TaskUpdate here. The TodoWrite branch in
check() applies the same caps to a TodoWrite list, but no matcher reaches it.

WHERE THE LIVE LIST IS (read in the Claude Code 2.1.280 binary and checked
against ~/.claude/tasks on 2026-09-24):
  * One file per task, <config dir>/tasks/<list-id>/<task-id>.json, written
    when the task is created or updated, not at session end. Session 422ceb8a
    created task 18 at 2026-09-21T00:37:20.349Z, and
    tasks/session-82d531ea/18.json has mtime 00:37:20. The task_reminder
    attachment is read from this same list, so BUDGET measures what the
    reminder re-sends.
  * <list-id> is $CLAUDE_CODE_TASK_LIST_ID if set, else the session's team
    name (settings.json turns agent teams on). A new session's team is
    session-<first 8 hex of its id>. A session that resumes, or whose id
    changes inside a running process, KEEPS its team, so its list keeps the
    first session's name: session 9651e093's team, and so its list, is
    session-11c909b8. The old gate looked only at tasks/session-<own id>.
    After any resume or id change it found an empty or missing dir, so BUDGET
    never fired. The 2026-08-08 note that a live session
    "keeps task bodies in memory and only flushes them at session end" was this
    mismatch.
  * .highwatermark is written only when a task is deleted or the list is
    reset, never on create (session-82d531ea: 11 on disk, ids up to 18). It is
    not an id counter. The old ID CAP read it; this gate does not.

HOW THE LIST IS FOUND (first hit wins). Never "the newest dir by mtime": on
2026-08-08 that adopted a live session's store and false-blocked.
  1. $CLAUDE_CODE_TASK_LIST_ID. The hook inherits the session's environment.
  2. teamName in this session's teammate sidecars
     (<transcript_path minus .jsonl>/subagents/*.meta.json), if all agree.
  3. Evidence: the (id, subject) pairs that TaskCreate returned in this
     transcript. The one tasks/ dir whose <id>.json files hold the most of
     them wins. A tie resolves to nothing.
  4. tasks/session-<first 8 of session_id>, then tasks/<session_id>: a
     session in its first life.
  Nothing found: BUDGET is skipped (fail open). The usual case is the first
  TaskCreate after an id change, in a session that has no teammates.

HOW THE ID COUNT IS READ: TaskCreate results in transcript_path (tied to a
TaskCreate call by tool_use_id) whose record session_id (else sessionId)
equals the hook's session_id. A resumed transcript keeps the original
session_id on its copied history, so earlier sessions are not counted.
Limits: a subagent's hook payload carries the PARENT's transcript_path and
session_id, so the count is the main thread's. Teammates' own calls sit in
their sidecar transcripts and are not counted. Results from the same tool
batch may not be written yet. BUDGET still covers the whole shared list.

FAILURE MODES (operator ruling 2026-09-24):
  fail CLOSED (exit 2) on malformed model input: a capped field that is not a
    string (subject, description, activeForm; TodoWrite content/activeForm),
    a TodoWrite `todos` that is not a list, or a todo item that is not an
    object.
  fail OPEN (exit 0) on everything else that goes wrong: any exception in
    check() (the hook's own bugs); a stdin payload that is not a JSON object
    (the harness writes it, and it may be the status-only escape hatch); a
    tool_input that is not an object (the API only sends objects, so that is
    a harness fault); a list or transcript that cannot be found or read.

Selftest: python3 task-payload-gate.py --selftest   (run after ANY edit here).
Tests:    python3 -u -m pytest ~/.claude/hooks/tests -q
An internal failure fails open and lands in the hook-error log.
"""
# ---- hook-error log: the same block in every clanker python hook ----------------
# A failure that the hook swallows (the hook stays fail-open for the session)
# appends one JSON line {ts, hook, session_id, cwd, rc, stderr_tail} to
# $CLANKER_DATA/raw/health/hook-errors-<UTC day>.jsonl (default /data/clanker),
# where `clanker doctor --harness` counts it. hook_err never raises, never
# blocks and never writes to stdout. Usage: hook_err(rc, step, error text or
# exception); stderr_tail is "<step>: " plus the end of the text (of the
# traceback, for an exception), 300 characters at most. Set
# HOOK_ERR["session_id"] and HOOK_ERR["cwd"] once the payload is parsed; a
# script read from stdin has no __file__ and sets HOOK_ERR["hook"] as well.
import os as _he_os
import sys as _he_sys

HOOK_ERR = {"hook": _he_os.path.basename(globals().get("__file__") or "") or "?",
            "session_id": "", "cwd": ""}


def hook_err(rc, step, err=""):
    try:
        import json
        import time
        import traceback
        if isinstance(err, BaseException):
            err = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        step, err = str(step), str(err or "").strip()
        room = 298 - len(step)
        msg = (step + ": " + err[-room:]) if err and room > 0 else step
        try:
            cwd = HOOK_ERR.get("cwd") or _he_os.getcwd()
        except OSError:
            cwd = ""
        rc = int(rc) if str(rc).lstrip("-").isdigit() else 1
        now = time.gmtime()
        d = _he_os.path.join(_he_os.environ.get("CLANKER_DATA") or "/data/clanker",
                             "raw", "health")
        _he_os.makedirs(d, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", now),
               "hook": str(HOOK_ERR.get("hook") or "?"),
               "session_id": str(HOOK_ERR.get("session_id") or ""), "cwd": str(cwd),
               "rc": rc, "stderr_tail": msg[:300]}
        path = _he_os.path.join(d, "hook-errors-" + time.strftime("%Y-%m-%d", now) + ".jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass
# ---- end of hook-error log --------------------------------------------------------

import json
import os
import re
import sys

MAX_SUBJECT = 80          # chars; p50 on 2026-08-08 was 71, p90 96
MAX_DESCRIPTION = 400     # chars; p50 on 2026-08-08 was 401 — forces detail into plan docs
MAX_TODO_ITEM = 80        # TodoWrite content/activeForm — same discipline as subject
TASK_STORE_BUDGET = 6144  # bytes of task JSON in the LIVE list before content-adding calls block
MAX_TASK_IDS = 25         # successful TaskCreate calls per session (never freed)
MAX_TOOL_INPUT = 4096     # shape-agnostic backstop for any gated tool call
MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024  # larger transcripts are not scanned (fail open)
MAX_EVIDENCE_PAIRS = 50   # newest TaskCreate results used to identify the list

# Why a count cap as well as BUDGET: the length caps alone do NOT bound the
# list. A task at both limits serialises to ~676 B, so 49 tasks (the observed
# blowout) would still reach ~33 KB. BUDGET bounds the list whenever the gate
# can find it; the count bounds decomposition per session either way. Residual
# cost is unavoidable while the harness re-injects the whole list per reminder
# — set CLAUDE_CODE_TODO_REMINDER_MODE=off to remove it entirely, at the price
# of losing passive task awareness between explicit TaskList calls.

TASK_TOOLS = ("TaskCreate", "TaskUpdate")
GATED_TOOLS = TASK_TOOLS + ("TodoWrite",)
TASK_TEXT_FIELDS = ("subject", "description", "activeForm")
CONTENT_FIELDS = ("subject", "description", "activeForm", "metadata")
CLEARING = ("completed", "deleted", "failed", "killed")


# ── where things are ─────────────────────────────────────────────────────────
def _config_dir(env):
    """The harness's config dir: $CLAUDE_CONFIG_DIR, else ~/.claude."""
    return env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def _safe_name(name):
    """The harness's sanitiser for list and task ids: [^A-Za-z0-9_-] -> '-'."""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", name)


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _loads(line):
    try:
        return json.loads(line)
    except ValueError:
        return None


def _store_stats(d):
    """(payload_bytes, n_tasks, n_completed) for a task list dir.

    Mirrors the harness's own listing: *.json files that are not dotfiles
    (.highwatermark, .lock and a .meta.json are not tasks).
    """
    payload = n = done = 0
    try:
        names = os.listdir(d)
    except OSError:
        return 0, 0, 0
    for fn in names:
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        t = _load_json(os.path.join(d, fn))
        if not isinstance(t, dict) or "subject" not in t:
            continue
        payload += len(json.dumps(t))
        n += 1
        if t.get("status") in CLEARING:
            done += 1
    return payload, n, done


# ── transcript: TaskCreate results ───────────────────────────────────────────
class _Scan(object):
    """TaskCreate results found in one transcript."""
    __slots__ = ("pairs", "created_here")

    def __init__(self, pairs, created_here):
        self.pairs = pairs                  # [(task id, subject)], newest first
        self.created_here = created_here    # results recorded under this session id


def _blocks(rec):
    msg = rec.get("message") if isinstance(rec, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _scan_transcript(tp, session_id):
    """Read the TaskCreate results in a transcript, or None if it cannot be read."""
    if not isinstance(tp, str) or not tp:
        return None
    try:
        if os.path.getsize(tp) > MAX_TRANSCRIPT_BYTES:
            return None
        fh = open(tp, "rb")
    except OSError:
        return None
    create_ids = set()
    pairs = []
    here = 0
    with fh:
        for line in fh:
            if b'"name":"TaskCreate"' in line:
                for blk in _blocks(_loads(line)):
                    if (blk.get("type") == "tool_use" and blk.get("name") == "TaskCreate"
                            and isinstance(blk.get("id"), str)):
                        create_ids.add(blk["id"])
            if b'"toolUseResult"' not in line or b'"task"' not in line:
                continue
            rec = _loads(line)
            if not isinstance(rec, dict):
                continue
            tur = rec.get("toolUseResult")
            task = tur.get("task") if isinstance(tur, dict) else None
            if not isinstance(task, dict):
                continue
            tid, subject = task.get("id"), task.get("subject")
            if not isinstance(tid, str) or not isinstance(subject, str):
                continue
            if not any(b.get("type") == "tool_result" and b.get("tool_use_id") in create_ids
                       for b in _blocks(rec)):
                continue            # a TaskGet or other result with the same shape
            pairs.append((tid, subject))
            if session_id and (rec.get("session_id") or rec.get("sessionId")) == session_id:
                here += 1
    pairs.reverse()
    return _Scan(pairs, here)


# ── which list is live ──────────────────────────────────────────────────────
def _team_from_sidecars(tp):
    """The team this session's teammates were spawned into, if they all agree."""
    if not isinstance(tp, str) or not tp.endswith(".jsonl"):
        return None
    sidecar = os.path.join(tp[:-len(".jsonl")], "subagents")
    try:
        names = os.listdir(sidecar)
    except OSError:
        return None
    teams = set()
    for fn in names:
        if not fn.endswith(".meta.json"):
            continue
        meta = _load_json(os.path.join(sidecar, fn))
        team = meta.get("teamName") if isinstance(meta, dict) else None
        if isinstance(team, str) and team:
            teams.add(team)
    return teams.pop() if len(teams) == 1 else None


def _dir_from_evidence(root, pairs):
    """The one list dir that holds the most of these (id, subject) pairs, else None."""
    want = {}
    for tid, subject in pairs[:MAX_EVIDENCE_PAIRS]:
        want.setdefault(_safe_name(tid) + ".json", set()).add(subject)
    if not want:
        return None
    try:
        lists = os.listdir(root)
    except OSError:
        return None
    scores = {}
    for name in lists:
        d = os.path.join(root, name)
        try:
            files = set(os.listdir(d))
        except OSError:
            continue
        hits = 0
        for fn, subjects in want.items():
            if fn in files:
                t = _load_json(os.path.join(d, fn))
                if isinstance(t, dict) and t.get("subject") in subjects:
                    hits += 1
        if hits:
            scores[d] = hits
    if not scores:
        return None
    best = max(scores.values())
    winners = [d for d, s in scores.items() if s == best]
    return winners[0] if len(winners) == 1 else None


def _resolve_list(data, env, scan):
    """(dir, how) for the live task list, or (None, "unresolved").

    `scan` is a zero-argument callable returning the transcript _Scan (or None),
    so the transcript is read only when a step needs it.
    """
    root = os.path.join(_config_dir(env), "tasks")
    list_id = env.get("CLAUDE_CODE_TASK_LIST_ID")
    if list_id:
        return os.path.join(root, _safe_name(list_id)), "CLAUDE_CODE_TASK_LIST_ID"
    team = _team_from_sidecars(data.get("transcript_path"))
    if team:
        return os.path.join(root, _safe_name(team)), "team %s, from teammate records" % team
    s = scan()
    if s is not None and s.pairs:
        d = _dir_from_evidence(root, s.pairs)
        if d:
            return d, "matched to TaskCreate results in the transcript"
    sid = data.get("session_id")
    if isinstance(sid, str) and len(sid) >= 8:
        for name in ("session-" + sid[:8], sid):
            d = os.path.join(root, _safe_name(name))
            if os.path.isdir(d):
                return d, "session id"
    return None, "unresolved"


# ── checks ───────────────────────────────────────────────────────────────────
def _too_long(val, cap):
    return isinstance(val, str) and len(val) > cap


def _json_type(val):
    if isinstance(val, bool):
        return "a boolean"
    if isinstance(val, (int, float)):
        return "a number"
    if isinstance(val, dict):
        return "an object"
    if isinstance(val, list):
        return "an array"
    return type(val).__name__


def _malformed(field, val, want="a string"):
    return (
        "task-payload-gate: {f} is {t}, not {w}. The gate measures this field and "
        "blocks input it cannot measure (fail-closed on malformed input). Resend it "
        "as {w}.".format(f=field, t=_json_type(val), w=want)
    )


def _short(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def check(data, env=None):
    """Return (exit_code, message). Pure apart from reads — tests drive this directly."""
    env = os.environ if env is None else env
    tool = data.get("tool_name", "")
    if tool not in GATED_TOOLS:
        return 0, ""
    ti = data.get("tool_input")
    if not isinstance(ti, dict):
        return 0, ""          # the API only sends objects: a harness fault, fail open
    if tool == "TodoWrite":
        return _check_todos(ti)

    # ---- malformed model input: fail CLOSED ---------------------------------
    for field in TASK_TEXT_FIELDS:
        val = ti.get(field)
        if val is not None and not isinstance(val, str):
            return 2, _malformed(field, val)

    # ---- named caps ----------------------------------------------------------
    if _too_long(ti.get("subject"), MAX_SUBJECT):
        return 2, (
            "task-payload-gate: subject is {n} chars (max {m}). The task panel renders "
            "on a vertical monitor and every byte re-injects on each task_reminder. "
            "Shorten to verb + object; put the rest in the plan doc.".format(
                n=len(ti["subject"]), m=MAX_SUBJECT)
        )
    if _too_long(ti.get("description"), MAX_DESCRIPTION):
        return 2, (
            "task-payload-gate: description is {n} chars (max {m}). A task states WHAT "
            "to do — findings, traces and rationale belong in the plan/design doc, not "
            "the task registry. Every byte here is re-sent on EVERY task_reminder "
            "(measured: 129 reminders x 53 KB = ~1.33M tokens in one session). "
            "Trim to the action, or link the doc.".format(
                n=len(ti["description"]), m=MAX_DESCRIPTION)
        )
    code, msg = _backstop(tool, ti)
    if code:
        return code, msg

    # ---- back-pressure: only calls that ADD content ---------------------------
    # A status-only update (completing/deleting) is the escape hatch and is
    # never blocked — otherwise an over-budget list could never be drained.
    if tool != "TaskCreate" and not any(k in ti for k in CONTENT_FIELDS):
        return 0, ""
    return _back_pressure(tool, data, env)


def _back_pressure(tool, data, env):
    memo = []

    def scan():
        if not memo:
            memo.append(_scan_transcript(data.get("transcript_path"), data.get("session_id")))
        return memo[0]

    d, how = _resolve_list(data, env, scan)
    if d:
        payload, n, done = _store_stats(d)
        if payload > TASK_STORE_BUDGET:
            return 2, (
                "task-payload-gate: the task list {d} ({how}) holds {p} B across {n} "
                "tasks ({c} completed), over the {b} B budget. The harness re-sends this "
                "whole list on every task_reminder. Clear finished work first: TaskUpdate "
                "with status=\"deleted\" on the completed tasks, then retry. (Status-only "
                "updates are never blocked; this gate never deletes anything itself.)".format(
                    d=_short(d), how=how, p=payload, n=n, c=done, b=TASK_STORE_BUDGET)
            )
    if tool == "TaskCreate":
        s = scan()
        if s is not None and s.created_here >= MAX_TASK_IDS:
            return 2, (
                "task-payload-gate: {h} tasks were already created in this session (max "
                "{m}). The whole list re-injects on every task_reminder, and 49 tasks cost "
                "~1.33M tokens in one measured session. This many tasks means "
                "over-decomposition: fold the remaining work into fewer, coarser tasks and "
                "track the steps in the plan doc. Deleting tasks does not lower this "
                "count.".format(h=s.created_here, m=MAX_TASK_IDS)
            )
    return 0, ""


def _check_todos(ti):
    """TodoWrite feeds a `todo_reminder` with the same cost model. Not routed here today."""
    todos = ti.get("todos")
    if todos is None:
        return _backstop("TodoWrite", ti)
    if not isinstance(todos, list):
        return 2, _malformed("todos", todos, "an array")
    for i, td in enumerate(todos):
        if not isinstance(td, dict):
            return 2, _malformed("todos[%d]" % i, td, "an object")
        for field in ("content", "activeForm"):
            val = td.get(field)
            if val is not None and not isinstance(val, str):
                return 2, _malformed("todos[%d].%s" % (i, field), val)
            if _too_long(val, MAX_TODO_ITEM):
                return 2, (
                    "task-payload-gate: todos[{i}].{f} is {n} chars (max {m}). "
                    "TodoWrite feeds a `todo_reminder` that re-sends the WHOLE "
                    "list every time — same cost model as the task registry. "
                    "Verb + object; detail goes in the plan doc.".format(
                        i=i, f=field, n=len(val), m=MAX_TODO_ITEM)
                )
    size = len(json.dumps(todos))
    if size > TASK_STORE_BUDGET:
        return 2, (
            "task-payload-gate: todo list is {s} B across {n} items — over the "
            "{b} B budget. This whole list re-injects on every `todo_reminder`. "
            "Drop finished items and split the rest into a plan doc.".format(
                s=size, n=len(todos), b=TASK_STORE_BUDGET)
        )
    return _backstop("TodoWrite", ti)


def _backstop(tool, ti):
    """Shape-agnostic cap so an unrecognised field can't route around the above."""
    size = len(json.dumps(ti, default=str))
    if size > MAX_TOOL_INPUT:
        return 2, (
            "task-payload-gate: {t} payload is {s} B (max {m}). Whatever is in there "
            "re-injects on every reminder — move the bulk into a plan/design doc and "
            "reference it.".format(t=tool, s=size, m=MAX_TOOL_INPUT)
        )
    return 0, ""


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception as e:
        # FAIL OPEN: the harness wrote this payload. Unreadable, it could just as
        # well be the status-only escape hatch as a create.
        hook_err(1, "parse hook input", e)
        return 0
    if not isinstance(data, dict):
        hook_err(1, "parse hook input", "payload is not a JSON object")
        return 0
    HOOK_ERR.update(session_id=str(data.get("session_id") or ""), cwd=str(data.get("cwd") or ""))
    try:
        code, msg = check(data)
    except Exception as exc:  # FAIL OPEN on the hook's own bugs
        sys.stderr.write("task-payload-gate: internal error, failing open: %s: %s\n"
                         % (type(exc).__name__, exc))
        hook_err(1, "check", exc)
        return 0
    if code and msg:
        sys.stderr.write(msg + "\n")
    return code


# ── selftest ──────────────────────────────────────────────────────────────────
def _selftest():
    import shutil
    import tempfile

    fails = []
    n_cases = [0]
    tmp = tempfile.mkdtemp()
    env = {"CLAUDE_CONFIG_DIR": os.path.join(tmp, ".claude")}

    def ck(label, data, want):
        n_cases[0] += 1
        got, _ = check(data, env)
        if got != want:
            fails.append("%s: got exit %s, want %s" % (label, got, want))

    try:
        # --- pass-through -----------------------------------------------------
        ck("non-gated tool passes", {"tool_name": "Bash", "tool_input": {"command": "x" * 9000}}, 0)
        ck("Agent tool passes", {"tool_name": "Agent", "tool_input": {"prompt": "x" * 9000}}, 0)
        ck("missing tool_input passes", {"tool_name": "TaskCreate"}, 0)
        ck("non-object tool_input fails open (harness fault)",
           {"tool_name": "TaskCreate", "tool_input": []}, 0)
        ck("null description passes (treated as absent)",
           {"tool_name": "TaskCreate", "tool_input": {"subject": "ok", "description": None}}, 0)

        # --- malformed model input: fail closed ---------------------------------
        ck("numeric subject blocks",
           {"tool_name": "TaskCreate", "tool_input": {"subject": 7, "description": "ok"}}, 2)
        ck("object description blocks",
           {"tool_name": "TaskCreate", "tool_input": {"subject": "ok", "description": {"t": "x"}}}, 2)
        ck("array activeForm blocks",
           {"tool_name": "TaskUpdate", "tool_input": {"taskId": "1", "activeForm": ["x"]}}, 2)
        ck("boolean subject on TaskUpdate blocks",
           {"tool_name": "TaskUpdate", "tool_input": {"taskId": "1", "subject": True}}, 2)

        # --- length caps --------------------------------------------------------
        ck("short create passes",
           {"tool_name": "TaskCreate", "tool_input": {"subject": "Fix gate", "description": "Do it."}}, 0)
        ck("description at limit passes",
           {"tool_name": "TaskCreate", "tool_input": {"subject": "ok", "description": "x" * 400}}, 0)
        ck("description over limit blocks",
           {"tool_name": "TaskCreate", "tool_input": {"subject": "ok", "description": "x" * 401}}, 2)
        ck("subject at limit passes",
           {"tool_name": "TaskCreate", "tool_input": {"subject": "s" * 80, "description": "ok"}}, 0)
        ck("subject over limit blocks",
           {"tool_name": "TaskCreate", "tool_input": {"subject": "s" * 81, "description": "ok"}}, 2)
        ck("TaskUpdate long description blocks",
           {"tool_name": "TaskUpdate", "tool_input": {"taskId": "1", "description": "x" * 401}}, 2)
        ck("TaskUpdate status-only passes",
           {"tool_name": "TaskUpdate", "tool_input": {"taskId": "1", "status": "completed"}}, 0)

        # --- backstop -----------------------------------------------------------
        ck("unknown fat field hits backstop",
           {"tool_name": "TaskCreate", "tool_input": {"subject": "ok", "notes": "z" * 5000}}, 2)
        ck("unknown small field passes",
           {"tool_name": "TaskCreate", "tool_input": {"subject": "ok", "notes": "z" * 10}}, 0)

        # --- TodoWrite (kept for a future matcher) ------------------------------
        ck("TodoWrite small list passes",
           {"tool_name": "TodoWrite", "tool_input": {"todos": [
               {"content": "Fix gate", "activeForm": "Fixing gate", "status": "pending"}]}}, 0)
        ck("TodoWrite long content blocks",
           {"tool_name": "TodoWrite", "tool_input": {"todos": [
               {"content": "c" * 81, "activeForm": "a", "status": "pending"}]}}, 2)
        ck("TodoWrite long activeForm blocks",
           {"tool_name": "TodoWrite", "tool_input": {"todos": [
               {"content": "c", "activeForm": "a" * 81, "status": "pending"}]}}, 2)
        ck("TodoWrite oversized list blocks",
           {"tool_name": "TodoWrite", "tool_input": {"todos": [
               {"content": "c" * 70, "activeForm": "a" * 70, "status": "pending"}
               for _ in range(60)]}}, 2)
        ck("TodoWrite non-object item blocks (malformed)",
           {"tool_name": "TodoWrite", "tool_input": {"todos": ["not a dict"]}}, 2)
        ck("TodoWrite non-array todos blocks (malformed)",
           {"tool_name": "TodoWrite", "tool_input": {"todos": "one, two"}}, 2)
        ck("TodoWrite missing todos passes",
           {"tool_name": "TodoWrite", "tool_input": {}}, 0)

        # --- back-pressure against a real temp list ------------------------------
        sid = "abcdef12-0000-0000-0000-000000000000"
        sd = os.path.join(env["CLAUDE_CONFIG_DIR"], "tasks", "session-" + sid[:8])
        os.makedirs(sd)
        for i in range(40):
            with open(os.path.join(sd, "%d.json" % i), "w") as fh:
                json.dump({"id": str(i), "subject": "s" * 60,
                           "description": "d" * 380, "status": "completed"}, fh, indent=2)
        payload, n, done = _store_stats(sd)
        if payload <= TASK_STORE_BUDGET:
            fails.append("fixture list %d B did not exceed budget %d B" % (payload, TASK_STORE_BUDGET))
        if n != 40 or done != 40:
            fails.append("list stats wrong: n=%s done=%s" % (n, done))
        ck("over-budget blocks TaskCreate",
           {"tool_name": "TaskCreate", "session_id": sid,
            "tool_input": {"subject": "ok", "description": "ok"}}, 2)
        ck("over-budget blocks content-adding TaskUpdate",
           {"tool_name": "TaskUpdate", "session_id": sid,
            "tool_input": {"taskId": "1", "description": "new text"}}, 2)
        ck("over-budget still allows status-only completion",
           {"tool_name": "TaskUpdate", "session_id": sid,
            "tool_input": {"taskId": "1", "status": "completed"}}, 0)
        ck("over-budget still allows deletion (escape hatch)",
           {"tool_name": "TaskUpdate", "session_id": sid,
            "tool_input": {"taskId": "1", "status": "deleted"}}, 0)
        ck("unresolvable sid fails OPEN (never guesses another session's list)",
           {"tool_name": "TaskCreate", "session_id": "nope",
            "tool_input": {"subject": "ok", "description": "ok"}}, 0)
        ck("short/absent sid fails OPEN",
           {"tool_name": "TaskCreate", "session_id": "",
            "tool_input": {"subject": "ok", "description": "ok"}}, 0)
        with open(os.path.join(sd, "bad.json"), "w") as fh:
            fh.write("{not json")
        for dot in (".highwatermark", ".lock", ".meta.json"):
            with open(os.path.join(sd, dot), "w") as fh:
                fh.write('{"subject": "not a task"}' if dot == ".meta.json" else "999")
        p2, _, _ = _store_stats(sd)
        if p2 != payload:
            fails.append("corrupt file or dotfile changed payload: %s vs %s" % (p2, payload))

        for i in range(40):
            os.remove(os.path.join(sd, "%d.json" % i))
        with open(os.path.join(sd, "0.json"), "w") as fh:
            json.dump({"id": "0", "subject": "s", "description": "d", "status": "pending"}, fh)
        ck("under-budget passes TaskCreate",
           {"tool_name": "TaskCreate", "session_id": sid,
            "tool_input": {"subject": "ok", "description": "ok"}}, 0)
        ck(".highwatermark 999 does not block (it is not an id counter)",
           {"tool_name": "TaskCreate", "session_id": sid,
            "tool_input": {"subject": "ok", "description": "ok"}}, 0)

        # --- another session's list is NEVER adopted ----------------------------
        other = os.path.join(env["CLAUDE_CONFIG_DIR"], "tasks", "session-99999999")
        os.makedirs(other)
        for i in range(40):                        # way over budget
            with open(os.path.join(other, "%d.json" % i), "w") as fh:
                json.dump({"id": str(i), "subject": "s" * 60,
                           "description": "d" * 380, "status": "pending"}, fh)
        os.utime(other, None)                      # newest mtime in the tree
        ck("newest foreign list is not adopted on an unresolvable sid",
           {"tool_name": "TaskCreate", "session_id": "nope",
            "tool_input": {"subject": "ok", "description": "ok"}}, 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not fails:
        print("task-payload-gate selftest: %d/%d PASS" % (n_cases[0], n_cases[0]))
        return 0
    print("task-payload-gate selftest: %d FAILURE(S) of %d" % (len(fails), n_cases[0]))
    for f in fails:
        print("  -", f)
    return 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(_selftest())
    sys.exit(main())
