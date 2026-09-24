#!/bin/bash
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# pretooluse-bash-dispatch.sh — ONE PreToolUse(Bash) hook (originally replacing the 11 separate
# hook commands previously wired in ~/.claude/settings.json; round-2 harness
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
# 2026-09-24 rewire: the dispatcher now carries only the 4 GENERIC gates —
# check-compute, gpu-vm-guard, pre-commit-verification, ssh-tunnel-port-guard
# (budgets 5+15+5+10 = 35 s; settings timeout 60 s). Project-specific gates moved
# to their repos' .claude/settings.json: check-git-target / deploy-gate /
# backfill-safety -> eigenstate; the pwb commit + suite-green gates -> polymarket.
# Retired: databento-native-completeness-guard (0 fires in 60 days) and the
# memory-lint bash guard (auto-memory is off since 2026-08-08).
#
# CONTRACT (Claude Code 2.1.x):
#   exit 0 + JSON stdout → advisory additionalContext, or permissionDecision deny
#   exit 2 + stderr      → blocking error; stderr is fed to Claude
#   gate exit 124/137    → the gate hit its per-gate timeout budget (same budget it
#                          had as a standalone settings entry): fail-OPEN, skip it
#   malformed stdin      → fail-CLOSED exit 2 (kept from the pwb gates' H-3
#                          convention; those gates now fail closed on their own
#                          in the polymarket repo)
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
#   * Fail-visible (2026-09-24): a gate that errors (rc not 0/2), hits its
#     budget or prints unparseable stdout, and a failed harness.env source or
#     jq emit, append a row to the hook-error log (block below). Gate
#     decisions are unchanged.
set -uo pipefail

# ---- hook-error log: the same block in every clanker bash hook ------------------
# A failure that the hook swallows (the hook stays fail-open for the session)
# appends one JSON line {ts, hook, session_id, cwd, rc, stderr_tail} to
# ${CLANKER_DATA:-/data/clanker}/raw/health/hook-errors-<UTC day>.jsonl, where
# `clanker doctor --harness` counts it. hook_err never blocks, never writes to
# stdout and never changes the hook's exit code, under set -e and set -u too.
# Usage: hook_err <rc> <step> [<error text>]. stderr_tail is "<step>: " plus the
# end of the text, 300 characters at most. Put the raw hook payload in
# HOOK_ERR_IN so that the row names the session and its cwd.
HOOK_ERR_IN=""
hook_err_str() {
    local s="$1"
    s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
    s="${s//$'\t'/\\t}"; s="${s//$'\r'/\\r}"; s="${s//$'\n'/\\n}"
    printf '"%s"' "$(printf '%s' "$s" | LC_ALL=C tr -d '\000-\010\013\014\016-\037\177')"
}
hook_err() {
    (
        set +eu
        rc="$1" step="${2:-?}" text="$3" sid="" re=""
        [[ $rc =~ ^-?[0-9]+$ ]] || rc=1
        room=$(( 298 - ${#step} ))
        if (( room <= 0 )); then text=""
        elif (( ${#text} > room )); then text="${text:${#text}-room}"; fi
        msg="$step${text:+: $text}"
        re='"session_id"[[:space:]]*:[[:space:]]*"([^"\\]*)"'
        [[ $HOOK_ERR_IN =~ $re ]] && sid="${BASH_REMATCH[1]}"
        re='"cwd"[[:space:]]*:[[:space:]]*"(([^"\\]|\\.)*)"'
        if [[ $HOOK_ERR_IN =~ $re ]]; then cwd="\"${BASH_REMATCH[1]}\""
        else cwd=$(hook_err_str "$PWD"); fi
        ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        d="${CLANKER_DATA:-/data/clanker}/raw/health"
        mkdir -p "$d" && printf '{"ts":"%s","hook":%s,"session_id":"%s","cwd":%s,"rc":%s,"stderr_tail":%s}\n' \
            "$ts" "$(hook_err_str "${0##*/}")" "$sid" "$cwd" "$rc" "$(hook_err_str "${msg:0:300}")" \
            >> "$d/hook-errors-${ts%%T*}.jsonl"
        exit 0
    ) </dev/null >/dev/null 2>&1
    return 0
}
# ---- end of hook-error log --------------------------------------------------------

H="$HOME/.claude/hooks"

# Operator/machine config layer (publint law: no operator literals in hook
# bodies). Gates run as children of this dispatcher and inherit the exports —
# GPU_HOST / GPU_USER for gates 1 and 2. Generic installs without this file
# simply run with the gates inert-by-default.
# CLANKER_HARNESS_ENV overrides the file path — the parity harness points it at
# /dev/null so operator values can't clobber fixture env (2026-07-22: sourcing
# unconditionally broke the git-target parity case ever since harness.env
# introduced an operator CHECK_GIT_TARGET_REPOS — latent since 07-17).
HF="${CLANKER_HARNESS_ENV:-$HOME/.claude/harness.env}"
if [ -f "$HF" ]; then
    . "$HF" 2>/dev/null || hook_err $? "source $HF"
fi

INPUT=$(cat 2>/dev/null || true)
HOOK_ERR_IN="$INPUT"

# ── stdin parse (fail-closed on malformed payload) ─────────────────────────────
if ! CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null); then
    echo "pretooluse-bash-dispatch: malformed hook stdin, failing closed (pwb H-3 convention)" >&2
    exit 2
fi
[[ -z "$CMD" ]] && exit 0

ERRF=$(mktemp "${TMPDIR:-/tmp}/ptbd-err.XXXXXX") || { hook_err $? "mktemp (gate stderr is lost)"; ERRF=/dev/null; }
trap '[ "$ERRF" = /dev/null ] || rm -f "$ERRF" 2>/dev/null' EXIT

declare -a CTX=()
declare -a SYS=()

# ── route a gate's exit-0 stdout ────────────────────────────────────────────────
classify_and_route() {
    local label="$1" out="$2" kind jrc=0
    kind=$(printf '%s' "$out" | jq -r '
        if ((.hookSpecificOutput.permissionDecision // "") == "deny")
           or ((.hookSpecificOutput.permissionDecision // "") == "ask") then "deny"
        elif ((.decision // "") == "block") then "legacyblock"
        else "soft" end' 2>/dev/null) || { jrc=$?; kind="invalid"; }
    case "$kind" in
        deny)
            # Forward verbatim; if earlier advisories accumulated, carry them along.
            if (( ${#CTX[@]} > 0 )); then
                local merged=""
                printf -v merged '%s\n\n' "${CTX[@]}"
                printf '%s' "$out" | jq -c --arg extra "${merged%$'\n\n'}" \
                    '.hookSpecificOutput.additionalContext =
                       ([$extra, (.hookSpecificOutput.additionalContext // "")]
                        | map(select(. != "")) | join("\n\n"))' \
                    || hook_err $? "gate $label: merge the deny with earlier advice (jq)"
            else
                printf '%s\n' "$out"
            fi
            exit 0
            ;;
        legacyblock)
            local reason
            reason=$(printf '%s' "$out" | jq -r '.reason // "blocked"' 2>/dev/null) || reason="blocked"
            jq -cn --arg r "$reason" \
                '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}' \
                || hook_err $? "gate $label: translate a legacy block (jq)"
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
            hook_err "$jrc" "gate $label: unparseable stdout ignored" "${out:0:200}"
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
        hook_err "$rc" "gate $label: exceeded its ${budget}s budget, skipped (fail-open)" "$err"
        return 0
    fi
    # Non-blocking error (rc not 0/2): fail-open, surface stderr like the old
    # per-hook transcript notice, and log it.
    if (( rc != 0 )); then
        [[ -n "$err" ]] && printf '%s\n' "$err" >&2
        hook_err "$rc" "gate $label: error, skipped (fail-open)" "$err"
        return 0
    fi
    # rc==0: advisory stderr passes through; stdout (if any) gets routed.
    [[ -n "$err" ]] && printf '%s\n' "$err" >&2
    [[ -z "${out//[$' \t\r\n']/}" ]] && return 0
    classify_and_route "$label" "$out"
}

# GPU_HOST for gate 2's prefilter comes from harness.env (sourced above), the same
# export the gate itself reads. research.env is NOT read (2026-09-24): it also
# carries third-party API keys. Empty default → the host-match prefilter is inert
# until an operator sets GPU_HOST; gpu-train still triggers the gate.
GPU_HOST="${GPU_HOST:-}"

# ── gates, in their original relative order ─────────────────────────────────────
# Prefilter comments state the gate's own internal trigger the prefilter supersets.

# 1. check-compute (advisory) — gate needs `(^|\s)python3?\s`.
re_python='(^|[[:space:]])python3?[[:space:]]'
if [[ "$CMD" =~ $re_python ]]; then
    run_gate check-compute 5 bash "$H/check-compute.sh"
fi

# 2. gpu-vm-guard (deny / advisory) — gate needs GPU_HOST or gpu-train in cmd.
if [[ "$CMD" == *gpu-train* || ( -n "$GPU_HOST" && "$CMD" == *"$GPU_HOST"* ) ]]; then
    run_gate gpu-vm-guard 15 python3 "$H/gpu-vm-guard.py"
fi

# 3. pre-commit-verification (advisory) — re-detects commit precisely; superset: git…commit.
if [[ "$CMD" == *git*commit* ]]; then
    run_gate pre-commit-verification 5 bash "$H/pre-commit-verification.sh"
fi

# 4. ssh-tunnel-port-guard (deny) — gate needs ssh + `-L/-D <arg>`.
re_ld='-[LD][[:space:]]*[^[:space:]]'
if [[ "$CMD" == *ssh* && "$CMD" =~ $re_ld ]]; then
    run_gate ssh-tunnel-port-guard 10 python3 "$H/ssh-tunnel-port-guard.py"
fi

# ── merged advisory emit ────────────────────────────────────────────────────────
if (( ${#CTX[@]} > 0 || ${#SYS[@]} > 0 )); then
    ctx_merged=""; sys_merged=""
    (( ${#CTX[@]} > 0 )) && printf -v ctx_merged '%s\n\n' "${CTX[@]}"
    (( ${#SYS[@]} > 0 )) && printf -v sys_merged '%s\n' "${SYS[@]}"
    jq -cn --arg ctx "${ctx_merged%$'\n\n'}" --arg sys "${sys_merged%$'\n'}" '
        {}
        | (if $sys != "" then .systemMessage = $sys else . end)
        | (if $ctx != "" then .hookSpecificOutput = {hookEventName:"PreToolUse", additionalContext:$ctx} else . end)' \
        || hook_err $? "emit the merged advice (jq)"
fi
exit 0
