#!/bin/bash
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# closure-claim-verifier — PostToolUse Bash hook.
#
# Iron Law clause 3 (build-loop SKILL §"Quality Iron Law"):
#   "Integration test exists and is green for the cross-component flow
#    the wave introduced. Component-level unit tests are necessary but
#    not sufficient."
#
# This hook fires after every Bash invocation. When the invocation is
# a `git commit`, it scans the just-made commit for closure-claim
# language ("closes D", "fixes D", "shipped", "completed", "✅",
# "all green", "verified") and verifies the SAME COMMIT carries
# integration-test evidence — either a test file path matching
# integration|e2e|end_to_end|p\d+_p\d+ in the diff, OR explicit
# phrases ("integration test", "end-to-end", "cross-component",
# "regression-safety verified") in the commit body.
#
# Closure-claim WITHOUT evidence emits a non-blocking systemMessage
# warning so the model sees it on the next turn. Does NOT block the
# commit (already happened).
#
# Filed 2026-04-28 in response to operator pushback on shortcut
# pattern (D54/D55/D56/D51 marked closed via narrow patches; P1+P2+P3
# shipped with only unit tests). See:
#   ~/.claude/projects/<namespace>/memory/feedback_no_closure_claim_without_integration_test.md
set -euo pipefail

input=$(cat)

# Extract Bash command. Skip non-Bash invocations (matcher already
# filters but defense-in-depth).
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || echo "")
[ -z "$cmd" ] && exit 0

# Skip non-git-commit invocations. The settings.json `if` filter
# already does this but a defense-in-depth check costs nothing.
if ! printf '%s' "$cmd" | grep -qE '\bgit commit\b'; then
    exit 0
fi

# Locate repo root. CLAUDE_PROJECT_DIR if set; otherwise PWD.
repo_dir="${CLAUDE_PROJECT_DIR:-$PWD}"
cd "$repo_dir" 2>/dev/null || exit 0

# git log/diff are no-ops if we're not in a git repo or the commit
# already failed (HEAD unchanged) — exit cleanly without false alarms.
body=$(git log -1 --format='%B' 2>/dev/null || echo "")
[ -z "$body" ] && exit 0

# Names of files changed in the just-made commit (HEAD~1..HEAD). On
# the FIRST commit of a repo there is no HEAD~1; fall back to listing
# all files in HEAD.
if git rev-parse HEAD~1 >/dev/null 2>&1; then
    diff_names=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || echo "")
else
    diff_names=$(git ls-tree --name-only -r HEAD 2>/dev/null || echo "")
fi

# Closure-claim language detection. Case-insensitive. Matches:
# - "closes D<n>" / "fixes D<n>" — D-entry closure language
# - "✅" emoji — used in DEBT_REGISTER closed-row headings
# - "all green" — common closure phrasing in commit bodies
# - "verified: N passed" — quality-claim phrasing
# - "shipped" / "completed" / "done" — closure verbs
closure_pattern='close[ds]? D[0-9]+|fix(e[ds])? D[0-9]+|✅|all green|verified:.*passed|verified independently|^Verified: .*passed|\bshipped\b|\bcompleted\b'
if ! printf '%s' "$body" | grep -qiE "$closure_pattern"; then
    exit 0
fi

# Integration-test evidence detection. Either:
# - File path in the diff matches integration/e2e/end_to_end/p\d+_p\d+
# - Body contains explicit phrases acknowledging the integration test
evidence=0
if printf '%s' "$diff_names" | grep -qiE '(integration|e2e|end_to_end|test_p[0-9]+_p[0-9]+|test_.*_byte_equal|cross_validation)'; then
    evidence=1
fi
# Explicit-phrase forms PLUS the constructionmanagement iron-law token forms (widened 2026-07-13
# after two false positives in two days: bold-markdown evidence 2026-07-12, then "FULL: all green
# in 460s (integration 555/104, e2e 137 passed 3.4m)" 2026-07-13 — FULL by definition runs the
# integration + e2e stages, so citing its token IS citing integration evidence).
if printf '%s' "$body" | grep -qiE '(integration test|end-to-end test|cross-component|regression-safety|cross-validation|byte-equality|byte-equal|FULL: all green|integration [0-9]+( passed|/[0-9]+)|e2e [0-9]+ passed|[0-9]+ passed \([0-9.]+m\))'; then
    evidence=1
fi

if [ "$evidence" -eq 1 ]; then
    exit 0
fi

# Closure claim WITHOUT evidence — emit systemMessage warning.
# Output is JSON; the harness surfaces systemMessage to the user +
# additionalContext to the model.
short_sha=$(git log -1 --format='%h' 2>/dev/null || echo "?")
warning="[closure-claim-verifier] Commit ${short_sha} contains closure-claim language but neither the diff (file paths) nor the body (explicit phrases) shows integration-test evidence. Per Iron Law clause 3, cross-component closure claims require integration tests, not just unit tests. Review whether the closure is justified — if it is, add integration-test evidence to the diff or amend the body to cite it."

# Use jq to construct valid JSON regardless of special chars in $warning.
jq -n --arg msg "$warning" '{
  "systemMessage": $msg,
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": $msg
  }
}'
