#!/bin/bash
# Hook-tax timing: mean wall-ms of session-start.sh over N runs.
# CLANKER_DATA -> tmp store (stub rows never touch live); registry real (read).
HOOK="${1:?usage: hooktax.sh <hook-path> [runs]}"
RUNS="${2:-5}"
TDATA=$(mktemp -d)
mkdir -p "$TDATA/raw/sessions" "$TDATA/alerts"
total=0
for i in $(seq 1 "$RUNS"); do
  start=$(date +%s%N)
  echo "{\"session_id\":\"perf-$i\",\"cwd\":\"$HOME/projects/clanker\",\"source\":\"startup\"}" \
    | CLANKER_DATA="$TDATA" bash "$HOOK" > /dev/null 2>&1
  end=$(date +%s%N)
  ms=$(( (end - start) / 1000000 ))
  total=$(( total + ms ))
  echo "run $i: ${ms}ms"
done
echo "mean: $(( total / RUNS ))ms over $RUNS runs"
rm -rf "$TDATA"
