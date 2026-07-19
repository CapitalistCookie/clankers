#!/usr/bin/env bash
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# Stop hook: nudge invocation of session-retrospective skill once per
# session when the session has substantial activity.
#
# Why this exists: skills cannot auto-invoke themselves — this Stop-hook
# nudge is what actually triggers ~/.claude/skills/session-retrospective
# after substantial sessions. Self-rate-limited to fire once per session
# via /tmp marker file.

set -uo pipefail

input=$(cat)
session_id=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)
transcript=$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null || true)

[ -z "$session_id" ] && exit 0
[ -z "$transcript" ] && exit 0
[ ! -f "$transcript" ] && exit 0

MARKER="/tmp/.claude-retro-${session_id}"
[ -f "$MARKER" ] && exit 0

# Substantial-activity threshold: transcript has >2000 lines (heuristic
# for "enough work happened to warrant a retrospective"). A short Q&A
# session won't fire this; a multi-feature build will.
lines=$(wc -l < "$transcript" 2>/dev/null || echo 0)
[ "$lines" -lt 2000 ] && exit 0

# Fire exactly once per session.
touch "$MARKER"

# Telemetry.
LOG="$HOME/.claude/.self-improving-log.jsonl"
ts=$(date -u +%FT%TZ)
printf '{"ts":"%s","hook":"retro-prompt","session_id":"%s","transcript_lines":%d}\n' "$ts" "$session_id" "$lines" >> "$LOG" 2>/dev/null || true

# Best-effort cleanup of old markers (>3 days) to avoid /tmp bloat.
find /tmp -maxdepth 1 -name '.claude-retro-*' -mtime +3 -delete 2>/dev/null || true

# additionalContext = model-visible Stop channel (systemMessage only warned the
# USER; the model never saw the nudge — docs-verified 2026-07-05).
cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"SUBSTANTIAL SESSION DETECTED (>2000 transcript lines). Before truly stopping, invoke the session-retrospective skill via the Skill tool: it analyzes what went wrong, what went well, and proposes harness improvements (Rule additions, hook proposals, skill updates). Skip only if you've already filed a retrospective this session."}}
EOF
exit 0
