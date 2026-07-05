#!/usr/bin/env bash
# UserPromptSubmit hook: check if prompt looks like a complex task
# Only advises — never blocks

set -euo pipefail

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.user_prompt // empty')

# Skip short prompts (commands, yes/no, etc.)
[ ${#PROMPT} -lt 50 ] && exit 0

# Skip if no project context
PROJECT="${CLANKER_PROJECT:-}"
[ -z "$PROJECT" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Run decomposition check
RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/../lib')
from decompose import analyze_prompt
r = analyze_prompt('''$PROMPT''', '$PROJECT')
if r and r['risk_level'] == 'high':
    signals = '; '.join(r['signals'][:3])
    print(f'DECOMPOSITION ADVISORY: {signals}. Consider breaking into smaller tasks.')
" 2>/dev/null || true)

if [ -n "$RESULT" ]; then
    # Output as advisory context (doesn't block)
    ESCAPED=$(printf '%s' "$RESULT" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" 2>/dev/null | sed 's/^"//;s/"$//')
    echo "{\"hookSpecificOutput\":{\"hookEventName\":\"UserPromptSubmit\",\"additionalContext\":\"$ESCAPED\"}}"
fi

exit 0
