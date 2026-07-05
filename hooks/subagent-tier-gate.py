#!/usr/bin/env python3
"""PreToolUse gate on Agent dispatches (GLOBAL) — operator ruling 2026-07-05:
subagents run on an EXPLICITLY chosen model (default choice: opus; orchestration
itself may be fable or opus). An Agent call with NO model field silently inherits
whatever the parent runs — exactly the drift the ruling forbids — so this gate
fail-closes on missing `model` and tells the model how to fix the call.

Task tool calls and non-Agent tools pass through untouched. Never crashes the
session: any internal error exits 0 (fail-open on OUR bugs, fail-closed only on
the designed condition)."""
import json
import sys


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool = data.get("tool_name", "")
    if tool != "Agent":
        return 0
    ti = data.get("tool_input") or {}
    model = (ti.get("model") or "").strip()
    if model:
        return 0  # explicit choice made — ruling satisfied (opus expected for subagents)
    sys.stderr.write(
        "subagent-tier-gate: Agent dispatch has NO explicit `model` — operator ruling "
        "(2026-07-05, memory feedback-subagent-model-tier): every subagent dispatch "
        "carries an explicit model; use `model: \"opus\"` unless this lane genuinely "
        "needs fable (harness/governance-quality work). Re-issue the Agent call with "
        "the model field set.\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
