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
set -uo pipefail

PY="$(dirname "$0")/context-gauge.py"

if [ "${1:-}" = "--selftest" ]; then
  exec bash "$(dirname "$0")/tests/test_context_gauge.sh"
fi

INPUT=$(cat)

run_py() { printf '%s' "$INPUT" | CCG_KEY="${1:-}" CCG_SIZE="${2:-}" python3 "$PY"; exit 0; }

# ── pure-bash field extraction (payload is single-line JSON) ────────────────
tp=""; agent=""
case "$INPUT" in
  *'"transcript_path":"'*) tp=${INPUT#*\"transcript_path\":\"}; tp=${tp%%\"*} ;;
esac
case "$INPUT" in
  *'"agent_id":"'*) agent=${INPUT#*\"agent_id\":\"}; agent=${agent%%\"*} ;;
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
