"""classify_rest_state / _craft_notification — the ntfy accuracy layer.

Pure-function tests: pane-tail fixtures modeled on real Claude Code rest
states (permission dialog, AskUserQuestion options, usage-limit banner,
API-error banner, plain finished prompt)."""

import os
import sys
import tempfile

# Same import-time isolation dance as test_webauth.py, for the same reason:
# importing serve pulls in webauth, which captures CLANKER_DATA into module
# constants AT IMPORT (collection) time — before any fixture runs. Without
# this, whichever test module imports the serve/webauth chain first freezes
# it onto PRODUCTION /data/clanker for the whole session (bit us 2026-07-19:
# webauth tests started colliding against the live user store).
_OLD_DATA = os.environ.get("CLANKER_DATA")
os.environ["CLANKER_DATA"] = tempfile.mkdtemp(prefix="clk-servestate-test-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from serve import (  # noqa: E402
    classify_rest_state,
    classify_working_state,
    _craft_notification,
    _last_activity_line,
)
if _OLD_DATA is None:
    os.environ.pop("CLANKER_DATA", None)
else:
    os.environ["CLANKER_DATA"] = _OLD_DATA


PERMISSION_DIALOG = """\
╭──────────────────────────────────────────────╮
│ Bash command                                 │
│   rm -rf build/                              │
│ Do you want to proceed?                      │
│ ❯ 1. Yes                                     │
│   2. Yes, and don't ask again this session   │
│   3. No, and tell Claude what to do          │
╰──────────────────────────────────────────────╯"""

QUESTION_DIALOG = """\
│ Which library should we use?                 │
│ ❯ 1. requests                                │
│   2. httpx                                   │
│   3. Other                                   │"""

LIMIT_BANNER = """\
  You've reached your usage limit.
  Your limit resets at 3am (Asia/Seoul).
❯ """

ERROR_BANNER = """\
  API Error: 529 overloaded_error
  Request timed out after 3 retries
❯ """

PLAIN_PROMPT = """\
⏺ Done — the tests pass and the fix is committed.
╭──────────────────────────────────────────────╮
│ ❯                                            │
╰──────────────────────────────────────────────╯"""


def test_permission_dialog_is_decision():
    sub, detail = classify_rest_state(PERMISSION_DIALOG)
    assert sub == "decision"
    assert "do you want" in detail.lower()


def test_question_options_are_decision():
    sub, _ = classify_rest_state(QUESTION_DIALOG)
    assert sub == "decision"


def test_limit_banner():
    sub, detail = classify_rest_state(LIMIT_BANNER)
    assert sub == "limit"
    assert "resets" in detail.lower()


def test_error_banner():
    sub, _ = classify_rest_state(ERROR_BANNER)
    assert sub == "error"


def test_plain_prompt_is_waiting():
    sub, detail = classify_rest_state(PLAIN_PROMPT)
    assert sub == "waiting"
    assert detail == ""


def test_empty_tail_is_waiting():
    assert classify_rest_state("")[0] == "waiting"
    assert classify_rest_state(None)[0] == "waiting"


AGENTS_PANE = """\
⏺ Task(Audit the hook layer)
  ⎿  Running…
⏺ Task(Audit the memory subsystem)
  ⎿  Running…
· 3 agents running
✻ Orchestrating… (esc to interrupt)"""

COMPACTING_PANE = """\
✻ Compacting conversation… (esc to interrupt)"""

PLAIN_WORKING = """\
⏺ Running the fast suite before committing.
✻ Cogitating… (2m 14s · esc to interrupt)"""


def test_agents_pane_counts_and_details():
    wsub, agents, detail = classify_working_state(AGENTS_PANE)
    assert wsub == "subagents"
    assert agents == 3  # trusts the explicit "3 agents running" over line count
    assert "Task(" in detail


def test_task_lines_counted_without_explicit_total():
    tail = "⏺ Task(one)\n⏺ Task(two)\n✻ Working…"
    wsub, agents, _ = classify_working_state(tail)
    assert wsub == "subagents" and agents == 2


def test_compacting_detected():
    assert classify_working_state(COMPACTING_PANE)[0] == "compacting"


def test_plain_working():
    wsub, agents, detail = classify_working_state(PLAIN_WORKING)
    assert wsub == "working" and agents == 0
    assert detail.startswith("⏺")  # activity line preferred


def test_last_activity_prefers_action_lines():
    assert _last_activity_line(PLAIN_WORKING).startswith("⏺ Running the fast suite")
    assert _last_activity_line("") == ""


def test_craft_priorities_and_titles():
    t, p, _, _ = _craft_notification("eigenstate", PERMISSION_DIALOG)
    assert p == "max" and "decision" in t
    t, p, _, body = _craft_notification("eigenstate", LIMIT_BANNER)
    assert p == "default" and "limit" in t and "resets" in body.lower()
    t, p, _, _ = _craft_notification("eigenstate", ERROR_BANNER)
    assert p == "high" and "erroring" in t
    t, p, _, body = _craft_notification("eigenstate", PLAIN_PROMPT)
    assert p == "high" and "your turn" in t
    assert "Done" in body  # the content line, not a box-drawing row
