#!/usr/bin/env python3
"""PreToolUse gate on TaskCreate/TaskUpdate/TodoWrite (GLOBAL) — caps what the
task registry costs in CONTEXT, not on disk.

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

WHAT THIS ENFORCES (operator ruling 2026-08-08):
  1. LENGTH      — a task states WHAT to do; findings/traces/rationale belong in
                   the plan doc. subject <= 80, description <= 400.
  2. BACK-PRESSURE — TaskCreate fail-closes once the session registry exceeds
                   TASK_STORE_BUDGET. So does a TaskUpdate that ADDS content.
                   A status-only update (completing/deleting) is NEVER blocked,
                   or the budget would deadlock with no way out.
  3. NO BYPASS   — TodoWrite feeds an identical `todo_reminder` injection, so it
                   gets the same list budget and per-item cap. A shape-agnostic
                   backstop caps total tool_input for every gated tool, so a
                   future field can't route around the named checks.

This gate NEVER deletes anything (standing operator rule: never delete without
explicit confirmation — archive instead). It makes eviction a conscious act.

Never crashes the session: any internal error exits 0 (fail-open on OUR bugs,
fail-closed only on the designed conditions).

Selftest: python3 task-payload-gate.py --selftest   (run after ANY edit here).
"""
import json
import os
import sys

MAX_SUBJECT = 80          # chars; p50 today 71, p90 96
MAX_DESCRIPTION = 400     # chars; p50 today 401 — forces detail into plan docs
MAX_TODO_ITEM = 80        # TodoWrite content/activeForm — same discipline as subject
TASK_STORE_BUDGET = 6144  # bytes of FLUSHED registry before creation back-pressures
MAX_TASK_IDS = 25         # ids issued this session (live signal; see note below)
MAX_TOOL_INPUT = 4096     # shape-agnostic backstop for any gated tool call

# NOTE on the two back-pressure signals (both are needed; neither alone suffices):
#
#   payload  — sums the task *.json files in the store. Accurate, but a LIVE
#              session keeps task bodies in memory and only flushes them at
#              session end, so this reads 0 for the session you are in. It is
#              the right signal for a RESUMED session that inherits a fat
#              registry (verified: session-b664edca reads 54,540 B on disk).
#   id count — `.highwatermark` in the store dir is the monotonic id counter and
#              IS live (this session read 5 after 5 TaskCreates). It cannot
#              decrease, so it measures how much you have DECOMPOSED, not how
#              much is currently open — which is exactly the operator rule it
#              enforces ("prefer fewer tasks; a 12-step task is over-decomposed").
#
# Why a count cap at all: the length caps alone do NOT bound the registry. A
# task at both limits serializes to ~676 B, so 49 tasks (the observed blowout)
# would still reach ~33 KB. At MAX_TASK_IDS the worst case is ~17 KB, and real
# descriptions run well under the cap. Residual cost is unavoidable while the
# harness re-injects the whole list per reminder — set
# CLAUDE_CODE_TODO_REMINDER_MODE=off to remove it entirely, at the price of
# losing passive task awareness between explicit TaskList calls.

TASK_TOOLS = ("TaskCreate", "TaskUpdate")
GATED_TOOLS = TASK_TOOLS + ("TodoWrite",)
CLEARING = ("completed", "deleted", "failed", "killed")


def _store_dir(session_id):
    """The task store for this session, or None — DIRECT MAPPING ONLY.

    ~/.claude/tasks/session-<first 8 of session uuid>. That mapping does not
    always hold (a session whose transcript is 6ff5d21a had store a8656f2d), and
    when it fails we return None and skip store-based back-pressure entirely.

    DO NOT add an "assume the newest store is ours" fallback. That was tried on
    2026-08-08 and was actively harmful: an unrelated maintenance script touched
    another store's mtime, the gate adopted a LIVE session's store, and reported
    its 63 ids as this session's — a false block. Guessing which store belongs to
    which session risks both false blocks and, worse, reasoning about a running
    session's state. Failing open here is correct: the length caps below are
    session-independent and remain the primary defence.
    """
    if not session_id or len(session_id) < 8:
        return None
    d = os.path.join(os.path.expanduser("~/.claude/tasks"), "session-" + session_id[:8])
    return d if os.path.isdir(d) else None


def _highwatermark(d):
    """Monotonic id counter for the store — LIVE, unlike the *.json bodies."""
    try:
        with open(os.path.join(d, ".highwatermark")) as fh:
            return int(fh.read().strip() or 0)
    except Exception:
        return 0


def _store_stats(d):
    """(payload_bytes, n_tasks, n_completed) for a task store dir."""
    payload = n = done = 0
    try:
        names = os.listdir(d)
    except OSError:
        return 0, 0, 0
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn)) as fh:
                t = json.load(fh)
        except Exception:
            continue
        if not isinstance(t, dict) or "subject" not in t:
            continue
        payload += len(json.dumps(t))
        n += 1
        if t.get("status") in CLEARING:
            done += 1
    return payload, n, done


def _too_long(val, cap):
    return isinstance(val, str) and len(val) > cap


def check(data):
    """Return (exit_code, message). Pure — the selftest drives this directly."""
    tool = data.get("tool_name", "")
    if tool not in GATED_TOOLS:
        return 0, ""
    ti = data.get("tool_input") or {}
    if not isinstance(ti, dict):
        return 0, ""

    # ---- TodoWrite: the parallel injection path, same budget ----------------
    if tool == "TodoWrite":
        todos = ti.get("todos")
        if isinstance(todos, list):
            for i, td in enumerate(todos):
                if not isinstance(td, dict):
                    continue
                for field in ("content", "activeForm"):
                    if _too_long(td.get(field), MAX_TODO_ITEM):
                        return 2, (
                            "task-payload-gate: todos[{i}].{f} is {n} chars (max {m}). "
                            "TodoWrite feeds a `todo_reminder` that re-sends the WHOLE "
                            "list every time — same cost model as the task registry. "
                            "Verb + object; detail goes in the plan doc.".format(
                                i=i, f=field, n=len(td[field]), m=MAX_TODO_ITEM)
                        )
            size = len(json.dumps(todos))
            if size > TASK_STORE_BUDGET:
                return 2, (
                    "task-payload-gate: todo list is {s} B across {n} items — over the "
                    "{b} B budget. This whole list re-injects on every `todo_reminder`. "
                    "Drop finished items and split the rest into a plan doc.".format(
                        s=size, n=len(todos), b=TASK_STORE_BUDGET)
                )
        return _backstop(tool, ti)

    # ---- TaskCreate / TaskUpdate: named caps -------------------------------
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

    # ---- back-pressure: anything that ADDS content to an over-budget store --
    # A status-only update (completing/deleting) is the escape hatch and is
    # never blocked — otherwise an over-budget store could never be drained.
    adds_content = tool == "TaskCreate" or any(
        k in ti for k in ("subject", "description", "activeForm", "metadata"))
    if adds_content:
        d = _store_dir(data.get("session_id", ""))
        if d:
            payload, n, done = _store_stats(d)
            if payload > TASK_STORE_BUDGET:
                return 2, (
                    "task-payload-gate: task registry is {p} B across {n} tasks ({c} "
                    "already completed) — over the {b} B budget. This whole payload "
                    "re-injects on every task_reminder. Clear finished work first: "
                    "TaskUpdate with status=\"deleted\" on the completed tasks, then "
                    "retry. (Status-only updates are never blocked; this gate never "
                    "deletes anything itself.)".format(p=payload, n=n, c=done,
                                                       b=TASK_STORE_BUDGET)
                )
            hw = _highwatermark(d)
            if tool == "TaskCreate" and hw >= MAX_TASK_IDS:
                return 2, (
                    "task-payload-gate: {h} task ids issued this session (max {m}). "
                    "The whole registry re-injects on every task_reminder, so a long "
                    "task list is a per-reminder tax — 49 tasks cost ~1.33M tokens in "
                    "one measured session. This many ids means over-decomposition: "
                    "fold the remaining work into fewer, coarser tasks and track the "
                    "steps in the plan doc.".format(h=hw, m=MAX_TASK_IDS)
                )
    return _backstop(tool, ti)


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
        data = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        code, msg = check(data)
    except Exception:
        return 0  # fail-open on our own bugs
    if code and msg:
        sys.stderr.write(msg + "\n")
    return code


# ── selftest ──────────────────────────────────────────────────────────────────
def _selftest():
    import shutil
    import tempfile

    fails = []
    n_cases = [0]

    def ck(label, data, want):
        n_cases[0] += 1
        got, _ = check(data)
        if got != want:
            fails.append("%s: got exit %s, want %s" % (label, got, want))

    # --- pass-through -------------------------------------------------------
    ck("non-gated tool passes", {"tool_name": "Bash", "tool_input": {"command": "x" * 9000}}, 0)
    ck("Agent tool passes", {"tool_name": "Agent", "tool_input": {"prompt": "x" * 9000}}, 0)
    ck("missing tool_input passes", {"tool_name": "TaskCreate"}, 0)
    ck("non-dict tool_input passes", {"tool_name": "TaskCreate", "tool_input": []}, 0)
    ck("non-string description passes",
       {"tool_name": "TaskCreate", "tool_input": {"subject": "ok", "description": None}}, 0)

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

    # --- TodoWrite (the bypass path) ----------------------------------------
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
    ck("TodoWrite malformed items tolerated",
       {"tool_name": "TodoWrite", "tool_input": {"todos": ["not a dict", None]}}, 0)
    ck("TodoWrite missing todos passes",
       {"tool_name": "TodoWrite", "tool_input": {}}, 0)

    # --- back-pressure against a real temp store ----------------------------
    tmp = tempfile.mkdtemp()
    orig_home = os.environ.get("HOME")
    try:
        sid = "abcdef12-0000-0000-0000-000000000000"
        sd = os.path.join(tmp, ".claude", "tasks", "session-" + sid[:8])
        os.makedirs(sd)
        os.environ["HOME"] = tmp
        for i in range(40):
            with open(os.path.join(sd, "%d.json" % i), "w") as fh:
                json.dump({"id": str(i), "subject": "s" * 60,
                           "description": "d" * 380, "status": "completed"}, fh)
        payload, n, done = _store_stats(sd)
        if payload <= TASK_STORE_BUDGET:
            fails.append("fixture store %d B did not exceed budget %d B"
                         % (payload, TASK_STORE_BUDGET))
        if n != 40 or done != 40:
            fails.append("store stats wrong: n=%s done=%s" % (n, done))
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
        ck("unresolvable sid fails OPEN (never guesses another session's store)",
           {"tool_name": "TaskCreate", "session_id": "nope",
            "tool_input": {"subject": "ok", "description": "ok"}}, 0)
        ck("short/absent sid fails OPEN",
           {"tool_name": "TaskCreate", "session_id": "",
            "tool_input": {"subject": "ok", "description": "ok"}}, 0)
        ck("corrupt task file does not crash stats",
           {"tool_name": "TaskCreate", "session_id": sid,
            "tool_input": {"subject": "ok", "description": "ok"}}, 2)
        with open(os.path.join(sd, "bad.json"), "w") as fh:
            fh.write("{not json")
        p2, _, _ = _store_stats(sd)
        if p2 != payload:
            fails.append("corrupt file changed payload: %s vs %s" % (p2, payload))
        os.remove(os.path.join(sd, "bad.json"))

        for i in range(40):
            os.remove(os.path.join(sd, "%d.json" % i))
        with open(os.path.join(sd, "0.json"), "w") as fh:
            json.dump({"id": "0", "subject": "s", "description": "d", "status": "pending"}, fh)
        ck("under-budget passes TaskCreate",
           {"tool_name": "TaskCreate", "session_id": sid,
            "tool_input": {"subject": "ok", "description": "ok"}}, 0)

        # --- highwatermark: the LIVE signal (task bodies are unflushed mid-session)
        hwf = os.path.join(sd, ".highwatermark")
        if _highwatermark(sd) != 0:
            fails.append("missing highwatermark should read 0")
        with open(hwf, "w") as fh:
            fh.write(str(MAX_TASK_IDS - 1))
        if _highwatermark(sd) != MAX_TASK_IDS - 1:
            fails.append("highwatermark not parsed")
        ck("id count below cap passes",
           {"tool_name": "TaskCreate", "session_id": sid,
            "tool_input": {"subject": "ok", "description": "ok"}}, 0)
        with open(hwf, "w") as fh:
            fh.write(str(MAX_TASK_IDS))
        ck("id count at cap blocks TaskCreate",
           {"tool_name": "TaskCreate", "session_id": sid,
            "tool_input": {"subject": "ok", "description": "ok"}}, 2)
        ck("id count at cap does NOT block status-only TaskUpdate",
           {"tool_name": "TaskUpdate", "session_id": sid,
            "tool_input": {"taskId": "1", "status": "deleted"}}, 0)
        ck("id count at cap does NOT block a content TaskUpdate (only creation)",
           {"tool_name": "TaskUpdate", "session_id": sid,
            "tool_input": {"taskId": "1", "description": "short edit"}}, 0)
        with open(hwf, "w") as fh:
            fh.write("garbage")
        if _highwatermark(sd) != 0:
            fails.append("garbage highwatermark should degrade to 0")
        ck("garbage highwatermark fails open",
           {"tool_name": "TaskCreate", "session_id": sid,
            "tool_input": {"subject": "ok", "description": "ok"}}, 0)

        # --- another session's store is NEVER adopted -------------------------
        os.remove(hwf)
        other = os.path.join(tmp, ".claude", "tasks", "session-99999999")
        os.makedirs(other)
        with open(os.path.join(other, ".highwatermark"), "w") as fh:
            fh.write("999")                       # a live session, way over cap
        for i in range(40):                        # and way over budget
            with open(os.path.join(other, "%d.json" % i), "w") as fh:
                json.dump({"id": str(i), "subject": "s" * 60,
                           "description": "d" * 380, "status": "pending"}, fh)
        os.utime(other, None)                      # newest mtime in the tree
        if _store_dir("nope") is not None:
            fails.append("unresolvable sid must not resolve to any store")
        ck("newest foreign store is not adopted on an unresolvable sid",
           {"tool_name": "TaskCreate", "session_id": "nope",
            "tool_input": {"subject": "ok", "description": "ok"}}, 0)
    finally:
        if orig_home is not None:
            os.environ["HOME"] = orig_home
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
