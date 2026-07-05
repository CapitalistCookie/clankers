#!/usr/bin/env bash
# UserPromptSubmit hook: check if prompt looks like a complex task
# Only advises — never blocks
#
# 2026-07-05 hardening: the prompt used to be interpolated directly into a
# python -c '''…''' string INSIDE shell double-quotes — a prompt containing
# $(…) was shell-expanded (command execution), and quotes/''' crashed the
# check. Prompt + project now travel via the environment; internal errors
# fail open (advisory hook must never break prompt submission).

set -uo pipefail

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.user_prompt // empty' 2>/dev/null || true)

# Skip short prompts (commands, yes/no, etc.)
[ ${#PROMPT} -lt 50 ] && exit 0

# Skip if no project context
PROJECT="${CLANKER_PROJECT:-}"
[ -z "$PROJECT" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Run decomposition check (data via env, code via stdin — nothing interpolated)
RESULT=$(CLANKER_LIB="$SCRIPT_DIR/../lib" \
         CLANKER_CHECK_PROMPT="$PROMPT" \
         CLANKER_CHECK_PROJECT="$PROJECT" \
         python3 - <<'PY' 2>/dev/null || true
import os, sys
sys.path.insert(0, os.environ.get("CLANKER_LIB", ""))
from decompose import analyze_prompt
r = analyze_prompt(os.environ.get("CLANKER_CHECK_PROMPT", ""),
                   os.environ.get("CLANKER_CHECK_PROJECT", ""))
if r and r.get("risk_level") == "high":
    signals = "; ".join(r.get("signals", [])[:3])
    print(f"DECOMPOSITION ADVISORY: {signals}. Consider breaking into smaller tasks.")
PY
)

if [ -n "$RESULT" ]; then
    # Output as advisory context (doesn't block); jq builds valid JSON for any content
    jq -cn --arg ctx "$RESULT" \
        '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$ctx}}' 2>/dev/null || true
fi

exit 0
