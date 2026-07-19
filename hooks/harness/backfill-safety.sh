#!/bin/sh
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# PreToolUse Bash: safety check for backfill/port scripts.
#
# v2 (2026-04-27): tightened regex from broad `backfill|port-v1` (matched
# any command mentioning "backfill" — including read-only `head`, `grep`,
# `ls *backfill*`, even `head ~/.claude/hooks/backfill-safety.sh` itself)
# to actual EXECUTION patterns only.
cmd=$(jq -r '.tool_input.command // empty' 2>/dev/null || true)
[ -z "$cmd" ] && exit 0

# Only trigger when actually EXECUTING a backfill / port script:
#   uv run python -m scripts.backfill_*    (the cotton + research pattern)
#   python -m app.backfill_*                (cron entry-point pattern)
#   python scripts/backfill_*.py            (positional invocation)
#   port-v1                                 (legacy port script)
echo "$cmd" | grep -qE '(python([0-9.]*)?[[:space:]]+(-u[[:space:]]+)?-m[[:space:]]+[a-zA-Z_.]+\.backfill_|python([0-9.]*)?[[:space:]]+[^[:space:]]*scripts/backfill_|(^|[^a-z_])port-v1([^a-z_]|$))' || exit 0

# Skip if already includes a limit flag
echo "$cmd" | grep -qE '\-\-limit|\-\-dry.run|\-\-test' && exit 0

cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"BACKFILL SAFETY: This command processes bulk data.\n1. Have you tested with --limit 10 first?\n2. After completion, check for duplicates: SELECT symbol, timestamp, COUNT(*) FROM candles_1s GROUP BY symbol, timestamp HAVING COUNT(*) > 1\n3. If this is a re-run, existing data may create duplicates — consider adding upsert logic first."}}
EOF
