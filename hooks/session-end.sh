#!/usr/bin/env bash
# SessionEnd metrics hook. Fail-OPEN: a telemetry failure must never surface
# as a session error, so no `set -e` — guards on every fallible substitution.
set -uo pipefail

CLANKER_DATA="${CLANKER_DATA:-/data/clanker}"
SESSIONS_DIR="$CLANKER_DATA/raw/sessions"
mkdir -p "$SESSIONS_DIR" 2>/dev/null || exit 0

# Read hook input from stdin
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null || true)
CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)

# Bail if no transcript
[ -z "$TRANSCRIPT" ] && exit 0
[ -f "$TRANSCRIPT" ] || exit 0

# Prevent duplicate logging (Stop + SessionEnd might both fire)
# Dedup files expire after 60 seconds — only prevents the same hook firing twice
# in quick succession, not across sessions or manual tests
DEDUP_FILE="/tmp/clanker-session-${SESSION_ID:-$$}"
if [ -f "$DEDUP_FILE" ]; then
    AGE=$(( $(date +%s) - $(stat -c %Y "$DEDUP_FILE" 2>/dev/null || echo 0) ))
    if [ "$AGE" -lt 60 ]; then
        exit 0
    fi
fi
touch "$DEDUP_FILE"

# Clean up old dedup files (>5 min)
find /tmp -maxdepth 1 -name "clanker-session-*" -mmin +5 -delete 2>/dev/null || true

# Extract metrics using Python
OUTFILE="$SESSIONS_DIR/$(date -u +%Y-%m-%d).jsonl"

# Expose the repo's lib/ so the hook resolves a session's project the SAME way
# the dashboard does (git-aware: worktrees collapse, repos outside ~/projects work).
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLANKER_LIB="$HOOK_DIR/../lib"
export TRANSCRIPT SESSION_ID CWD CLANKER_LIB

python3 -u << 'PYEOF' | flock "$OUTFILE.lock" tee -a "$OUTFILE" > /dev/null
import json, sys, os
from datetime import datetime
from collections import Counter

transcript_path = os.environ.get("TRANSCRIPT", "")
session_id = os.environ.get("SESSION_ID", "")
cwd = os.environ.get("CWD", "")

# Fallback: read from env if heredoc substitution fails
if not transcript_path:
    sys.exit(0)

# Determine project from cwd via the shared git-aware resolver. Falls back to the
# legacy ~/projects-only rule if lib/ isn't importable, so logging never breaks.
project = "global"
try:
    sys.path.insert(0, os.environ.get("CLANKER_LIB", ""))
    from projects import resolve_project
    project = resolve_project(cwd)
except Exception:
    if "/home/user/projects/" in cwd:
        project = cwd.split("/home/user/projects/")[-1].split("/")[0]

tool_uses = Counter()
error_tools = Counter()
errors = 0
files_touched = Counter()
user_corrections = 0
subagent_count = 0
first_ts = None
last_ts = None
claude_version = None
model_counter = Counter()
flags = []

# Token tracking
tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}

# Track tool_use IDs for error attribution
tool_id_to_name = {}
last_bash_commands = []
has_git_commit = False
has_git_push = False
has_deploy = False

try:
    with open(transcript_path) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = obj.get("timestamp")
            if ts and not first_ts:
                first_ts = ts
            if ts:
                last_ts = ts

            if not claude_version:
                claude_version = obj.get("version")

            msg_type = obj.get("type")

            if msg_type == "assistant":
                msg = obj.get("message", {})
                if isinstance(msg, dict):
                    m = msg.get("model")
                    if m:
                        model_counter[m] += 1
                    # Extract token usage
                    usage = msg.get("usage")
                    if isinstance(usage, dict):
                        tokens["input"] += usage.get("input_tokens", 0)
                        tokens["output"] += usage.get("output_tokens", 0)
                        tokens["cache_read"] += usage.get("cache_read_input_tokens", 0)
                        tokens["cache_create"] += usage.get("cache_creation_input_tokens", 0)

                    for item in msg.get("content", []):
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            tool_name = item.get("name", "unknown")
                            tool_id = item.get("id", "")
                            tool_uses[tool_name] += 1
                            tool_id_to_name[tool_id] = tool_name

                            inp = item.get("input", {})
                            if isinstance(inp, dict):
                                # Track file paths
                                for key in ("file_path", "path"):
                                    fp = inp.get(key)
                                    if fp and isinstance(fp, str):
                                        files_touched[fp] += 1

                                # Track Bash commands for outcome detection
                                if tool_name == "Bash":
                                    cmd = inp.get("command", "")
                                    last_bash_commands.append(cmd)
                                    if len(last_bash_commands) > 20:
                                        last_bash_commands.pop(0)
                                    if "git commit" in cmd:
                                        has_git_commit = True
                                    if "git push" in cmd:
                                        has_git_push = True
                                    # Require a real deploy INVOCATION, not the word "deploy"
                                    # appearing anywhere (which it does in skill names, logs, paths…).
                                    cl = cmd.lower()
                                    if ("docker push" in cl or "deploy-via-bundle" in cl
                                            or "gcloud run deploy" in cl or "npm run deploy" in cl
                                            or "./deploy" in cl or "bash deploy" in cl
                                            or "systemctl restart" in cl):
                                        has_deploy = True

                            if tool_name == "Agent":
                                subagent_count += 1

            elif msg_type == "user":
                msg = obj.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content", "")

                    # Check for user corrections (direct rejection patterns)
                    if isinstance(content, str):
                        lower = content.strip().lower()
                        if any(lower.startswith(p) for p in ["no ", "no,", "don't ", "stop ", "wrong", "not that", "undo ", "revert "]):
                            user_corrections += 1

                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict):
                                # Tool result errors
                                if item.get("is_error"):
                                    errors += 1
                                    err_tool_id = item.get("tool_use_id", "")
                                    err_tool_name = tool_id_to_name.get(err_tool_id, "unknown")
                                    error_tools[err_tool_name] += 1

                                # User rejected tool call
                                if isinstance(item.get("content"), str):
                                    if "user denied" in item.get("content", "").lower() or \
                                       "user doesn't want" in item.get("content", "").lower():
                                        user_corrections += 1

            # Track architecture keywords in assistant messages
            if msg_type == "assistant":
                msg2 = obj.get("message", {})
                if isinstance(msg2, dict):
                    text_parts = []
                    for item in msg2.get("content", []):
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                    full_text = " ".join(text_parts).lower()
                    arch_keywords = ["architecture", "design decision", "trade-off", "tradeoff",
                                     "decided to", "alternative was"]
                    if any(kw in full_text for kw in arch_keywords):
                        if "architecture-discussion" not in flags:
                            flags.append("architecture-discussion")

except Exception as e:
    # Log error but don't fail the hook
    sys.stderr.write(f"clanker session-end: {e}\n")

# Calculate duration
duration_s = 0
if first_ts and last_ts:
    try:
        t1 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        duration_s = int((t2 - t1).total_seconds())
    except:
        pass

# Determine outcome
outcome = "unknown"
if has_deploy:
    outcome = "deploy"
elif has_git_push:
    outcome = "push"
elif has_git_commit:
    outcome = "commit"
elif sum(tool_uses.values()) == 0:
    outcome = "empty"
elif errors > sum(tool_uses.values()) * 0.5:
    outcome = "abandoned"

# Resolve dominant model + per-model pricing ($/M tokens: in, out, cache_read, cache_write)
# Rates re-verified 2026-07-05 against the claude-api reference: Fable 5 $10/$50,
# Opus 4.8 $5/$25, Sonnet 5 $3/$15, Haiku 4.5 $1/$5; cache read ~0.1x input,
# cache write ~1.25x input. (Old table had Opus-4.1-era $15/$75, Haiku-3.5-era
# $0.8/$4, and priced Fable at sonnet rates — a 3-10x cost underestimate.)
model = model_counter.most_common(1)[0][0] if model_counter else None
_PRICING = {"fable": (10.0, 50.0, 1.00, 12.50),
            "opus": (5.0, 25.0, 0.50, 6.25),
            "sonnet": (3.0, 15.0, 0.30, 3.75),
            "haiku": (1.0, 5.0, 0.10, 1.25)}
_ml = (model or "").lower()
if "fable" in _ml or "mythos" in _ml:
    _pin, _pout, _pcr, _pcw = _PRICING["fable"]
elif "opus" in _ml:
    _pin, _pout, _pcr, _pcw = _PRICING["opus"]
elif "haiku" in _ml:
    _pin, _pout, _pcr, _pcw = _PRICING["haiku"]
else:  # sonnet + unknown -> sonnet-tier mid estimate
    _pin, _pout, _pcr, _pcw = _PRICING["sonnet"]
estimated_cost = round(
    tokens["input"] / 1e6 * _pin + tokens["output"] / 1e6 * _pout +
    tokens["cache_read"] / 1e6 * _pcr + tokens["cache_create"] / 1e6 * _pcw, 2)

# Build output
top_files = [f for f, _ in files_touched.most_common(50)]

record = {
    "timestamp": datetime.now(tz=__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "session_id": session_id,
    "project": project,
    "cwd": cwd,
    "duration_s": duration_s,
    "claude_version": claude_version,
    "model": model,
    "tool_uses": dict(tool_uses),
    "errors": errors,
    "error_tools": dict(error_tools),
    "files_touched": top_files,
    "files_touched_count": len(files_touched),
    "user_corrections": user_corrections,
    "subagent_count": subagent_count,
    "outcome": outcome,
    "flags": flags,
    "tokens": tokens,
    "estimated_cost_usd": estimated_cost,
}

print(json.dumps(record))
PYEOF

# Generate handoff
if [ -n "$CWD" ] && [ -d "$CWD/.git" ]; then
    python3 -c "
import sys
sys.path.insert(0, '$(dirname $(realpath $0))/../lib')
from handoff import generate_handoff
generate_handoff('$SESSION_ID', '$(basename $CWD)', '$CWD')
" 2>/dev/null || true
fi

# ── Memory safety net (2026-07-05, memory hardening): autocommit every memory
# change so a weak model clobbering/deleting memories is one `git revert` away.
# flock-guarded (45 sessions can end concurrently); push is best-effort with a
# hard timeout so SessionEnd never hangs; everything || true — fail-open.
(
  flock -n 9 || exit 0
  cd /home/user/.claude 2>/dev/null || exit 0
  # shell-expanded literal dirs (git wildcard pathspecs don't recurse here);
  # each add separate — one missing path must not abort the rest
  for d in projects/*/memory memory; do
      [ -d "$d" ] && git add -A -- "$d" 2>/dev/null || true
  done
  if ! git diff --cached --quiet 2>/dev/null; then
      git commit -q -m "memory: session autocommit (${SESSION_ID:-unknown})" 2>/dev/null || true
      for r in $(git remote 2>/dev/null); do
          timeout 10 git push -q "$r" HEAD 2>/dev/null || true
      done
  fi
) 9>/tmp/.claude-memory-autocommit.lock 2>/dev/null || true

exit 0
