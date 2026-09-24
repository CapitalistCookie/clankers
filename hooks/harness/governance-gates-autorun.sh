#!/bin/bash
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# governance-gates-autorun — PostToolUse(Edit|Write|MultiEdit) hook.
# When a governed research-registry file is modified (any <name>_spec/ dir:
# SPEC.md, prereg.yml, anchors.yml, freezes.yml, eval_sources.yml, governance.yml,
# conformance.yml, reproduction_days.yml), auto-run that spec dir's ci_gates.sh and
# inject the verdict into model context. Model-agnostic enforcement: any session
# (esp. Opus) sees FAIL immediately after the edit — it cannot silently break a
# registry and move on. Added 2026-07-02 (operator: "Opus bulletproof, nonnegotiable").
# Fail-visible (2026-09-24): a failed parse or emit lands in the hook-error log
# (block below). A gate FAIL is the designed verdict, not a hook error.
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

payload=$(cat)
HOOK_ERR_IN="$payload"
f=$(printf '%s' "$payload" | jq -r '.tool_input.file_path // .tool_response.filePath // empty' 2>/dev/null) \
  || { hook_err $? "jq: parse the hook input"; exit 0; }
[ -z "$f" ] && exit 0

base=$(basename "$f")
case "$base" in
  SPEC.md|prereg.yml|anchors.yml|freezes.yml|eval_sources.yml|governance.yml|conformance.yml|reproduction_days.yml) ;;
  *) exit 0 ;;
esac

dir=$(dirname "$f")
case "$(basename "$dir")" in
  *_spec) ;;
  *) exit 0 ;;
esac

ci="$dir/ci_gates.sh"
[ -x "$ci" ] || [ -f "$ci" ] || exit 0

out=$(cd "$dir" && timeout 120 bash ci_gates.sh 2>&1)
rc=$?
tail_out=$(printf '%s' "$out" | tail -6)

if [ $rc -eq 0 ]; then
  ctx="GOVERNANCE GATES auto-run ($(basename "$dir")/ci_gates.sh) after edit of $base: ALL GREEN (exit 0).
$tail_out"
  jq -cn --arg ctx "$ctx" '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}' \
    || hook_err $? "emit the GREEN verdict (jq)"
else
  fails=$(printf '%s' "$out" | grep -E '\[FAIL\]|GATE FAILURE|SELFTEST FAIL' | head -8)
  ctx="🔴 GOVERNANCE GATES FAILED (exit $rc) — the edit of $base violated the $(basename "$dir") governance. THIS MUST BE FIXED BEFORE ANY OTHER WORK; do not proceed, do not weaken the gate — fix the violation or register an explicit expiring waiver.
$fails
$tail_out"
  jq -cn --arg ctx "$ctx" --arg sm "Governance gates FAILED after $base edit — fix before proceeding" \
    '{systemMessage:$sm, hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}' \
    || hook_err $? "emit the FAILED verdict (jq): the model does not see it"
fi
exit 0
