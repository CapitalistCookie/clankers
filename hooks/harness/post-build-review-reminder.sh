#!/bin/bash
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# PostToolUse hook: remind to provide comments and concerns after git commit/push
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null || true)
[ -z "$COMMAND" ] && exit 0

# Only trigger on git commit or git push
if echo "$COMMAND" | grep -qE '^git (commit|push)\b'; then
  echo '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"REMINDER: Provide comments and concerns — what'\''s clean, what gaps exist, architectural implications for next steps."}}'
fi
