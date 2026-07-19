#!/usr/bin/env bash
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# stop-dispatch.sh — ONE Stop hook consolidating the 4 separate Stop commands
# previously wired in ~/.claude/settings.json (iron-law-check, retro-prompt,
# scope-calibration, status-stale-nudge). Mirrors pretooluse-bash-dispatch.sh:
# read the payload ONCE, run each gate on it, merge advisory outputs into ONE
# envelope, and let the first blocker win.
#
# WHY: 4 Stop hooks = 4 process spawns (each re-reading + re-jq-parsing the same
# stdin) on EVERY Stop — and iron-law re-fires via asyncRewake on every Stop, so
# this is a hot path. One dispatcher reads stdin once and hands the SAME bytes to
# each gate; gate decisions are unchanged, just cheaper to reach.
#
# CONTRACT (Claude Code 2.1.x Stop):
#   exit 2 + stderr → blocking; stderr is fed back to the model (iron-law uses
#                     this + asyncRewake). FIRST blocker wins — iron-law runs
#                     first, so its exit-2 output is forwarded verbatim and no
#                     later gate runs.
#   exit 0 + JSON stdout with hookSpecificOutput.additionalContext → advisory,
#                     MERGED across gates into one envelope (systemMessage too).
#   plain stderr from an exit-0 gate → passed through (transcript-visible).
#   gate exit 124/137 → hit its per-gate timeout budget: fail-OPEN, skip it.
#   missing gate file → skipped silently (partial/generic installs).
#
# Selftest: bash stop-dispatch.sh --selftest  (run after ANY edit here).
set -uo pipefail

H="$HOME/.claude/hooks"

# ── selftest: fake-HOME + fixture gates prove the dispatcher's plumbing ─────────
if [ "${1:-}" = "--selftest" ]; then
  SELF=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")   # abs path — fake-HOME sub-runs re-invoke it
  T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
  HK="$T/home/.claude/hooks"
  mkdir -p "$HK/clanker-dist"
  # Minimal Stop stdin (the fields the real gates parse: session_id / transcript_path / cwd).
  STDIN='{"session_id":"stopdispatch-selftest","transcript_path":"","cwd":"/tmp"}'
  fails=""

  silent() { cat > "$1" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  }
  silent "$HK/iron-law-check.sh"
  silent "$HK/retro-prompt.sh"
  silent "$HK/scope-calibration.sh"
  silent "$HK/clanker-dist/status-stale-nudge.sh"

  # (1) claim-free turn: every gate silent -> exit 0, no stdout envelope.
  out1=$(printf '%s' "$STDIN" | HOME="$T/home" bash "$SELF" 2>/dev/null); rc1=$?
  [ "$rc1" -eq 0 ] || fails="$fails claim-free-rc($rc1)"
  [ -z "$out1" ] || fails="$fails claim-free-stdout"

  # (2) iron-law RED: first gate exits 2 with a message -> dispatcher exits 2 and
  #     forwards it; the downstream gate must NOT run (first blocker wins).
  cat > "$HK/iron-law-check.sh" <<'SH'
#!/usr/bin/env bash
echo "Iron-law violations detected:" >&2
echo "  • DEPLOY-CLAIM: said success without evidence" >&2
exit 2
SH
  cat > "$HK/retro-prompt.sh" <<SH
#!/usr/bin/env bash
touch "$T/ran-downstream"
exit 0
SH
  rm -f "$T/ran-downstream"
  err2=$(printf '%s' "$STDIN" | HOME="$T/home" bash "$SELF" 2>&1 >/dev/null); rc2=$?
  [ "$rc2" -eq 2 ] || fails="$fails ironlaw-rc($rc2)"
  printf '%s' "$err2" | grep -q "Iron-law violations detected" || fails="$fails ironlaw-msg"
  [ -f "$T/ran-downstream" ] && fails="$fails first-blocker-not-honored"

  # (3) two advisory outputs merge into ONE envelope.
  silent "$HK/iron-law-check.sh"
  cat > "$HK/retro-prompt.sh" <<'SH'
#!/usr/bin/env bash
cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"RETRO_CTX"}}
JSON
exit 0
SH
  cat > "$HK/scope-calibration.sh" <<'SH'
#!/usr/bin/env bash
cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"SCOPE_CTX"}}
JSON
exit 0
SH
  out3=$(printf '%s' "$STDIN" | HOME="$T/home" bash "$SELF" 2>/dev/null); rc3=$?
  [ "$rc3" -eq 0 ] || fails="$fails merge-rc($rc3)"
  if printf '%s' "$out3" | jq -e '.hookSpecificOutput.additionalContext' >/dev/null 2>&1; then
    ac=$(printf '%s' "$out3" | jq -r '.hookSpecificOutput.additionalContext')
    case "$ac" in
      *RETRO_CTX*SCOPE_CTX*|*SCOPE_CTX*RETRO_CTX*) ;;
      *) fails="$fails merge-missing-token" ;;
    esac
  else
    fails="$fails merge-not-one-json"
  fi

  if [ -z "$fails" ]; then
    echo "stop-dispatch selftest: claim-free->0, iron-law RED->2 (msg, downstream skipped), 2 advisories merged — PASS"
    exit 0
  fi
  echo "stop-dispatch selftest FAIL:$fails"
  printf '  out3=%s\n' "$out3" >&2
  exit 1
fi

# ── normal dispatch ────────────────────────────────────────────────────────────
INPUT=$(cat 2>/dev/null || true)

ERRF=$(mktemp "${TMPDIR:-/tmp}/stopd-err.XXXXXX") || ERRF=/dev/null
trap 'rm -f "$ERRF" 2>/dev/null' EXIT

declare -a CTX=()
declare -a SYS=()

# Collect an exit-0 gate's JSON stdout: additionalContext + systemMessage merge.
classify_and_route() {
  local label="$1" out="$2" ctx sys
  ctx=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null) || ctx=""
  [ -n "$ctx" ] && CTX+=("$ctx")
  sys=$(printf '%s' "$out" | jq -r '.systemMessage // empty' 2>/dev/null) || sys=""
  [ -n "$sys" ] && SYS+=("$sys")
  if [ -z "$ctx" ] && [ -z "$sys" ]; then
    printf 'stop-dispatch: %s emitted unrecognized stdout (ignored): %.200s\n' "$label" "$out" >&2
  fi
}

# Run one gate on the shared stdin with its own settings.json timeout budget.
run_gate() {
  local label="$1" budget="$2" path="$3"
  [ -f "$path" ] || return 0   # gate not installed on this box — skip silently
  local out rc err=""
  out=$(printf '%s' "$INPUT" | timeout "$budget" bash "$path" 2>"$ERRF"); rc=$?
  [ -s "$ERRF" ] && err=$(<"$ERRF")
  : > "$ERRF" 2>/dev/null || true
  if [ "$rc" -eq 2 ]; then
    # Blocking gate: first blocker wins. Forward its output verbatim, exit 2.
    [ -n "$out" ] && printf '%s\n' "$out"
    [ -n "$err" ] && printf '%s\n' "$err" >&2
    exit 2
  fi
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    printf 'stop-dispatch: %s exceeded its %ss budget — skipped (fail-open)\n' "$label" "$budget" >&2
    return 0
  fi
  if [ "$rc" -ne 0 ]; then
    # Non-blocking error: fail-open, surface stderr like the old per-hook notice.
    [ -n "$err" ] && printf '%s\n' "$err" >&2
    return 0
  fi
  # rc==0: advisory stderr passes through; route stdout (if any).
  [ -n "$err" ] && printf '%s\n' "$err" >&2
  [ -z "${out//[$' \t\r\n']/}" ] && return 0
  classify_and_route "$label" "$out"
}

# Gates, in the exact order (and with the timeout budgets) they had in settings.json.
# iron-law FIRST: it's the only blocker (exit 2 + asyncRewake), so a violation
# short-circuits here and never runs the advisory gates.
run_gate iron-law-check      8 "$H/iron-law-check.sh"
run_gate retro-prompt        5 "$H/retro-prompt.sh"
run_gate scope-calibration   5 "$H/scope-calibration.sh"
run_gate status-stale-nudge  8 "$H/clanker-dist/status-stale-nudge.sh"

# ── merged advisory emit (mirrors pretooluse-bash-dispatch.sh) ──────────────────
if (( ${#CTX[@]} > 0 || ${#SYS[@]} > 0 )); then
  ctx_merged=""; sys_merged=""
  (( ${#CTX[@]} > 0 )) && printf -v ctx_merged '%s\n\n' "${CTX[@]}"
  (( ${#SYS[@]} > 0 )) && printf -v sys_merged '%s\n' "${SYS[@]}"
  jq -cn --arg ctx "${ctx_merged%$'\n\n'}" --arg sys "${sys_merged%$'\n'}" '
      {}
      | (if $sys != "" then .systemMessage = $sys else . end)
      | (if $ctx != "" then .hookSpecificOutput = {hookEventName:"Stop", additionalContext:$ctx} else . end)'
fi
exit 0
