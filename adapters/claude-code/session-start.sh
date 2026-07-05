#!/usr/bin/env bash
set -euo pipefail

CLANKER_DATA="${CLANKER_DATA:-/data/clanker}"
CLANKER_REGISTRY="${CLANKER_REGISTRY:-/home/user/projects/.clanker.yaml}"

ALERTS_DIR="$CLANKER_DATA/alerts"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Read hook input from stdin
INPUT=$(cat)
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$CWD}"

# --- 0. On /clear, extract metrics from the PREVIOUS session ---
SOURCE=$(echo "$INPUT" | jq -r '.source // empty')

if [ "$SOURCE" = "clear" ]; then
    # /clear was invoked — the session_id and transcript_path are for the NEW session
    # The OLD session's transcript is the most recently modified .jsonl in this project dir
    PROJECT_DIR_FOR_CLEAR="${CLAUDE_PROJECT_DIR:-$CWD}"
    CLAUDE_PROJ_DIR=$(echo "$PROJECT_DIR_FOR_CLEAR" | sed 's|/|-|g')
    OLD_TRANSCRIPT=$(ls -t "$HOME/.claude/projects/$CLAUDE_PROJ_DIR/"*.jsonl 2>/dev/null | head -2 | tail -1)

    if [ -n "$OLD_TRANSCRIPT" ] && [ -f "$OLD_TRANSCRIPT" ]; then
        OLD_SESSION_ID=$(basename "$OLD_TRANSCRIPT" .jsonl)
        export TRANSCRIPT="$OLD_TRANSCRIPT" SESSION_ID="$OLD_SESSION_ID" CWD="$CWD"
        bash "$(dirname "${BASH_SOURCE[0]}")/session-end.sh" < <(
            echo "{\"session_id\":\"$OLD_SESSION_ID\",\"transcript_path\":\"$OLD_TRANSCRIPT\",\"cwd\":\"$CWD\"}"
        ) 2>/dev/null &
    fi
fi

# --- 1. Determine project and archetype ---
PROJECT=""
ARCHETYPE=""
if [ -n "$PROJECT_DIR" ] && [ -f "$CLANKER_REGISTRY" ]; then
    PROJECT=$(basename "$PROJECT_DIR")
    ARCHETYPE=$(python3 -c "
import yaml, sys
try:
    with open('$CLANKER_REGISTRY') as f:
        reg = yaml.safe_load(f)
    p = reg.get('projects', {}).get('$PROJECT', {})
    print(p.get('archetype', 'unknown'))
except:
    print('unknown')
" 2>/dev/null)
fi

# --- 2. Set env vars for downstream hooks ---
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    [ -n "$PROJECT" ] && echo "export CLANKER_PROJECT=$PROJECT" >> "$CLAUDE_ENV_FILE"
    [ -n "$ARCHETYPE" ] && echo "export CLANKER_ARCHETYPE=$ARCHETYPE" >> "$CLAUDE_ENV_FILE"
fi

# --- 3. Check for active alerts ---
ALERT_MSG=""
if [ -d "$ALERTS_DIR" ]; then
    ALERT_COUNT=$(find "$ALERTS_DIR" -name "*.json" -type f 2>/dev/null | wc -l)
    if [ "$ALERT_COUNT" -gt 0 ]; then
        ALERT_MSG="CLANKER ALERTS ($ALERT_COUNT active):\\n"
        for alert_file in "$ALERTS_DIR"/*.json; do
            [ -f "$alert_file" ] || continue
            severity=$(jq -r '.severity // "info"' "$alert_file" 2>/dev/null)
            message=$(jq -r '.message // "unknown alert"' "$alert_file" 2>/dev/null)
            ALERT_MSG="${ALERT_MSG}  [${severity}] ${message}\\n"
        done
    fi
fi

# --- 4. Generate project briefing ---
BRIEFING=""
if [ -n "$PROJECT" ] && [ "$PROJECT" != "unknown" ] && [ -d "$PROJECT_DIR" ]; then
    BRIEFING=$(python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/../lib')
from briefing import generate_briefing
b = generate_briefing('$PROJECT', '$PROJECT_DIR')
if b:
    print(b)
" 2>/dev/null || true)
fi

# --- 4b. Run codebase indexer if project has one ---
if [ -n "$PROJECT_DIR" ] && [ -d "$PROJECT_DIR" ]; then
    if [ -f "$PROJECT_DIR/docs/codebase-index/generate.py" ]; then
        python3 "$PROJECT_DIR/docs/codebase-index/generate.py" \
            --routing-table "/home/user/.claude/projects/-home-user/memory/CODEBASE_INDEX.md" \
            2>/dev/null &
    fi
    if [ -f "$PROJECT_DIR/docs/codebase-index/generate-sdk-reference.sh" ]; then
        bash "$PROJECT_DIR/docs/codebase-index/generate-sdk-reference.sh" 2>/dev/null &
    fi
fi

# --- 5. Output ---
FULL_MSG=""
[ -n "$ALERT_MSG" ] && FULL_MSG="$ALERT_MSG"
[ -n "$BRIEFING" ] && FULL_MSG="${FULL_MSG:+${FULL_MSG}\\n}$BRIEFING"
if [ -z "$FULL_MSG" ] && [ -n "$PROJECT" ]; then
    FULL_MSG="Clanker: project=$PROJECT archetype=$ARCHETYPE"
fi

if [ -n "$FULL_MSG" ]; then
    ESCAPED=$(printf '%s' "$FULL_MSG" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" 2>/dev/null | sed 's/^"//;s/"$//')
    cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "$ESCAPED"
  }
}
EOF
fi

exit 0
