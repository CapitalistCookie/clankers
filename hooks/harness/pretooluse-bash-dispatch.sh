#!/bin/bash
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# pretooluse-bash-dispatch.sh — ONE PreToolUse(Bash) hook replacing the 11 separate
# hook commands previously wired in ~/.claude/settings.json (round-2 harness
# hardening, 2026-07-05; queued in clanker STATUS.md NEXT).
#
# WHY: 11 hook commands on matcher Bash meant 11 process spawns per Bash tool call
# (3× python3 at ~25ms each): measured ~160ms CPU / ~40ms wall — paid even for `ls`.
# This dispatcher reads the payload ONCE, applies each gate's trigger as a pure-bash
# prefilter (zero extra spawns on the no-match hot path), and spawns only gates whose
# prefilter matches. Every prefilter is a strict SUPERSET of the gate's own internal
# trigger, and a spawned gate re-applies its precise logic on the same stdin — so
# gate decisions are unchanged, just cheaper.
#
# Parity proof: ~/.claude/hooks/tests/test_pretooluse_dispatch.sh (RED/GREEN per gate,
# dispatcher vs standalone).
#
# CONTRACT (Claude Code 2.1.x):
#   exit 0 + JSON stdout → advisory additionalContext, or permissionDecision deny
#   exit 2 + stderr      → blocking error; stderr is fed to Claude
#   gate exit 124/137    → the gate hit its per-gate timeout budget (same budget it
#                          had as a standalone settings entry): fail-OPEN, skip it
#   malformed stdin      → fail-CLOSED exit 2 (preserves the pwb gates' H-3
#                          convention, which was the effective aggregate behavior:
#                          any unparseable payload blocked via those gates)
#
# Documented deltas vs the old 11-parallel-hooks wiring:
#   * First blocking gate wins; later gates don't run (old: all ran in parallel and
#     every tripped blocker reported; the call was denied either way).
#   * Legacy top-level {"decision":"block"} is translated to
#     hookSpecificOutput.permissionDecision="deny" — the legacy form is no longer in
#     the documented PreToolUse contract. (The wrapped gates were also updated to
#     emit the modern form directly; translation remains as a safety net.)
#   * Multiple advisory outputs merge into ONE additionalContext JSON (docs:
#     "Claude receives all of the values" — preserved, just in one envelope).
#   * Advisory stderr from an exit-0 gate passes through to dispatcher stderr
#     (transcript-visible), as before.
set -uo pipefail

H="$HOME/.claude/hooks"

# Operator/machine config layer (publint law: no operator literals in hook
# bodies). Gates run as children of this dispatcher and inherit the exports —
# GPU_HOST, CHECK_GIT_TARGET_REPOS, CLANKER_DEPLOY_PREFLIGHT, etc. Generic
# installs without this file simply run with the gates inert-by-default.
# CLANKER_HARNESS_ENV overrides the file path — the parity harness points it at
# /dev/null so operator values can't clobber fixture env (2026-07-22: sourcing
# unconditionally broke the git-target parity case ever since harness.env
# introduced an operator CHECK_GIT_TARGET_REPOS — latent since 07-17).
HF="${CLANKER_HARNESS_ENV:-$HOME/.claude/harness.env}"
[ -f "$HF" ] && . "$HF" 2>/dev/null || true

INPUT=$(cat 2>/dev/null || true)

# ── stdin parse (fail-closed on malformed payload) ─────────────────────────────
if ! CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null); then
    echo "pretooluse-bash-dispatch: malformed hook stdin, failing closed (pwb H-3 convention)" >&2
    exit 2
fi
[[ -z "$CMD" ]] && exit 0

ERRF=$(mktemp "${TMPDIR:-/tmp}/ptbd-err.XXXXXX") || ERRF=/dev/null
trap 'rm -f "$ERRF" 2>/dev/null' EXIT

declare -a CTX=()
declare -a SYS=()

# ── route a gate's exit-0 stdout ────────────────────────────────────────────────
classify_and_route() {
    local label="$1" out="$2" kind
    kind=$(printf '%s' "$out" | jq -r '
        if ((.hookSpecificOutput.permissionDecision // "") == "deny")
           or ((.hookSpecificOutput.permissionDecision // "") == "ask") then "deny"
        elif ((.decision // "") == "block") then "legacyblock"
        else "soft" end' 2>/dev/null) || kind="invalid"
    case "$kind" in
        deny)
            # Forward verbatim; if earlier advisories accumulated, carry them along.
            if (( ${#CTX[@]} > 0 )); then
                local merged=""
                printf -v merged '%s\n\n' "${CTX[@]}"
                printf '%s' "$out" | jq -c --arg extra "${merged%$'\n\n'}" \
                    '.hookSpecificOutput.additionalContext =
                       ([$extra, (.hookSpecificOutput.additionalContext // "")]
                        | map(select(. != "")) | join("\n\n"))'
            else
                printf '%s\n' "$out"
            fi
            exit 0
            ;;
        legacyblock)
            local reason
            reason=$(printf '%s' "$out" | jq -r '.reason // "blocked"' 2>/dev/null) || reason="blocked"
            jq -cn --arg r "$reason" \
                '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
            exit 0
            ;;
        soft)
            local ctx sys
            ctx=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null) || ctx=""
            [[ -n "$ctx" ]] && CTX+=("$ctx")
            sys=$(printf '%s' "$out" | jq -r '.systemMessage // empty' 2>/dev/null) || sys=""
            [[ -n "$sys" ]] && SYS+=("$sys")
            ;;
        *)
            printf 'pretooluse-bash-dispatch: %s emitted unparseable stdout (ignored): %.200s\n' "$label" "$out" >&2
            ;;
    esac
}

# ── run one gate with its old per-hook timeout budget ───────────────────────────
run_gate() {
    local label="$1" budget="$2"; shift 2
    local out rc err=""
    out=$(printf '%s' "$INPUT" | timeout "$budget" "$@" 2>"$ERRF"); rc=$?
    [[ -s "$ERRF" ]] && err=$(<"$ERRF")
    : > "$ERRF" 2>/dev/null || true
    if (( rc == 2 )); then
        # Blocking gate: its stderr verbatim on our stderr; first blocker wins.
        if [[ -n "$err" ]]; then printf '%s\n' "$err" >&2
        else printf '%s: blocked (exit 2, no message)\n' "$label" >&2; fi
        exit 2
    fi
    if (( rc == 124 || rc == 137 )); then
        printf 'pretooluse-bash-dispatch: %s exceeded its %ss budget — skipped (fail-open)\n' "$label" "$budget" >&2
        return 0
    fi
    # Non-blocking error (rc not 0/2): fail-open, surface stderr like the old
    # per-hook transcript notice.
    if (( rc != 0 )); then
        [[ -n "$err" ]] && printf '%s\n' "$err" >&2
        return 0
    fi
    # rc==0: advisory stderr passes through; stdout (if any) gets routed.
    [[ -n "$err" ]] && printf '%s\n' "$err" >&2
    [[ -z "${out//[$' \t\r\n']/}" ]] && return 0
    classify_and_route "$label" "$out"
}

LC="${CMD,,}"

# GPU_HOST for gate 5's prefilter (gate re-reads research.env itself).
# De-personalized: empty default → gate 5's host-match prefilter is inert until
# an operator sets GPU_HOST (env or research.env below); gpu-train still triggers it.
GPU_HOST="${GPU_HOST:-}"
if [[ -r "$HOME/.claude/research.env" ]]; then
    while IFS= read -r _line; do
        [[ "$_line" == GPU_HOST=* ]] || continue
        GPU_HOST="${_line#GPU_HOST=}"
        GPU_HOST="${GPU_HOST%\"}"; GPU_HOST="${GPU_HOST#\"}"
    done < "$HOME/.claude/research.env"
fi

# ── gates, in the exact order they were wired in settings.json ──────────────────
# Prefilter comments state the gate's own internal trigger the prefilter supersets.

# 1. databento-native-completeness-guard (advisory) — gate needs a pull verb.
if [[ "$LC" == *get_cost* || "$LC" == *batch.submit* || "$LC" == *submit_job* \
   || "$LC" == *.get_range* || "$LC" == *databento-historical-pull* ]]; then
    run_gate databento-native-completeness-guard 5 python3 "$H/databento-native-completeness-guard.py"
fi

# 2. check-git-target (deny) — gate needs `git\s+push`.
if [[ "$CMD" =~ git[[:space:]]+push ]]; then
    run_gate check-git-target 5 bash "$H/check-git-target.sh"
fi

# 3. check-compute (advisory) — gate needs `(^|\s)python3?\s`.
re_python='(^|[[:space:]])python3?[[:space:]]'
if [[ "$CMD" =~ $re_python ]]; then
    run_gate check-compute 5 bash "$H/check-compute.sh"
fi

# 4. deploy-gate (deny / advisory) — gate needs deploy-vm.sh or `spacetime publish`.
# Budget 60s (was 30 in settings): the eigenstate deploy-preflight it shells out to
# measures ~26s wall — 30s was already one box-hiccup away from silently losing the
# gate to a timeout. Measured 2026-07-05.
if [[ "$CMD" == *deploy-vm.sh* || "$CMD" == *"spacetime publish"* ]]; then
    run_gate deploy-gate 60 bash "$H/deploy-gate.sh"
fi

# 5. gpu-vm-guard (deny / advisory) — gate needs GPU_HOST or gpu-train in cmd.
if [[ "$CMD" == *gpu-train* || ( -n "$GPU_HOST" && "$CMD" == *"$GPU_HOST"* ) ]]; then
    run_gate gpu-vm-guard 15 python3 "$H/gpu-vm-guard.py"
fi

# 6. backfill-safety (advisory) — gate needs a backfill_/port-v1 EXECUTION pattern.
if [[ "$LC" == *backfill_* || "$CMD" == *port-v1* ]]; then
    run_gate backfill-safety 5 bash "$H/backfill-safety.sh"
fi

# 7/8/9. git-commit gates — each re-detects commit precisely; superset: git…commit.
if [[ "$CMD" == *git*commit* ]]; then
    run_gate pre-commit-verification 5 bash "$H/pre-commit-verification.sh"
    run_gate pwb-pre-commit-bot-change-needs-test 10 bash "$H/pwb-pre-commit-bot-change-needs-test.sh"
    run_gate pwb-risk-surface-review-required 10 bash "$H/pwb-risk-surface-review-required.sh"
fi

# 10. pwb-pre-deploy-suite-green (block) — gate needs polymarket_weather_bot in line 1.
if [[ "$CMD" == *polymarket_weather_bot* ]]; then
    run_gate pwb-pre-deploy-suite-green 120 bash "$H/pwb-pre-deploy-suite-green.sh"
fi

# 11. ssh-tunnel-port-guard (deny) — gate needs ssh + `-L/-D <arg>`.
re_ld='-[LD][[:space:]]*[^[:space:]]'
if [[ "$CMD" == *ssh* && "$CMD" =~ $re_ld ]]; then
    run_gate ssh-tunnel-port-guard 10 python3 "$H/ssh-tunnel-port-guard.py"
fi

# 12. memory bash-write guard (block) — gate needs a write op INTO */memory/*.md
# (memory hardening 2026-07-05: shell writes bypass memory-lint; force Write/Edit).
if [[ "$CMD" == *"/memory/"* ]]; then
    run_gate memory-bash-guard 5 bash "$H/memory-lint.sh" --bash-guard
fi

# ── merged advisory emit ────────────────────────────────────────────────────────
if (( ${#CTX[@]} > 0 || ${#SYS[@]} > 0 )); then
    ctx_merged=""; sys_merged=""
    (( ${#CTX[@]} > 0 )) && printf -v ctx_merged '%s\n\n' "${CTX[@]}"
    (( ${#SYS[@]} > 0 )) && printf -v sys_merged '%s\n' "${SYS[@]}"
    jq -cn --arg ctx "${ctx_merged%$'\n\n'}" --arg sys "${sys_merged%$'\n'}" '
        {}
        | (if $sys != "" then .systemMessage = $sys else . end)
        | (if $ctx != "" then .hookSpecificOutput = {hookEventName:"PreToolUse", additionalContext:$ctx} else . end)'
fi
exit 0
