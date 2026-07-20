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
      *RETRO_CTX*SCOPE_CTX*) ;;   # fixed order: retro before scope (deterministic)
      *) fails="$fails merge-order-or-token" ;;
    esac
  else
    fails="$fails merge-not-one-json"
  fi

  # (4) the three advisory gates run CONCURRENTLY: each sleeps ~1s then emits its
  #     context. Sequential would be >=3s; concurrent finishes near 1s. Assert the
  #     wall time is well under sequential AND all three merge in the fixed order.
  silent "$HK/iron-law-check.sh"
  for g in retro-prompt scope-calibration; do
    cat > "$HK/$g.sh" <<SH
#!/usr/bin/env bash
sleep 1
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"${g}_CTX"}}'
exit 0
SH
  done
  cat > "$HK/clanker-dist/status-stale-nudge.sh" <<'SH'
#!/usr/bin/env bash
sleep 1
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"STATUS_CTX"}}'
exit 0
SH
  t0=$(date +%s%N)
  out4=$(printf '%s' "$STDIN" | HOME="$T/home" bash "$SELF" 2>/dev/null); rc4=$?
  t1=$(date +%s%N)
  ms=$(( (t1 - t0) / 1000000 ))
  [ "$rc4" -eq 0 ] || fails="$fails concurrent-rc($rc4)"
  ac4=$(printf '%s' "$out4" | jq -r '.hookSpecificOutput.additionalContext' 2>/dev/null)
  case "$ac4" in
    *retro-prompt_CTX*scope-calibration_CTX*STATUS_CTX*) ;;
    *) fails="$fails concurrent-merge-order" ;;
  esac
  # Concurrent ~1s; sequential floor is 3s. Assert < 2500ms (generous headroom).
  [ "$ms" -lt 2500 ] || fails="$fails concurrent-not-parallel(${ms}ms)"

  if [ -z "$fails" ]; then
    echo "stop-dispatch selftest: claim-free->0, iron-law RED->2 (msg, downstream skipped), 2 advisories merged (fixed order), 3 advisories concurrent in ${ms}ms (<3s sequential) — PASS"
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

# Post-run routing, shared by the synchronous iron-law gate and the concurrent
# advisory gates: honor a blocker (exit 2 — first wins), fail-open on timeout /
# error, else route the advisory stdout into the merge arrays.
route_gate_result() {
  local label="$1" budget="$2" rc="$3" out="$4" err="$5"
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

# Run one gate on the shared stdin with its own timeout budget, SYNCHRONOUSLY —
# used for iron-law, the blocker that must gate everything downstream.
run_gate() {
  local label="$1" budget="$2" path="$3"
  [ -f "$path" ] || return 0   # gate not installed on this box — skip silently
  local out rc err=""
  out=$(printf '%s' "$INPUT" | timeout "$budget" bash "$path" 2>"$ERRF"); rc=$?
  [ -s "$ERRF" ] && err=$(<"$ERRF")
  : > "$ERRF" 2>/dev/null || true
  route_gate_result "$label" "$budget" "$rc" "$out" "$err"
}

# Concurrent advisory gate: capture stdout/stderr/rc into its OWN slot files (the
# single shared ERRF can't be reused across parallel gates). The parent merges
# the slots in a fixed order after `wait`, so classify_and_route runs in the main
# shell where CTX/SYS must accumulate. A missing gate leaves no .rc (skipped).
run_advisory() {
  local budget="$1" path="$2" slot="$3"
  [ -f "$path" ] || return 0
  local out rc
  out=$(printf '%s' "$INPUT" | timeout "$budget" bash "$path" 2>"$slot.err"); rc=$?
  printf '%s' "$out" > "$slot.out"
  printf '%s' "$rc"  > "$slot.rc"
}

# iron-law FIRST, synchronous + blocking: it's the only blocker (exit 2 +
# asyncRewake), so a violation short-circuits here and no advisory gate runs.
run_gate iron-law-check 8 "$H/iron-law-check.sh"

# The three advisory gates are independent (each re-reads the same stdin and
# only emits additionalContext/systemMessage), so run them CONCURRENTLY, then
# merge in the FIXED order retro,scope,status — the envelope is deterministic
# regardless of finish order. Per-gate timeout budgets are unchanged (5/5/8).
JOBD=$(mktemp -d "${TMPDIR:-/tmp}/stopd-jobs.XXXXXX" 2>/dev/null) || JOBD=""
if [ -n "$JOBD" ]; then
  trap 'rm -f "$ERRF" 2>/dev/null; rm -rf "$JOBD" 2>/dev/null' EXIT
  run_advisory 5 "$H/retro-prompt.sh"                    "$JOBD/retro"  &
  run_advisory 5 "$H/scope-calibration.sh"               "$JOBD/scope"  &
  run_advisory 8 "$H/clanker-dist/status-stale-nudge.sh" "$JOBD/status" &
  wait
  for entry in "retro:retro-prompt:5" "scope:scope-calibration:5" \
               "status:status-stale-nudge:8"; do
    IFS=: read -r slot label budget <<<"$entry"
    [ -f "$JOBD/$slot.rc" ] || continue        # gate not installed — skip silently
    grc=$(<"$JOBD/$slot.rc"); gout=""; gerr=""
    [ -f "$JOBD/$slot.out" ] && gout=$(<"$JOBD/$slot.out")
    [ -s "$JOBD/$slot.err" ] && gerr=$(<"$JOBD/$slot.err")
    route_gate_result "$label" "$budget" "$grc" "$gout" "$gerr"
  done
else
  # Degraded FS (no temp dir): fall back to sequential runs, same order/budgets.
  run_gate retro-prompt        5 "$H/retro-prompt.sh"
  run_gate scope-calibration   5 "$H/scope-calibration.sh"
  run_gate status-stale-nudge  8 "$H/clanker-dist/status-stale-nudge.sh"
fi

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
