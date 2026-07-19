#!/bin/sh
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# PreToolUse hook: for RESEARCH commits, remind Claude to invoke
# superpowers:verification-before-completion + disclose deviations.
#
# v2 (2026-04-21 session 18 P3 fix): scope check — skip entirely when all
# staged files are bot-infrastructure or ops docs. Previously the hook
# fired on every polymarket commit because its remote is
# github.com/<org>/eigenstateresearch, producing walk-forward/
# frozen-memory prompts that don't apply to trading-bot code.
#
# Also adds cd-prefix target parsing (same approach as v2.2+ of
# pwb-risk-surface-review-required.sh) so `cd /repo && git commit` is
# evaluated against /repo's index rather than the session CWD's index.

cmd=$(jq -r '.tool_input.command // empty' 2>/dev/null || true)
[ -z "$cmd" ] && exit 0

first_line=$(echo "$cmd" | head -1)
# Detect git-commit invocation (broad pattern accepting absolute paths,
# -c/-C flags, env prefix; same shape as pwb-risk-surface-review v2.3).
echo "$cmd" | grep -qE '(^|[^a-zA-Z0-9])(git-commit|git[[:space:]]+(-[^[:space:]]+([[:space:]]+[^-[:space:]][^[:space:]]*)?[[:space:]]+)*commit)([[:space:]]|$|;|&|\||\))' || exit 0

# Allow amend commits (user explicitly chose this)
echo "$cmd" | grep -qE '\-\-amend' && exit 0

# cd-prefix target: if cmd starts with `cd <path> &&`, switch to that repo
# before checking staged files. Unquoted path only (shared limitation
# with the other hooks).
cd_target=$(echo "$first_line" | sed -nE "s/^[[:space:]]*cd[[:space:]]+([^[:space:]&;|\"']+)[[:space:]]*(&&|;|\\|\\|).*/\\1/p" | head -1)
if [ -n "$cd_target" ] && [ -d "$cd_target" ]; then
    cd "$cd_target" 2>/dev/null || exit 0  # fail-open; this is advisory
fi

# Registry-archetype scope (2026-07-05, replaces per-repo whack-a-mole): research
# discipline applies ONLY to repos registered archetype=research in
# ~/projects/.clanker.yaml. The 4th documented false-fire class (a `docs:` commit
# in clanker) triggered this: the message-prefix filter below never matched
# because msg extraction didn't isolate the -m text. Registered non-research →
# exit 0 here; UNREGISTERED repos fall through to the legacy heuristics.
arch=$(python3 - "$(pwd)" <<'PY' 2>/dev/null
import os, subprocess, sys, yaml
try:
    d = sys.argv[1]
    r = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True, timeout=3)
    in_repo = r.returncode == 0 and r.stdout.strip()
    root = os.path.realpath(r.stdout.strip()) if in_repo else os.path.realpath(d)
    reg = yaml.safe_load(open(os.path.expanduser("~/projects/.clanker.yaml"))) or {}
    for name, v in (reg.get("projects") or {}).items():
        if not isinstance(v, dict):
            continue
        p = v.get("path") or os.path.expanduser(f"~/projects/{name}")
        rp = os.path.realpath(p)
        # exact / cwd-inside-project always count; project-inside-root only when
        # root is a REAL git toplevel (yon-style subprojects) — a non-repo cwd
        # like ~ would otherwise swallow every registered project as first-match.
        if root == rp or root.startswith(rp + os.sep) or (in_repo and rp.startswith(root + os.sep)):
            print(v.get("archetype", "unknown")); break
except Exception:
    pass
PY
)
if [ -n "$arch" ] && [ "$arch" != "research" ]; then
    exit 0
fi

# Extract the actual -m message so the prefix/keyword filters below see the
# MESSAGE, not "git commit -q -m ..." (the extraction bug behind false-fire #4).
m_arg=$(printf '%s' "$cmd" | sed -nE 's/.*-m[[:space:]]+"([^"]+)".*/\1/p' | head -1)
[ -z "$m_arg" ] && m_arg=$(printf '%s' "$cmd" | sed -nE "s/.*-m[[:space:]]+'([^']+)'.*/\1/p" | head -1)

# Cotton-dashboard scope check (added 2026-04-27): cottondashboard
# commits frequently include "research", "walk-forward", "validation"
# tokens because the repo vendors research.ecc.* and runs walk-forward
# crons — but those tokens describe PRODUCT plumbing, not research
# findings. The frozen-memory + leakage prompts don't apply.
#
# Detect via current working directory OR cd-prefix target.
pwd_now=$(pwd)
if echo "$pwd_now" | grep -qE '/projects/cottondashboard(/|$)' || \
   echo "$cd_target" | grep -qE '/projects/cottondashboard(/|$)' || \
   ([ -d ".git" ] && git remote get-url origin 2>/dev/null | grep -qE '/cottondashboard\.git$'); then
    exit 0
fi

# hftbacktester scope check (added 2026-04-27 session-4 v0.1.1 prep): the
# pmframework backtest-framework repo's commits routinely contain "backtest"
# / "validation" / "research" tokens because it BUILDS a backtest framework
# (the verb is "build", not "research"). Frozen-memory + leakage prompts
# don't apply to software-engineering commits in this repo. Skip when the
# session is under /projects/hftbacktester{,-worktrees}/ OR remote is
# <org>/hftbacktester.git.
if echo "$pwd_now" | grep -qE '/projects/hftbacktester(-worktrees)?(/|$)' || \
   echo "$cd_target" | grep -qE '/projects/hftbacktester(-worktrees)?(/|$)' || \
   ([ -d ".git" ] && git remote get-url origin 2>/dev/null | grep -qE '/hftbacktester\.git$'); then
    exit 0
fi

# Scope check: is this a bot/ops commit (not research)?
# Paths that mean NOT research:
#   polymarket_{scalper,weather_bot,core,ops}/
#   docs/OPS_RUNBOOK.md  docs/SESSION*_HANDOFF*.md
#   docs/plans/  docs/reviews/
#   .githooks/  .github/
#   clanker_hooks/  clanker_plugins/
# Anything outside this set (e.g., research/) triggers the research check.
staged=$(git diff --cached --name-only 2>/dev/null)
if [ -n "$staged" ]; then
    bot_or_ops_re='^(polymarket_[a-z_]+/|docs/(OPS_RUNBOOK|SESSION[0-9]+_HANDOFF|plans/|reviews/)|\.githooks/|\.github/|clanker_hooks/|clanker_plugins/)'
    non_bot=$(echo "$staged" | grep -vE "$bot_or_ops_re" || true)
    if [ -z "$non_bot" ]; then
        # All staged files are bot-infrastructure or ops docs. Not a
        # research commit — skip the research-specific verification prompts.
        exit 0
    fi
fi

# Extract the commit-message portion only. Prefer the parsed -m argument;
# fall back to stripping the cd/path prefix so "/polymarket_research" paths
# don't false-match on the "research" keyword below.
if [ -n "$m_arg" ]; then
    msg="$m_arg"
else
    msg=$(echo "$cmd" | sed 's/^.*git commit/git commit/')
fi

# Keyword check on the commit MESSAGE (not the full command).
# Research-trigger check — avoid false positives on infrastructure
# commits that happen to contain "research" in the repo name or
# file paths. Require the commit MESSAGE (not just presence) to
# carry a research-specific signal.
#
# Negative filters: if the message starts with an ops/infra prefix
# (like `ops:`, `writer:`, `merge:`, `r2:`, `daemon:`, `tools:`,
# `systemd:`, `plan:`, `docs:`), skip the research gate — those are
# infrastructure, not research.
first_line=$(echo "$msg" | head -1)
if echo "$first_line" | grep -qE '^(ops|writer|merger?|r2|daemon|tools|systemd|schema|config|monitor|converter|requirements|ws_manager|readiness|plan|docs?|writer\+daemon|daemon\+agent|agent):'; then
    exit 0
fi

# Keyword list (tightened 2026-04-28 OI-31 fix): dropped bare `validation` and
# `gated` (false-positive on "constructor validation", "auth-gated"). Added
# word-boundaries to `research|experiment|decay|OOS|AUC|Sharpe|CAGR`. Added
# `walk-forward leakage|frozen-memory|train-set|out-of-sample` for stricter
# research signals.
echo "$msg" | grep -qiE 'ML-[0-9]|backtest|\bresearch\b|\bexperiment\b|phenomenon|\bdecay\b|permutation|SA-[0-9]|Hopfield|hopfield|walk-forward|\bOOS\b|\bAUC\b|\bSharpe\b|\bCAGR\b|out-of-sample|frozen[- ]memory|train[- ]set|test[- ]set|cross-validation|leakage check|p-value|t-stat' || exit 0

# If we're here, it's a research commit.
cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"RESEARCH COMMIT DETECTED. Before committing:\n1. Have you run a verification pass (iron-law: success-token evidence in the SAME message; the verify skill for runtime changes)?\n2. List ALL deviations/shortcuts from the session plan\n3. For each deviation: was it flagged to the user BEFORE committing?\n4. Are there skipped tests, small sample sizes, or design flaws that should be disclosed?\n5. WALK-FORWARD LEAKAGE CHECK: Does ANY model accumulate test-period outcomes (labels, P&L) into its memory or training during the test period? If YES, the PRIMARY result MUST use FROZEN train-only memory. Walk-forward is SECONDARY only. Report BOTH.\n6. Is the frozen-memory calibration monotonic? If flat, the gate adds no value.\n\nIf you haven't done this yet, STOP and run verification first. Do NOT rationalize skipping this."}}
EOF
