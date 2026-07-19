#!/bin/sh
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# Bash PreToolUse: auto-run deploy pre-flight before deploy commands
cmd=$(jq -r '.tool_input.command // empty' 2>/dev/null || true)
[ -z "$cmd" ] && exit 0

# HARD BLOCK: --delete-data destroys all user data, candles, predictions.
# New tables do NOT require --delete-data. Only column changes to existing tables need it.
# Override: set DEPLOY_DELETE_DATA_OVERRIDE=1 when the user explicitly authorizes it.
if echo "$cmd" | grep -qE 'spacetime publish.*--delete-data|deploy-vm\.sh.*--module-delete-data'; then
  if echo "$cmd" | grep -qE 'DEPLOY_DELETE_DATA_OVERRIDE=1'; then
    # User authorized override — allow through but warn
    cat <<EOF
{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"--delete-data OVERRIDE authorized by user. This DESTROYS ALL DATA. init.ts will re-seed."}}
EOF
    exit 0
  fi
  jq -cn --arg r "BLOCKED: --delete-data DESTROYS ALL DATA (candles, predictions, users, sessions). New tables and new reducer params do NOT require --delete-data — use --module instead. If user authorizes, prefix command with DEPLOY_DELETE_DATA_OVERRIDE=1." \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
fi

# Only trigger on full deploy script or spacetime publish — NOT individual service restarts
echo "$cmd" | grep -qE 'deploy-vm\.sh|spacetime publish' || exit 0

# Skip if this IS the preflight, runtime check, or seed
echo "$cmd" | grep -qE 'deploy-preflight|deploy-runtime-check|seed-data' && exit 0

# De-personalized: preflight path comes from env (CLANKER_DEPLOY_PREFLIGHT).
# Empty default → the [ -f "$PREFLIGHT" ] guard below skips the preflight block,
# so the distributed copy carries no operator checkout path and is inert unless set.
PREFLIGHT="${CLANKER_DEPLOY_PREFLIGHT:-}"

if [ -f "$PREFLIGHT" ]; then
  result=$(bash "$PREFLIGHT" 2>&1)
  status=$?

  if [ $status -ne 0 ]; then
    errors=$(echo "$result" | grep "FAIL:" | head -5)
    # jq --arg: $errors is arbitrary text — interpolating it into a heredoc
    # produced invalid JSON whenever a FAIL line contained a quote.
    jq -cn --arg r "DEPLOY PRE-FLIGHT FAILED. Fix before deploying:
$errors

Run: bash scripts/deploy-preflight.sh" \
      '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  else
    cat <<EOF
{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"Deploy pre-flight passed. BEFORE deploying to VM: have you tested this locally with 'spacetime start --in-memory'? If not, STOP and test locally first. After deploy completes, run: bash scripts/deploy-runtime-check.sh"}}
EOF
  fi
fi
