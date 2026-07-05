#!/usr/bin/env bash
# Stop hook (GLOBAL, non-blocking) — harness overhaul M4 (2026-07-05).
# The STATUS.md contract: repo state is updated AS PART of finishing work, so any
# fresh session cold-starts from the repo alone. This nudges (never blocks) when a
# session stops with a dirty repo whose STATUS.md is stale relative to the work.
set -u

INPUT=$(cat 2>/dev/null || true)
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
[ -n "$CWD" ] || CWD="$PWD"
[ -d "$CWD/.git" ] || exit 0
[ -f "$CWD/STATUS.md" ] || exit 0

# dirty tree?
DIRTY=$(git -C "$CWD" status --porcelain 2>/dev/null | head -1)
[ -n "$DIRTY" ] || exit 0

# STATUS.md older than the newest modified tracked file by >2h?
# cut -c4- (not awk $2): porcelain paths start at col 4; awk split filenames
# containing spaces and silently dropped them from the freshness scan.
NEWEST=$(git -C "$CWD" status --porcelain 2>/dev/null | cut -c4- | head -20 | while IFS= read -r f; do
    f="${f#\"}"; f="${f%\"}"; f="${f##* -> }"
    [ -f "$CWD/$f" ] && stat -c %Y "$CWD/$f" 2>/dev/null; done | sort -rn | head -1)
STATUS_MT=$(stat -c %Y "$CWD/STATUS.md" 2>/dev/null || echo 0)
[ -n "$NEWEST" ] || exit 0
AGE=$(( NEWEST - STATUS_MT ))
[ "$AGE" -gt 7200 ] || exit 0

printf '{"systemMessage": "STATUS.md is stale (working tree changed %sh after its last update). Update STATUS.md (NOW/NEXT/BLOCKED/DECISIONS) before wrapping up — it is the next session'\''s cold-start."}\n' "$(( AGE / 3600 ))"
exit 0
