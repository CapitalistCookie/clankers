#!/bin/bash
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# context-gauge fast-path (2026-07-05). PostToolUse(*) fires on EVERY tool call;
# the python gauge costs ~28ms/call even when silent (the overwhelming majority
# of calls). This wrapper decides in pure bash (ZERO extra spawns on the hot
# path beyond one `stat`) whether the gauge could possibly need to speak, and
# only then spawns python. ALL measurement/emission logic stays in
# context-gauge.py — the wrapper only ever errs toward RUNNING it (fail-open =
# fail-toward-python; a skipped gauge that should have spoken is the only
# dangerous failure, so every uncertain branch falls through to python).
#
# Skip is allowed ONLY when ALL hold:
#   - python cached "size window pct" for this transcript on a previous run
#   - transcript grew monotonically (no rotation/replacement)
#   - grounding marker exists (first-call grounding already delivered)
#   - projected remaining % (cached pct minus a DELIBERATELY pessimistic
#     2 bytes/token growth estimate) stays above 67% — comfortably clear of
#     the gauge's 65% first-speak threshold
#   - growth since the cached run is < 4 MB (sanity bound)
#
# Selftest: bash context-gauge.sh --selftest   (covers wrapper AND python).
# Fail-visible (2026-09-24): a python run that exits nonzero lands in the
# hook-error log (block below); the python logs its own guarded failures.
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

if [ "${1:-}" = "--selftest" ]; then
  # The selftest drives interactive-run cases: a nested caller's env must not
  # silence them.
  unset CLAUDE_CODE_ENTRYPOINT CLANKER_INJECT_NESTED
  exec bash "$(dirname "$0")/tests/test_context_gauge.sh"
fi

# Nested runs (2026-09-24): every scripted `claude -p` carries
# CLAUDE_CODE_ENTRYPOINT=sdk-cli, and hooks inherit it. A one-shot run has no
# use for a context reading, so the gauge exits before it reads stdin: no cat,
# no stat, no python, no grounding line. CLANKER_INJECT_NESTED=1 keeps the
# gauge on, as it keeps the start brief on.
if [ "${CLAUDE_CODE_ENTRYPOINT:-}" = sdk-cli ] && [ -z "${CLANKER_INJECT_NESTED:-}" ]; then
  exit 0
fi

PY="$(dirname "$0")/context-gauge.py"
INPUT=$(cat)

# The gauge's stdout goes to the hook's stdout; its stderr is captured and, on
# a nonzero exit, logged.
run_py() {
  local err rc
  HOOK_ERR_IN="$INPUT"
  { err=$(printf '%s' "$INPUT" | CCG_KEY="${1:-}" CCG_SIZE="${2:-}" python3 "$PY" 2>&1 1>&3 3>&-); rc=$?; } 3>&1
  [ "$rc" -eq 0 ] || hook_err "$rc" "context-gauge.py" "$err"
  exit 0
}

# ── pure-bash field extraction (payload is single-line JSON) ────────────────
tp=""; agent=""
case "$INPUT" in
  *'"transcript_path":"'*) tp=${INPUT#*\"transcript_path\":\"}; tp=${tp%%\"*} ;;
esac
# agent_id must be the TOP-LEVEL field (2026-09-24). The old bare substring
# match also caught NESTED ones (an Agent tool_response naming the spawned
# teammate), minting a fresh key per spawn: the parent re-received its "first
# reading" on every Agent call (19 repeats in one coordinator session). jq runs
# only when the substring is present, so the common path stays spawn-free.
case "$INPUT" in
  *'"agent_id"'*) agent=$(printf '%s' "$INPUT" | jq -r 'if type == "object" then (.agent_id // empty | tostring) else empty end' 2>/dev/null) || agent="" ;;
esac

# Subagent payloads: resolution/grounding logic lives in python — always run it
# on the first sighting; afterwards python's cache (keyed by agent) enables skips.
if [ -n "$agent" ]; then
  key="ag-${agent//[^a-zA-Z0-9_-]/_}"
else
  [ -n "$tp" ] && [ -f "$tp" ] || exit 0   # nothing measurable → gauge is silent anyway
  key="tp-$(basename "$tp" .jsonl)"
fi

cache="/tmp/cc-ctxgauge-fast-${key}"
grounded="/tmp/cc-ctxgauge-grounded-${key}"

# grounding not yet delivered → python (it must speak once per transcript)
[ -f "$grounded" ] || run_py "$key"
[ -f "$cache" ] || run_py "$key"

# cache: "size window pct" (integers; pct = floor of remaining %)
read -r c_size c_window c_pct < "$cache" 2>/dev/null || run_py "$key"
case "$c_size$c_window$c_pct" in *[!0-9]*|"") run_py "$key" ;; esac

# current size: for subagents python cached the resolved path on line 2
m_tp="$tp"
if [ -n "$agent" ]; then
  m_tp=$(sed -n 2p "$cache" 2>/dev/null)
  [ -n "$m_tp" ] && [ -f "$m_tp" ] || run_py "$key"
fi
size=$(stat -c%s "$m_tp" 2>/dev/null) || run_py "$key"
case "$size" in *[!0-9]*|"") run_py "$key" ;; esac

# rotation/shrink/no-growth-info → python
[ "$size" -ge "$c_size" ] || run_py "$key"
delta=$((size - c_size))
[ "$delta" -lt 4194304 ] || run_py "$key"

# pessimistic projection: 1 token per 2 bytes of growth (real ratio is ~4-5
# bytes/token, so this over-shrinks remaining% → skips are conservative)
proj=$((c_pct - (delta * 50 / c_window)))
if [ "$proj" -gt 67 ]; then
  exit 0   # gauge provably silent — skip the python spawn
fi
run_py "$key" "$size"
