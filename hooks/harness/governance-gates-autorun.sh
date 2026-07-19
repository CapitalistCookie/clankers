#!/bin/bash
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# governance-gates-autorun — PostToolUse(Edit|Write|MultiEdit) hook.
# When a governed research-registry file is modified (any <name>_spec/ dir:
# SPEC.md, prereg.yml, anchors.yml, freezes.yml, eval_sources.yml, governance.yml,
# conformance.yml, reproduction_days.yml), auto-run that spec dir's ci_gates.sh and
# inject the verdict into model context. Model-agnostic enforcement: any session
# (esp. Opus) sees FAIL immediately after the edit — it cannot silently break a
# registry and move on. Added 2026-07-02 (operator: "Opus bulletproof, nonnegotiable").
set -uo pipefail

payload=$(cat)
f=$(printf '%s' "$payload" | jq -r '.tool_input.file_path // .tool_response.filePath // empty' 2>/dev/null)
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
  jq -cn --arg ctx "$ctx" '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}'
else
  fails=$(printf '%s' "$out" | grep -E '\[FAIL\]|GATE FAILURE|SELFTEST FAIL' | head -8)
  ctx="🔴 GOVERNANCE GATES FAILED (exit $rc) — the edit of $base violated the $(basename "$dir") governance. THIS MUST BE FIXED BEFORE ANY OTHER WORK; do not proceed, do not weaken the gate — fix the violation or register an explicit expiring waiver.
$fails
$tail_out"
  jq -cn --arg ctx "$ctx" --arg sm "Governance gates FAILED after $base edit — fix before proceeding" \
    '{systemMessage:$sm, hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}'
fi
exit 0
