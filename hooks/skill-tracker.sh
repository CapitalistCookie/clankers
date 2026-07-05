#!/usr/bin/env bash
# PostToolUse hook for Skill events — track which skills fire.
# Telemetry only: every failure path exits 0 (never break the session).
set -uo pipefail

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)

# Only track Skill tool uses
[ "$TOOL_NAME" = "Skill" ] || exit 0

SKILL_NAME=$(echo "$INPUT" | jq -r '.tool_input.skill // empty' 2>/dev/null || true)
[ -z "$SKILL_NAME" ] && exit 0

PROJECT="${CLANKER_PROJECT:-global}"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Append to skill tracking log (jq builds valid JSON for any skill/project value)
LOG_DIR="/data/clanker/raw/skills"
mkdir -p "$LOG_DIR" 2>/dev/null || exit 0
jq -cn --arg ts "$TIMESTAMP" --arg project "$PROJECT" --arg skill "$SKILL_NAME" \
    '{timestamp:$ts,project:$project,skill:$skill}' \
    >> "$LOG_DIR/$(date -u +%Y-%m-%d).jsonl" 2>/dev/null || true

exit 0
