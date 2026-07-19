#!/usr/bin/env bash
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# SessionEnd hook: run pattern-promoter.sh at most once per 24h; surface
# any promotion candidates in the terminal at session-end.
#
# Cross-session theme detection that closes part of the self-improving
# loop: when a theme appears in N+ feedback memories, that's the signal
# the lesson is stable enough to promote to a Rule, hook, or skill —
# but humans rarely scan the memory dir manually. This wraps the
# pattern-promoter CLI in a daily ritual so candidates surface
# automatically at session-end.

set -uo pipefail

LAST_RUN_FILE="$HOME/.claude/.pattern-promoter-last-run"
NOW=$(date +%s)

# Daily rate-limit: skip if last run was <24h ago.
if [ -f "$LAST_RUN_FILE" ]; then
  LAST=$(cat "$LAST_RUN_FILE" 2>/dev/null || echo 0)
  case "$LAST" in ''|*[!0-9]*) LAST=0 ;; esac
  ELAPSED=$(( NOW - LAST ))
  if [ "$ELAPSED" -lt 86400 ]; then
    exit 0
  fi
fi

# Run the promoter; emit only if there's something actionable.
# Stamp the run REGARDLESS of outcome — stamping only on candidates meant
# candidate-free days re-ran the full scan at every session end.
output=$(bash "$HOME/.claude/scripts/pattern-promoter.sh" 2>&1 || true)
echo "$NOW" > "$LAST_RUN_FILE" 2>/dev/null || true

if printf '%s' "$output" | grep -q "Promotion candidates"; then
  # Telemetry.
  LOG="$HOME/.claude/.self-improving-log.jsonl"
  ts=$(date -u +%FT%TZ)
  # `grep -E | wc -l` instead of `grep -c || echo 0` — see check-compute.sh
  # comment for the silent-no-op pattern this avoids.
  candidate_count=$(printf '%s' "$output" | grep -E '^▶ THEME:' | wc -l)
  printf '{"ts":"%s","hook":"pattern-promoter-daily","candidates":%d}\n' "$ts" "$candidate_count" >> "$LOG" 2>/dev/null || true
  echo
  echo "════════════════════════════════════════════════════════════════════"
  echo "  PATTERN-PROMOTER (daily check)"
  echo "  Cross-session feedback themes that may merit Rule/hook/skill"
  echo "  promotion. Run \`~/.claude/scripts/pattern-promoter.sh\` to re-scan."
  echo "════════════════════════════════════════════════════════════════════"
  printf '%s\n' "$output"
  echo "════════════════════════════════════════════════════════════════════"
fi

exit 0
