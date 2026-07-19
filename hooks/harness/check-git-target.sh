#!/bin/sh
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# Block git push if research files are in unpushed commits of non-research repos

cmd=$(jq -r '.tool_input.command // empty' 2>/dev/null || true)
[ -z "$cmd" ] && exit 0

# Only check git push commands (early-exit BEFORE sourcing config)
echo "$cmd" | grep -qE 'git\s+push' || exit 0

[ -f ~/.claude/research.env ] && . ~/.claude/research.env
RESEARCH_REPO="${RESEARCH_REPO:-}"

# CHECK_GIT_TARGET_REPOS: space-separated list of local repos to guard, from env
# or research.env. De-personalized default is EMPTY: the distributed copy is inert
# until an operator names the repos to guard (no baked-in machine checkout paths).
REPOS="${CHECK_GIT_TARGET_REPOS:-}"
[ -z "$REPOS" ] && exit 0

# Allow pushes that already target the configured research repo itself.
[ -n "$RESEARCH_REPO" ] && echo "$cmd" | grep -qF "$RESEARCH_REPO" && exit 0

for repo in $REPOS; do
    [ -d "$repo/.git" ] || continue
    if git -C "$repo" diff --name-only origin/main..HEAD 2>/dev/null | grep -qE '^research/|^docs/papers/'; then
        # permissionDecision form — legacy top-level {"decision":"block"} is no
        # longer in the documented PreToolUse contract (2.1.x).
        jq -cn --arg r "BLOCKED: $repo has unpushed commits with research files. Push to ${RESEARCH_REPO:-the research repo}, NOT this repo." \
            '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
        exit 0
    fi
done
