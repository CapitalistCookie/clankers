#!/bin/bash
# Two-layer selftest for the context gauge: context-gauge.sh (fast-path skip
# logic) + context-gauge.py (measurement/emission). RED/GREEN per behavior.
# Run: bash context-gauge.sh --selftest   (or this file directly)
set -uo pipefail

H="$(cd "$(dirname "$0")/.." && pwd)"
W="$H/context-gauge.sh"
T=$(mktemp -d)
PROBE="$T/py-ran"
fails=0
purge_keys() { rm -f /tmp/cc-ctxgauge-fast-tp-ccgtest* /tmp/cc-ctxgauge-fast-ag-ccgtagent* /tmp/cc-ctxgauge-grounded-tp-ccgtest* /tmp/cc-ctxgauge-grounded-ag-ccgtagent* /tmp/cc-ctxgauge-tp-*ccgtest* /tmp/cc-ctxgauge-ccgtests*.last /tmp/cc-ctxgauge-ag-ccgtagent*; }
cleanup() { rm -rf "$T"; purge_keys; }
trap cleanup EXIT
purge_keys

usage_line() { # $1 tokens-used
  printf '{"type":"assistant","message":{"role":"assistant","model":"claude-fable-5[1m]","usage":{"input_tokens":100,"cache_read_input_tokens":%d,"output_tokens":0}},"timestamp":"2026-07-05T20:00:00Z"}\n' "$(( $1 - 100 ))"
}
run() { # $1 payload -> stdout; probe reset first
  rm -f "$PROBE"
  printf '%s' "$1" | CCG_PROBE="$PROBE" bash "$W" 2>/dev/null
}
chk() { [ "$1" = ok ] || { echo "FAIL $2"; fails=1; }; }

# ── A: grounding fires once, cache+marker written, python ran ────────────────
TPA="$T/ccgtestA.jsonl"; usage_line 200000 > "$TPA"          # 80% free of 1M
PAYA=$(printf '{"transcript_path":"%s","session_id":"ccgtests"}' "$TPA")
outA=$(run "$PAYA")
echo "$outA" | grep -q 'first reading' && echo "$outA" | grep -q '~80%' && [ -f "$PROBE" ] && st=ok || st=bad
chk $st "A grounding (~80%, python ran): got: ${outA:0:90}"
CACHE=/tmp/cc-ctxgauge-fast-tp-ccgtestA
read -r csz cwin cpct < "$CACHE" 2>/dev/null || true
[ "${cwin:-}" = 1000000 ] && [ "${cpct:-}" = 80 ] && st=ok || st=bad
chk $st "A cache written (size window pct): got '${csz:-} ${cwin:-} ${cpct:-}'"

# ── B: no growth + high remaining -> wrapper SKIPS python, silent ────────────
outB=$(run "$PAYA")
[ -z "$outB" ] && [ ! -f "$PROBE" ] && st=ok || st=bad
chk $st "B skip path (silent, python NOT spawned)"

# ── C: 300KB growth projects <=67 -> python re-runs (still silent at 80%) ────
python3 -c "open('$TPA','a').write(('{\"type\":\"noise\",\"x\":\"'+'z'*280+'\"}\n')*1200)"
usage_line 210000 >> "$TPA"   # every real assistant turn logs fresh usage
outC=$(run "$PAYA")
[ -z "$outC" ] && [ -f "$PROBE" ] && st=ok || st=bad
chk $st "C growth forces python (probe present), still silent at 80%"
read -r csz2 _ _ < "$CACHE"
[ "$csz2" -gt "${csz:-0}" ] && st=ok || st=bad
chk $st "C cache size advanced ($csz -> $csz2)"

# ── D: near-threshold transcript: grounding, then bucket-speak, then throttle;
#       wrapper must NEVER skip below the 67% guard ───────────────────────────
TPD="$T/ccgtestD.jsonl"; usage_line 680000 > "$TPD"          # 32% free
PAYD=$(printf '{"transcript_path":"%s","session_id":"ccgtests2"}' "$TPD")
outD1=$(run "$PAYD"); echo "$outD1" | grep -q 'first reading' && echo "$outD1" | grep -q '~32%' && st=ok || st=bad
chk $st "D1 grounding at 32%"
outD2=$(run "$PAYD"); echo "$outD2" | grep -q 'REMAINING' && [ -f "$PROBE" ] && st=ok || st=bad
chk $st "D2 bucket message (python ran — no skip in the hot zone)"
outD3=$(run "$PAYD"); [ -z "$outD3" ] && [ -f "$PROBE" ] && st=ok || st=bad
chk $st "D3 throttled silence but python STILL consulted at 32%"

# ── E: compact boundary newer than usage -> stale note; cache NOT refreshed ──
before=$(cat /tmp/cc-ctxgauge-fast-tp-ccgtestD)
printf '{"isCompactSummary":true}\n' >> "$TPD"
outE=$(run "$PAYD"); echo "$outE" | grep -qi 'compact just occurred' && st=ok || st=bad
chk $st "E post-compact stale note"
after=$(cat /tmp/cc-ctxgauge-fast-tp-ccgtestD)
[ "$before" = "$after" ] && st=ok || st=bad
chk $st "E stale run does not refresh the cache"

# ── F: malformed stdin -> silent, no python ──────────────────────────────────
outF=$(run 'not-json-at-all')
[ -z "$outF" ] && [ ! -f "$PROBE" ] && st=ok || st=bad
chk $st "F malformed stdin: silent, python not spawned"

# ── G: subagent — resolve sidecar transcript; then skip on 2nd call ──────────
mkdir -p "$T/proj/ccgtestP/subagents"
TPP="$T/proj/ccgtestP.jsonl"; usage_line 200000 > "$TPP"     # parent (irrelevant)
SUB="$T/proj/ccgtestP/subagents/agent-accgtagentx-99.jsonl"; usage_line 100000 > "$SUB"  # 90% free
PAYG=$(printf '{"transcript_path":"%s","session_id":"p","agent_id":"ccgtagentx"}' "$TPP")
outG1=$(run "$PAYG"); echo "$outG1" | grep -q 'first reading' && echo "$outG1" | grep -q '~90%' && st=ok || st=bad
chk $st "G1 subagent resolved to sidecar transcript (~90%): got: ${outG1:0:90}"
line2=$(sed -n 2p /tmp/cc-ctxgauge-fast-ag-ccgtagentx 2>/dev/null)
[ "$line2" = "$SUB" ] && st=ok || st=bad
chk $st "G1 cache line2 = resolved subagent path"
outG2=$(run "$PAYG"); [ -z "$outG2" ] && [ ! -f "$PROBE" ] && st=ok || st=bad
chk $st "G2 subagent skip path (silent, python not spawned)"

# ── H: first-reading text stays <= 200 chars (2026-09-24 shortening) ────────
lenA=$(printf '%s' "$outA" | jq -r '.hookSpecificOutput.additionalContext' 2>/dev/null | tr -d '\n' | wc -m)
[ "${lenA:-999}" -le 200 ] && st=ok || st=bad
chk $st "H first-reading text <= 200 chars (got $lenA)"

# ── I: a NESTED agent_id (Agent tool_response naming a spawned teammate) must
#       not mint a new key: the grounded parent stays silent (2026-09-24 bug) ──
TPI="$T/ccgtestI.jsonl"; usage_line 200000 > "$TPI"
PAYI=$(printf '{"transcript_path":"%s","session_id":"ccgtests"}' "$TPI")
run "$PAYI" >/dev/null
PAYI2=$(printf '{"transcript_path":"%s","session_id":"ccgtests","tool_name":"Agent","tool_response":{"status":"teammate_spawned","agent_id":"ccgtagent-nested@s1"}}' "$TPI")
outI=$(run "$PAYI2")
! echo "$outI" | grep -q 'first reading' && [ ! -e /tmp/cc-ctxgauge-grounded-ag-ccgtagent-nested_s1 ] && st=ok || st=bad
chk $st "I nested agent_id ignored (no re-grounding, no stray key): got: ${outI:0:60}"

# ── J: parallel first calls (a batch of parallel tool calls) ground ONCE ─────
TPJ="$T/ccgtestJ.jsonl"; usage_line 200000 > "$TPJ"
PAYJ=$(printf '{"transcript_path":"%s","session_id":"ccgtests"}' "$TPJ")
for i in $(seq 1 12); do ( printf '%s' "$PAYJ" | bash "$W" > "$T/j.$i" 2>/dev/null ) & done; wait
nJ=$(grep -l 'first reading' "$T"/j.* 2>/dev/null | wc -l)
[ "$nJ" -eq 1 ] && st=ok || st=bad
chk $st "J 12 parallel first calls -> exactly 1 grounding (got $nJ)"

if [ "$fails" -eq 0 ]; then
  echo "context-gauge selftest: 16/16 PASS"
  exit 0
fi
echo "context-gauge selftest: FAILURES"
exit 1
