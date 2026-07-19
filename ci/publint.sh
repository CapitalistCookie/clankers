#!/usr/bin/env bash
# publint — public-readiness lint: no operator-specific paths/hosts/identities
# in CODE files (docs are exempt; they may legitimately narrate this install).
# Wired into ci/full.sh next to gitleaks (the queued "public-push gate",
# STATUS 2026-07-05; built 2026-07-19). Born green: all hits fixed first.
#
# Patterns are assembled from fragments so this file never matches itself.
set -uo pipefail
cd "$(dirname "$0")/.."

HOME_LIT='/home/'
HOME_LIT+='user'                 # hardcoded operator home
SLUG_LIT='-home-'
SLUG_LIT+='user'                 # hardcoded Claude Code namespace slug
LAN_RE='192\.168\.[0-9]'         # LAN addressing

PATTERN="${HOME_LIT}|${SLUG_LIT}|${LAN_RE}"

# Operator/machine identities live OUTSIDE the repo (they'd otherwise ship in
# this very file when published): one regex fragment per line, optional.
IDS_FILE="$HOME/.claude/publint-ids.txt"
if [ -f "$IDS_FILE" ]; then
  ID_RE=$(grep -v '^\s*#' "$IDS_FILE" | grep -v '^\s*$' | paste -sd'|' -)
  [ -n "$ID_RE" ] && PATTERN="${PATTERN}|${ID_RE}"
fi

# Tracked code files only; this lint and test fixtures excluded.
HITS=$(git ls-files '*.py' '*.sh' '*.js' \
  | grep -v '^ci/publint\.sh$' \
  | xargs grep -lE "$PATTERN" 2>/dev/null || true)

if [ -n "$HITS" ]; then
  echo "[publint] OPERATOR-SPECIFIC references in code files (parameterize via \$HOME / env / config):"
  for f in $HITS; do
    echo "  $f:"
    grep -nE "$PATTERN" "$f" | head -3 | sed 's/^/    /'
  done
  exit 1
fi
echo "[publint] clean — no operator paths/hosts/identities in code files"
exit 0
