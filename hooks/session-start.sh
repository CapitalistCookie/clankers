#!/usr/bin/env bash
# SessionStart briefing hook. Fail-OPEN by design: a briefing failure must
# never break session start, so no `set -e` — every substitution is guarded.
#
# PERF (P11, 2026-07-22): everything python runs in ONE interpreter at the end
# (stub row, archetype, env-file exports, alerts, briefing, final JSON) —
# the old shape spawned 4+ serial interpreters plus one jq PER alert file.
# Measured with the hook-tax harness: 338ms → see commit for the after number.
# Each piece is individually try/except'd inside the block, so per-piece
# degradation matches the old one-process-per-piece behavior.
set -uo pipefail

CLANKER_DATA="${CLANKER_DATA:-/data/clanker}"
CLANKER_REGISTRY="${CLANKER_REGISTRY:-$HOME/projects/.clanker.yaml}"
# Claude Code's namespace slug for $HOME (slashes/dots become dashes)
HOME_SLUG="${HOME//[\/.]/-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Read hook input from stdin — ONE jq pass for all fields (was one per field)
INPUT=$(cat)
IFS=$'\t' read -r CWD SOURCE SESSION_ID < <(
    echo "$INPUT" | jq -r '[.cwd // "", .source // "", .session_id // ""] | @tsv' 2>/dev/null
) || true
CWD="${CWD:-}"; SOURCE="${SOURCE:-}"; SESSION_ID="${SESSION_ID:-}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$CWD}"

# --- 0. On /clear, extract metrics from the PREVIOUS session ---
if [ "$SOURCE" = "clear" ]; then
    # /clear was invoked — the session_id and transcript_path are for the NEW session
    # The OLD session's transcript is the most recently modified .jsonl in this project dir
    PROJECT_DIR_FOR_CLEAR="${CLAUDE_PROJECT_DIR:-$CWD}"
    CLAUDE_PROJ_DIR=$(echo "$PROJECT_DIR_FOR_CLEAR" | sed 's|/|-|g')
    OLD_TRANSCRIPT=$(ls -t "$HOME/.claude/projects/$CLAUDE_PROJ_DIR/"*.jsonl 2>/dev/null | head -2 | tail -1 || true)

    if [ -n "$OLD_TRANSCRIPT" ] && [ -f "$OLD_TRANSCRIPT" ]; then
        OLD_SESSION_ID=$(basename "$OLD_TRANSCRIPT" .jsonl)
        export TRANSCRIPT="$OLD_TRANSCRIPT" SESSION_ID="$OLD_SESSION_ID" CWD="$CWD"
        bash "$(dirname "${BASH_SOURCE[0]}")/session-end.sh" < <(
            echo "{\"session_id\":\"$OLD_SESSION_ID\",\"transcript_path\":\"$OLD_TRANSCRIPT\",\"cwd\":\"$CWD\"}"
        ) 2>/dev/null &
        SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
    fi
fi

# --- 1. Run codebase indexer if project has one (backgrounded) ---
if [ -n "$PROJECT_DIR" ] && [ -d "$PROJECT_DIR" ]; then
    if [ -f "$PROJECT_DIR/docs/codebase-index/generate.py" ]; then
        python3 "$PROJECT_DIR/docs/codebase-index/generate.py" \
            --routing-table "$HOME/.claude/projects/$HOME_SLUG/memory/CODEBASE_INDEX.md" \
            2>/dev/null &
    fi
    if [ -f "$PROJECT_DIR/docs/codebase-index/generate-sdk-reference.sh" ]; then
        bash "$PROJECT_DIR/docs/codebase-index/generate-sdk-reference.sh" 2>/dev/null &
    fi
fi

# --- 2. Memory index self-heal (memory hardening 2026-07-05): if MEMORY.md
# changed since INDEX_ALL.md was generated (e.g. a bash-path write slipped past
# the lint hook, or a file was rm'd), regenerate the inventory and surface the
# orphan count. Covers BOTH the global router dir and this session's namespace.
MEM_NOTE=""
LINT="$HOME/.claude/hooks/memory-lint.sh"
if [ -x "$LINT" ] || [ -f "$LINT" ]; then
    NS_SLUG=$(printf '%s' "${CWD:-}" | sed 's|[/.]|-|g')
    for MDIR in "$HOME/.claude/projects/$HOME_SLUG/memory" "$HOME/.claude/projects/$NS_SLUG/memory"; do
        [ -f "$MDIR/MEMORY.md" ] || continue
        if [ ! -f "$MDIR/INDEX_ALL.md" ] || [ "$MDIR/MEMORY.md" -nt "$MDIR/INDEX_ALL.md" ]; then
            ORPH=$(bash "$LINT" --regen "$MDIR" 2>/dev/null | sed -n 's/^orphans=//p' || true)
            if [ -n "$ORPH" ] && [ "$ORPH" -gt 0 ] 2>/dev/null; then
                MEM_NOTE="memory: $ORPH orphaned file(s) unreachable from the router indexes — see $MDIR/INDEX_ALL.md §ORPHANS"
            fi
        fi
        # Index-budget warn band (sharded-router 2026-07-19): surface ≥80% at
        # session start so a filling index is heard about BEFORE the 16KB wall.
        for IDX in "$MDIR/MEMORY.md" "$MDIR"/*-POINTERS.md; do
            [ -f "$IDX" ] || continue
            SZ=$(wc -c < "$IDX" 2>/dev/null || echo 0)
            if [ "$SZ" -gt 13107 ] 2>/dev/null; then
                MEM_NOTE="${MEM_NOTE:+$MEM_NOTE · }memory: $(basename "$IDX") at $((SZ * 100 / 16384))% of its 16KB index budget — split the shard (law: MEMORY.md header)"
            fi
        done
    done
fi

# --- 3. STATUS.md + recent git log injection (harness overhaul M4, 2026-07-05) ---
# The cold-start contract: a session launched in a repo sees the repo's live state
# without any pasted handoff. Cheap + bounded (head -30 / log -5).
STATUS_MSG=""
if [ -n "$PROJECT_DIR" ] && [ -f "$PROJECT_DIR/STATUS.md" ]; then
    STATUS_MSG="=== STATUS.md (repo state — update it as part of finishing work) ===
$(head -30 "$PROJECT_DIR/STATUS.md")"
    if [ -d "$PROJECT_DIR/.git" ]; then
        STATUS_MSG="$STATUS_MSG
=== git log -5 ===
$(git -C "$PROJECT_DIR" log --oneline -5 2>/dev/null)"
    fi
fi

# --- 4. The single python pass: heartbeat stub (P7), archetype, env-file
# exports, alerts, briefing, assembly, final hook JSON. ---
export CWD SOURCE SESSION_ID PROJECT_DIR SCRIPT_DIR CLANKER_DATA CLANKER_REGISTRY MEM_NOTE STATUS_MSG
export CLAUDE_ENV_FILE="${CLAUDE_ENV_FILE:-}"
python3 - <<'PY' 2>/dev/null || true
import json, os, sys, time

cwd = os.environ.get("CWD", "")
session_id = os.environ.get("SESSION_ID", "")
project_dir = os.environ.get("PROJECT_DIR", "")
data_dir = os.environ.get("CLANKER_DATA", "/data/clanker")
registry = os.environ.get("CLANKER_REGISTRY", "")
sys.path.insert(0, os.path.join(os.environ.get("SCRIPT_DIR", "."), "..", "lib"))

# 4a. Heartbeat stub row (P7, audit M4): a long-lived session must EXIST in
# telemetry before it dies — 07-20/21 had ~49 live sessions and ZERO rows.
# SessionEnd's full record supersedes this via consumers' last-write-wins
# dedup (analyze.load_sessions); a stub with no final row = still running.
if session_id:
    try:
        project = "global"
        try:
            from projects import resolve_project
            project = resolve_project(cwd)
        except Exception:
            _root = os.path.expanduser("~/projects/")
            if _root in cwd:
                project = cwd.split(_root)[-1].split("/")[0]
        sdir = os.path.join(data_dir, "raw", "sessions")
        os.makedirs(sdir, exist_ok=True)
        out = os.path.join(sdir, time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl")
        import fcntl
        with open(out + ".lock", "a") as lk:          # same lock file session-end flocks
            fcntl.flock(lk, fcntl.LOCK_EX)
            with open(out, "a") as f:
                f.write(json.dumps({
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "session_id": session_id,
                    "project": project,
                    "cwd": cwd,
                    "outcome": "open",
                    "source": os.environ.get("SOURCE", "") or None,
                }) + "\n")
    except Exception:
        pass

# 4b. Project + archetype from the registry yaml
project_name, archetype = "", ""
if project_dir and registry and os.path.isfile(registry):
    project_name = os.path.basename(project_dir)
    archetype = "unknown"
    try:
        import yaml
        reg = yaml.safe_load(open(registry)) or {}
        archetype = (reg.get("projects", {}).get(project_name, {}) or {}).get("archetype", "unknown")
    except Exception:
        pass

# 4c. Env vars for downstream hooks
envf = os.environ.get("CLAUDE_ENV_FILE", "")
if envf:
    try:
        with open(envf, "a") as f:
            if project_name:
                f.write(f"export CLANKER_PROJECT={project_name}\n")
            if archetype:
                f.write(f"export CLANKER_ARCHETYPE={archetype}\n")
    except Exception:
        pass

# 4d. Active alerts (was one jq spawn per alert file)
alert_msg = ""
try:
    adir = os.path.join(data_dir, "alerts")
    files = sorted(fn for fn in os.listdir(adir) if fn.endswith(".json")) if os.path.isdir(adir) else []
    if files:
        lines = [f"CLANKER ALERTS ({len(files)} active):"]
        for fn in files:
            try:
                a = json.load(open(os.path.join(adir, fn)))
                lines.append(f"  [{a.get('severity') or 'info'}] {a.get('message') or 'unknown alert'}")
            except Exception:
                lines.append("  [info] unknown alert")
        alert_msg = "\n".join(lines)
except Exception:
    pass

# 4e. Project briefing
briefing = ""
if project_name and project_name != "unknown" and os.path.isdir(project_dir):
    try:
        from briefing import generate_briefing
        briefing = generate_briefing(project_name, project_dir) or ""
    except Exception:
        pass

# 4f. Assemble + emit (REAL newlines — the old bash literal "\\n" joins
# rendered as backslash-n in the injected context)
parts = [p.strip() for p in (alert_msg, briefing, os.environ.get("MEM_NOTE", ""),
                             os.environ.get("STATUS_MSG", "")) if p and p.strip()]
full = "\n".join(parts)
if not full and project_name:
    full = f"Clanker: project={project_name} archetype={archetype}"
if full:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": full}}))
PY

exit 0
