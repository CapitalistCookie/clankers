#!/usr/bin/env bash
# PostToolUse hook for Skill events — track which skills fire.
# Telemetry only: every failure path exits 0 (never break the session), and
# logs itself to the hook-error log (block below).
set -uo pipefail

# ---- hook-error log: the same block in every clanker bash hook ------------------
# A failure that the hook swallows (the hook stays fail-open for the session)
# appends one JSON line {ts, hook, session_id, cwd, rc, stderr_tail} to
# ${CLANKER_DATA:-/data/clanker}/raw/health/hook-errors-<UTC day>.jsonl, where
# `clanker doctor --harness` counts it. hook_err never blocks, never writes to
# stdout and never changes the hook's exit code, under set -e and set -u too.
# Usage: hook_err <rc> <step> [<error text>]. stderr_tail is "<step>: " plus the
# end of the text, 300 characters at most. Put the raw hook payload in
# HOOK_ERR_IN so that the row names the session and its cwd.
HOOK_ERR_IN=""
hook_err_str() {
    local s="$1"
    s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
    s="${s//$'\t'/\\t}"; s="${s//$'\r'/\\r}"; s="${s//$'\n'/\\n}"
    printf '"%s"' "$(printf '%s' "$s" | LC_ALL=C tr -d '\000-\010\013\014\016-\037\177')"
}
hook_err() {
    (
        set +eu
        rc="$1" step="${2:-?}" text="$3" sid="" re=""
        [[ $rc =~ ^-?[0-9]+$ ]] || rc=1
        room=$(( 298 - ${#step} ))
        if (( room <= 0 )); then text=""
        elif (( ${#text} > room )); then text="${text:${#text}-room}"; fi
        msg="$step${text:+: $text}"
        re='"session_id"[[:space:]]*:[[:space:]]*"([^"\\]*)"'
        [[ $HOOK_ERR_IN =~ $re ]] && sid="${BASH_REMATCH[1]}"
        re='"cwd"[[:space:]]*:[[:space:]]*"(([^"\\]|\\.)*)"'
        if [[ $HOOK_ERR_IN =~ $re ]]; then cwd="\"${BASH_REMATCH[1]}\""
        else cwd=$(hook_err_str "$PWD"); fi
        ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        d="${CLANKER_DATA:-/data/clanker}/raw/health"
        mkdir -p "$d" && printf '{"ts":"%s","hook":%s,"session_id":"%s","cwd":%s,"rc":%s,"stderr_tail":%s}\n' \
            "$ts" "$(hook_err_str "${0##*/}")" "$sid" "$cwd" "$rc" "$(hook_err_str "${msg:0:300}")" \
            >> "$d/hook-errors-${ts%%T*}.jsonl"
        exit 0
    ) </dev/null >/dev/null 2>&1
    return 0
}
# ---- end of hook-error log --------------------------------------------------------

INPUT=$(cat)
HOOK_ERR_IN="$INPUT"
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null) \
    || { hook_err $? "jq: parse the hook input"; exit 0; }

# Only track Skill tool uses
[ "$TOOL_NAME" = "Skill" ] || exit 0

SKILL_NAME=$(echo "$INPUT" | jq -r '.tool_input.skill // empty' 2>/dev/null) \
    || { hook_err $? "jq: read tool_input.skill"; exit 0; }
[ -z "$SKILL_NAME" ] && exit 0

PROJECT="${CLANKER_PROJECT:-global}"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Append to skill tracking log (jq builds valid JSON for any skill/project value)
LOG_DIR="/data/clanker/raw/skills"
mkdir -p "$LOG_DIR" 2>/dev/null || { hook_err $? "mkdir $LOG_DIR"; exit 0; }
jq -cn --arg ts "$TIMESTAMP" --arg project "$PROJECT" --arg skill "$SKILL_NAME" \
    '{timestamp:$ts,project:$project,skill:$skill}' \
    >> "$LOG_DIR/$(date -u +%Y-%m-%d).jsonl" 2>/dev/null || hook_err $? "append the skill row"

exit 0
