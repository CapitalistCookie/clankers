#!/usr/bin/env bash
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# Iron-law Stop hook — flag specific HIGH-STAKES success claims that don't
# have matching tool-output evidence in the recent transcript.
#
# Sister to completion-verification.sh:
#   - completion-verification.sh = soft reminder on generic words (done,
#     complete, deployed, finished, merged) → outputs additionalContext.
#   - iron-law-check.sh = harder check on SPECIFIC tokens (deploy success,
#     prepush green, pushed) where unverified claims have caused real
#     incidents this project. Exits 2 (asyncRewake) so the model is
#     re-woken with the violation, not just nudged.
#
# Real incidents that motivated this hook (2026-04-26 cottondashboard):
#   1. Deploy "SUCCESS" echoed by deploy-via-bundle.sh BUT backend was
#      crash-looping (ModuleNotFoundError). Caught only by post-hoc SSH.
#   2. Background bash exit-code-0 was tail's exit, not prepush's. The
#      actual run had 12 frontend test failures. Pipe exit ≠ evidence.
#   3. "pushed" without verifying `main -> main` line in git output.
#
# ACTIVATED 2026-07-05 (operator go, harness overhaul): was KNOWN-DEAD since
# wiring — Stop payloads carry NO `assistant_message`, so the old jq read was
# always empty and this hook never fired once (telemetry-proven). The message
# is now extracted from transcript_path via last-assistant-msg.py. RED/GREEN
# selftest: `bash iron-law-check.sh --selftest` (run it after ANY edit here).

set -uo pipefail

HOOKS_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── selftest: synthetic violating + clean transcripts must produce exit 2 / 0 ──
if [ "${1:-}" = "--selftest" ]; then
  T=$(mktemp -d)
  trap 'rm -rf "$T"' EXIT
  rm -f /tmp/.ironlaw-selftest* 2>/dev/null  # once-per-session markers must not mask RED reruns
  # RED: assistant claims deploy success; no evidence token in tool output
  printf '%s\n' \
    '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"deploy it"}]}}' \
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Deployed successfully to prod — all done."}]}}' \
    > "$T/red.jsonl"
  printf '{"session_id":"selftest","transcript_path":"%s","stop_hook_active":true}' "$T/red.jsonl" \
    | bash "$0" >/dev/null 2>&1
  red_rc=$?
  # GREEN: same claim WITH healthz evidence in a prior tool_result line
  printf '%s\n' \
    '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"healthz: 200\nActive: active (running)"}]}}' \
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Deployed successfully to prod — healthz 200 verified."}]}}' \
    > "$T/green.jsonl"
  printf '{"session_id":"selftest","transcript_path":"%s","stop_hook_active":true}' "$T/green.jsonl" \
    | bash "$0" >/dev/null 2>&1
  green_rc=$?
  # RED via the NATIVE last_assistant_message field (no transcript at all)
  printf '{"session_id":"selftest2","stop_hook_active":true,"last_assistant_message":"Deployed successfully to prod — all done."}' \
    | bash "$0" >/dev/null 2>&1
  native_rc=$?
  # CITATION-GREEN: backtick-quoted tokens are citations, not claims (3rd fire)
  printf '{"session_id":"selftest3","last_assistant_message":"the suite printed `all green` and the gate quoted `pushed to origin` earlier"}' \
    | bash "$0" >/dev/null 2>&1
  cite_rc=$?
  # ONCE-PER-SESSION: identical unevidenced claim, same session -> 2nd run silent
  printf '{"session_id":"selftest4","last_assistant_message":"prepush green, all tests pass"}' | bash "$0" >/dev/null 2>&1
  first_rc=$?
  printf '{"session_id":"selftest4","last_assistant_message":"prepush green, all tests pass"}' | bash "$0" >/dev/null 2>&1
  second_rc=$?
  rm -f /tmp/.ironlaw-selftest* 2>/dev/null
  if [ "$red_rc" -eq 2 ] && [ "$green_rc" -eq 0 ] && [ "$native_rc" -eq 2 ] \
     && [ "$cite_rc" -eq 0 ] && [ "$first_rc" -eq 2 ] && [ "$second_rc" -eq 0 ]; then
    echo "iron-law selftest: RED->2, GREEN->0, NATIVE-RED->2, CITATION->0, ONCE-PER-SESSION 2then0 — PASS"
    exit 0
  fi
  echo "iron-law selftest FAIL (red=$red_rc/2 green=$green_rc/0 native=$native_rc/2 cite=$cite_rc/0 once=$first_rc/2,$second_rc/0)"
  exit 1
fi

input=$(cat)
transcript=$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null || true)
# 2.1.x Stop payloads carry the message NATIVELY as `last_assistant_message`
# (docs-verified 2026-07-05) — prefer it (no python spawn); transcript-tail
# extraction remains as fallback for older payloads and the selftest fixtures.
msg=$(printf '%s' "$input" | jq -r '.last_assistant_message // empty' 2>/dev/null || true)
if [ -z "$msg" ] && [ -n "$transcript" ] && [ -f "$transcript" ]; then
  msg=$(python3 "$HOOKS_DIR/last-assistant-msg.py" "$transcript" 2>/dev/null || true)
fi

# Nothing to check if no assistant message could be extracted.
[ -z "$msg" ] && exit 0

# Strip backtick/quote-enclosed segments BEFORE claim matching (3rd live fire,
# 2026-07-05): citing an evidence token in prose (`all green`) is a CITATION,
# not a claim — without this, every compliance message re-triggers the gate
# (an infinite rewake ping-pong by construction). Mirrors scope-calibration.
msg=$(printf '%s' "$msg" | sed -E 's/`[^`]*`//g; s/"[^"]*"//g')

# Once-per-session-per-pattern guard: asyncRewake re-fires on every Stop; a
# violation class already surfaced this session must not ping-pong.
session_guard=$(printf '%s' "$input" | jq -r '.session_id // "unknown"' 2>/dev/null || echo unknown)
session_guard=${session_guard//[^a-zA-Z0-9_-]/}
fired_marker() { [ -f "/tmp/.ironlaw-${session_guard}-$1" ]; }
mark_fired() { touch "/tmp/.ironlaw-${session_guard}-$1" 2>/dev/null || true; }

# Pull the last ~60 lines of tool output from the JSONL transcript so we
# can grep for evidence tokens. Best-effort — if jq fails, recent_output
# is empty and every claim is treated as unverified (false positives are
# the safer failure mode for an iron-law check).
recent_output=""
if [ -n "$transcript" ] && [ -f "$transcript" ]; then
  # 400-line window (was 80): first live fire 2026-07-05 was a long final
  # summary whose evidence tokens sat a few messages back — real evidence,
  # too-small window. Long-turn sessions need the deeper look-back.
  recent_output=$(tail -n 400 "$transcript" 2>/dev/null \
    | jq -rs '.[] | (.tool_result? // .message?.content? // "") | tostring' 2>/dev/null \
    || true)
fi

violations=()

# --- Pattern 1: deploy success claim ---
if ! fired_marker deploy && printf '%s' "$msg" | grep -iqE '(deploy(ed|ment)? success(ful)?|successfully deployed|deploy.*green|live on (CT|prod)|deploy SUCCESS)'; then
  if ! printf '%s' "$recent_output" | grep -qE '(healthz: 200|healthz.*200|Active: active|active \(running\)|\[deploy\] SUCCESS)'; then
    violations+=("DEPLOY-CLAIM: said success without recent 'healthz: 200' / 'Active: active' / '[deploy] SUCCESS' token in tool output")
    mark_fired deploy
  fi
fi

# --- Pattern 2: prepush green / all green ---
if ! fired_marker prepush && printf '%s' "$msg" | grep -iqE '(prepush.*green|all (tests )?green|tests.*all pass(ing|ed)?|all tests pass)'; then
  # Evidence dialect extended 2026-07-05 (2nd live fire): local CI prints
  # "[ci/fast] all green" and pytest prints "N passed in Ns" — both are real
  # success tokens this box emits.
  if ! printf '%s' "$recent_output" | grep -qE '(prepush: all green|\[ci[/-]fast\] all green|ALL GREEN|✓ frontend test|tests? passed|[0-9]+ passed( in [0-9.]+s)?|Tests +Files +.*passed)'; then
    violations+=("PREPUSH-CLAIM: said 'all green' / 'prepush green' without that literal token in recent tool output (pipe exit code is not evidence — \`tail\` masks failures)")
    mark_fired prepush
  fi
fi

# --- Pattern 3: pushed claim ---
if ! fired_marker push && printf '%s' "$msg" | grep -iqE '(pushed (to )?(origin|main|remote)|push (succeeded|successful)|push success)'; then
  # "Everything up-to-date" = remote already has HEAD (valid push evidence);
  # quiet pushes print nothing — the convention is now push NON-quiet.
  if ! printf '%s' "$recent_output" | grep -qE '(\* \[new branch\]|main -> main|master -> master|HEAD -> |To https?://|To git@|Everything up-to-date)'; then
    violations+=("PUSH-CLAIM: said pushed without 'main -> main' / 'To <remote>' in recent tool output")
    mark_fired push
  fi
fi

# --- Pattern 4: deployed/shipped to specific host ---
if ! fired_marker host && printf '%s' "$msg" | grep -iqE '(deployed to (CT [0-9]+|192\.168|prod)|shipped to (CT [0-9]+|prod))'; then
  if ! printf '%s' "$recent_output" | grep -qE '(healthz: 200|Active: active|systemctl.*active|deploy.*SUCCESS)'; then
    violations+=("HOST-DEPLOY-CLAIM: said deployed to specific host without healthz/active/SUCCESS evidence")
    mark_fired host
  fi
fi

# Telemetry: log every fire (silent or violating) to JSONL for efficacy tracking.
LOG="$HOME/.claude/.self-improving-log.jsonl"
session_id=$(printf '%s' "$input" | jq -r '.session_id // "unknown"' 2>/dev/null || echo unknown)
ts=$(date -u +%FT%TZ)
n="${#violations[@]}"
printf '{"ts":"%s","hook":"iron-law-check","session_id":"%s","violations":%d}\n' "$ts" "$session_id" "$n" >> "$LOG" 2>/dev/null || true

# No violations → silent success.
[ "$n" -eq 0 ] && exit 0

# Emit violations on STDERR + exit 2 — the Stop-hook contract feeds stderr back
# to the model on exit 2 (stdout on exit 2 is dropped by the strict validator).
{
  echo "Iron-law violations detected:"
  for v in "${violations[@]}"; do
    echo "  • $v"
  done
  echo
  echo "Re-run the matching verification command and quote its success-token output before reasserting the claim. Pipe-exit-codes through tail/grep are not evidence — the actual success token must appear."
} >&2

exit 2
