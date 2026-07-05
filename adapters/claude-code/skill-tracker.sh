#!/usr/bin/env bash
# PostToolUse hook for Skill events — track which skills fire
set -euo pipefail

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

# Only track Skill tool uses
[ "$TOOL_NAME" = "Skill" ] || exit 0

SKILL_NAME=$(echo "$INPUT" | jq -r '.tool_input.skill // empty')
[ -z "$SKILL_NAME" ] && exit 0

PROJECT="${CLANKER_PROJECT:-global}"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Append to skill tracking log
LOG_DIR="/data/clanker/raw/skills"
mkdir -p "$LOG_DIR"
echo "{\"timestamp\":\"$TIMESTAMP\",\"project\":\"$PROJECT\",\"skill\":\"$SKILL_NAME\"}" \
    >> "$LOG_DIR/$(date -u +%Y-%m-%d).jsonl"

exit 0
