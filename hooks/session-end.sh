#!/usr/bin/env bash
# SessionEnd metrics hook. Fail-OPEN: a telemetry failure must never surface
# as a session error, so no `set -e`; every substitution that can fail is guarded.
#
# Telemetry truth (2026-09-24, design audit proposal 1):
# - Claude Code writes one transcript line per content block, each carrying the
#   whole message's usage, so summing every line counted tokens ~2.15x. Usage
#   now counts once per assistant message id (the largest value of each field:
#   subagent transcripts carry partial output_tokens on all but the last line).
# - Subagent transcripts (<transcript dir>/<session>/subagents/**/agent-*.jsonl)
#   are read into subagent_tokens / subagent_cost_usd, beside the main figures
#   rather than folded into them.
# - Each message is priced at its own model's rate (table below), with 1-hour
#   cache writes at 2x input and 5-minute writes at 1.25x.
# - The project comes from the registry the way session-start.sh resolves it:
#   `projects:` paths, then git repos directly under the project roots, then the
#   git checkout's name. The old `from projects import` never resolved in the
#   installed copy, beside which no module directory was ever installed, so
#   every session outside ~/projects was logged as "global": 274 rows from one
#   registered build repo that lives on a data volume behind a ~/projects symlink.
# - A scripted `claude -p` run (CLAUDE_CODE_ENTRYPOINT=sdk-cli, in the env or
#   on the transcript's lines) is tagged nested: true.
# - The hook imports no module of this repo. The limit-signature catalog is
#   inlined: its source, the subagent auto-resume detector, was retired the
#   same day, and the fallback the installed copy used held 7 of its 17
#   signatures. The handoff writer is gone: its import of the handoff module
#   pointed at a module directory that never existed on the installed side,
#   so it had not run since 07-19.
# - rewrite_tokens (proposal 3): the cache writes of main-transcript calls
#   that re-wrote a cached prefix, i.e. read less from the cache than the call
#   before them on the same model. `clanker analyze --rewrites` ranks sessions
#   by it.
# - last_assistant_line comes from last-assistant-msg.py, the helper the Stop
#   gates share. sync installs it in ~/.claude/hooks, the parent of this
#   hook's clanker-dist directory. Without it, this hook's own transcript pass
#   supplies the line.
# - Fail-visible (2026-09-24): a guarded step that fails, and a python run that
#   exits nonzero, append a row to the hook-error log (block below).
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

CLANKER_DATA="${CLANKER_DATA:-/data/clanker}"
SESSIONS_DIR="$CLANKER_DATA/raw/sessions"

# Read hook input from stdin
INPUT=$(cat)
HOOK_ERR_IN="$INPUT"
if ! mkdir_err=$(mkdir -p "$SESSIONS_DIR" 2>&1); then
    hook_err 1 "mkdir $SESSIONS_DIR" "$mkdir_err"
    exit 0
fi
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null) \
    || { hook_err $? "jq: parse the hook input"; SESSION_ID=""; }
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null || true)
CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)
# SessionEnd's own reason (clear/logout/prompt_input_exit/other) — recorded as
# end_reason so analytics can tell a /clear from a real exit (audit M4/P6).
END_REASON=$(echo "$INPUT" | jq -r '.reason // empty' 2>/dev/null || true)

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
touch "$DEDUP_FILE" 2>/dev/null

# Clean up old dedup files (>5 min)
find /tmp -maxdepth 1 -name "clanker-session-*" -mmin +5 -delete 2>/dev/null || true

# Extract metrics using Python
OUTFILE="$SESSIONS_DIR/$(date -u +%Y-%m-%d).jsonl"

# The helper the Stop gates share, one level up: for this hook in
# ~/.claude/hooks/clanker-dist that is ~/.claude/hooks/last-assistant-msg.py.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAST_MSG_HELPER="$HOOK_DIR/../last-assistant-msg.py"
export TRANSCRIPT SESSION_ID CWD END_REASON LAST_MSG_HELPER

# -I: isolated (no user site, no PYTHON* env); the block imports standard modules only.
metrics() {
python3 -I -u << 'PYEOF'
# ---- hook-error log: the same block in every clanker python hook ----------------
# A failure that the hook swallows (the hook stays fail-open for the session)
# appends one JSON line {ts, hook, session_id, cwd, rc, stderr_tail} to
# $CLANKER_DATA/raw/health/hook-errors-<UTC day>.jsonl (default /data/clanker),
# where `clanker doctor --harness` counts it. hook_err never raises, never
# blocks and never writes to stdout. Usage: hook_err(rc, step, error text or
# exception); stderr_tail is "<step>: " plus the end of the text (of the
# traceback, for an exception), 300 characters at most. Set
# HOOK_ERR["session_id"] and HOOK_ERR["cwd"] once the payload is parsed; a
# script read from stdin has no __file__ and sets HOOK_ERR["hook"] as well.
import os as _he_os
import sys as _he_sys

HOOK_ERR = {"hook": _he_os.path.basename(globals().get("__file__") or "") or "?",
            "session_id": "", "cwd": ""}


def hook_err(rc, step, err=""):
    try:
        import json
        import time
        import traceback
        if isinstance(err, BaseException):
            err = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        step, err = str(step), str(err or "").strip()
        room = 298 - len(step)
        msg = (step + ": " + err[-room:]) if err and room > 0 else step
        try:
            cwd = HOOK_ERR.get("cwd") or _he_os.getcwd()
        except OSError:
            cwd = ""
        rc = int(rc) if str(rc).lstrip("-").isdigit() else 1
        now = time.gmtime()
        d = _he_os.path.join(_he_os.environ.get("CLANKER_DATA") or "/data/clanker",
                             "raw", "health")
        _he_os.makedirs(d, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", now),
               "hook": str(HOOK_ERR.get("hook") or "?"),
               "session_id": str(HOOK_ERR.get("session_id") or ""), "cwd": str(cwd),
               "rc": rc, "stderr_tail": msg[:300]}
        path = _he_os.path.join(d, "hook-errors-" + time.strftime("%Y-%m-%d", now) + ".jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass
# ---- end of hook-error log --------------------------------------------------------

import itertools, json, os, re, subprocess, sys
from datetime import datetime, timezone
from collections import Counter, deque

transcript_path = os.environ.get("TRANSCRIPT", "")
session_id = os.environ.get("SESSION_ID", "")
cwd = os.environ.get("CWD", "")
HOOK_ERR.update(hook="session-end.sh", session_id=session_id, cwd=cwd)

# Fallback: read from env if heredoc substitution fails
if not transcript_path:
    sys.exit(0)

HOME = os.path.expanduser("~")
REGISTRY = os.environ.get("CLANKER_REGISTRY") or os.path.join(HOME, "projects", ".clanker.yaml")

# ---- project resolution -------------------------------------------------------
# Byte-identical in session-start.sh and session-end.sh: the stub row and the
# final row must name one project (tests/test_session_end.py compares them).
KEY_RE = re.compile(r"""^(['"]?)(.+?)\1:(?:[ \t]+(.*))?$""")


def unquote(v):
    v = (v or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    return v


def load_registry(path):
    """({name: {archetype, path}}, {alias: name}) from the registry's `projects:`
    and `aliases:` blocks; (None, {}) when there is no registry file. registry.py
    writes block-style YAML, read here line by line: no PyYAML (python3 -I keeps
    the user site out), and far cheaper. An alias whose value is a mapping
    rather than a name is ignored."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None, {}
    projects, aliases = {}, {}
    sec, cur, name_ind, key_ind = None, None, None, None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        ind = len(raw) - len(raw.lstrip(" "))
        if ind == 0:
            sec = s[:-1] if s in ("projects:", "aliases:") else None
            cur, name_ind, key_ind = None, None, None
            continue
        if sec is None:
            continue
        if name_ind is None:
            name_ind = ind
        if ind < name_ind:
            continue
        if ind == name_ind:
            m = KEY_RE.match(s)
            cur, key_ind = (m.group(2) if m else None), None
            if cur is None:
                continue
            if sec == "projects":
                projects[cur] = {}
            else:
                v = unquote(m.group(3))
                if v and v not in ("null", "~") and v[0] not in "{[|>&*!":
                    aliases[cur] = v
            continue
        if sec != "projects" or cur is None:
            continue
        if key_ind is None:
            key_ind = ind
        if ind != key_ind:
            continue                                  # continuation of a long value
        m = re.match(r"^(archetype|path):\s*(.*)$", s)
        if m:
            v = unquote(m.group(2))
            if v and v not in ("null", "~"):
                projects[cur][m.group(1)] = v
    if not projects and re.search(r"(?m)^projects:[ \t]*\S", text):
        try:                                          # flow style: needs PyYAML
            import site
            sys.path.append(site.getusersitepackages())
            import yaml
            for k, v in ((yaml.safe_load(text) or {}).get("projects") or {}).items():
                v = v if isinstance(v, dict) else {}
                projects[str(k)] = {kk: str(v[kk]) for kk in ("archetype", "path") if v.get(kk)}
        except Exception as e:
            hook_err(1, "registry: flow-style yaml", e)
    return projects, aliases


def project_roots():
    """CLANKER_PROJECT_ROOTS, colon-separated, default ~/projects, as the CLI reads it."""
    out = []
    for r in os.environ.get("CLANKER_PROJECT_ROOTS", "~/projects").split(":"):
        r = r.strip()
        if r:
            out.append(os.path.abspath(os.path.expanduser(r)))
    return out


def git_checkout(real):
    """(top, main) for a real path inside a git checkout: top holds the `.git`
    entry, main is the main repository's directory (a linked worktree resolves
    to the repository it belongs to). (None, None) outside git. Reads the
    filesystem only: no git process."""
    p = real
    while p:
        g = os.path.join(p, ".git")
        if os.path.isdir(g):
            return p, p
        if os.path.isfile(g):
            main = p
            try:
                with open(g, encoding="utf-8", errors="replace") as f:
                    first = f.readline().strip()
                if first.startswith("gitdir:"):
                    gd = os.path.realpath(os.path.join(p, first[len("gitdir:"):].strip()))
                    common = gd
                    try:
                        with open(os.path.join(gd, "commondir"), encoding="utf-8") as f:
                            common = os.path.realpath(os.path.join(gd, f.read().strip()))
                    except OSError:
                        pass
                    if os.path.basename(common) == ".git":
                        main = os.path.dirname(common)
            except OSError:
                pass
            return p, main
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None, None


def discovered_repos():
    """{main repo real path: name} for the git repos directly under each project
    root (a root that is itself a repo counts as one): the CLI's scan_projects,
    which clanker's Registry.projects unions with the yaml."""
    found = {}
    for root in project_roots():
        if not os.path.isdir(root):
            continue
        if os.path.isdir(os.path.join(root, ".git")):
            kids = [root]
        else:
            try:
                kids = [os.path.join(root, c) for c in sorted(os.listdir(root))]
            except OSError:
                continue
        for k in kids:
            if os.path.isdir(os.path.join(k, ".git")):
                main = os.path.realpath(k)
                found.setdefault(main, os.path.basename(main))
    return found


def resolve_project(cwd, reg, aliases):
    """(name, kind, path, top, main) for a working directory. kind:
      registry   - inside a `projects:` entry's path (its `path:`, else
                   ~/projects/<name>), or in a linked worktree of that repo
      discovered - inside a git repo directly under a project root, or in a
                   linked worktree of one
      name       - no path matched, but the directory's name is a registry name
      repo       - any other git checkout, named after its main repository
      dir        - a non-git directory directly under a project root that is not $HOME
      global     - none of these
    The innermost matching path wins; registry beats discovered on a tie. path
    is the matched project's real path (registry/discovered), top and main come
    from git_checkout. Registry aliases apply to the returned name."""
    if not cwd:
        return "global", "global", None, None, None
    real = os.path.realpath(cwd)
    top, main = git_checkout(real)
    reg = reg or {}
    cands = []

    def consider(rp, prio, name, kind):
        if real == rp or real.startswith(rp.rstrip(os.sep) + os.sep):
            cands.append((len(rp), prio, name, rp, kind))
        elif main and top and main == rp:
            cands.append((len(top), prio, name, rp, kind))

    for n, meta in reg.items():
        consider(os.path.realpath(os.path.expanduser(
            meta.get("path") or os.path.join(HOME, "projects", n))), 2, n, "registry")
    for rp, n in discovered_repos().items():
        consider(rp, 1, n, "discovered")
    path = None
    if cands:
        _, _, name, path, kind = max(cands)
    elif os.path.basename(os.path.normpath(cwd)) in reg:
        name, kind = os.path.basename(os.path.normpath(cwd)), "name"
    elif main:
        name, kind = os.path.basename(main), "repo"
    else:
        name, kind = "global", "global"
        ab = os.path.abspath(os.path.expanduser(cwd))
        for root in project_roots():
            if root == HOME or ab == root:
                continue
            if ab.startswith(root + os.sep):
                seg = ab[len(root):].lstrip(os.sep).split(os.sep)[0]
                if seg:
                    name, kind = seg, "dir"
                    break
    hops = 0
    while name in aliases and hops < 5:
        name, hops = aliases[name], hops + 1
    return name, kind, path, top, main
# ---- end of project resolution ------------------------------------------------

project = "global"
try:
    _reg, _aliases = load_registry(REGISTRY)
    project = resolve_project(cwd, _reg, _aliases)[0]
except Exception as e:
    hook_err(1, "resolve project", e)

# ---- pricing ------------------------------------------------------------------
# One price table and one way to count API calls. This block is byte-identical
# in hooks/session-end.sh and in the pricing module that the dashboard and the
# budget report import. The hook ships alone and cannot import that module, so
# it carries the block itself; tests/test_pricing.py fails when the two copies
# differ. Edit both copies together.
#
# $/MTok: (input, output, cache read, 5-minute cache write, 1-hour cache write).
# Source: the claude-api skill bundled with Claude Code 2.1.280, read
# 2026-09-24 (shared/models.md, shared/model-migration.md,
# shared/prompt-caching.md). 5-minute writes are 1.25x input and 1-hour writes
# 2x input on every model. Reads are 0.1x input, except Fable 5.1 ($0.25,
# 0.025x) and Opus 5.5 ($0.20, 0.05x). The table this replaced priced every
# write at 1.25x, Fable 5.1 reads at $1.00, Sonnet 5 at $3/$15, and a whole
# session at its dominant model's rate.
import itertools

PRICES = {
    "claude-fable-5-1":  (10.0, 50.0, 0.25, 12.50, 20.0),
    "claude-fable-5":    (10.0, 50.0, 1.00, 12.50, 20.0),
    "claude-opus-5-5":   (4.0, 20.0, 0.20, 5.00, 8.0),
    "claude-opus-5":     (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-8":   (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-7":   (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-6":   (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-sonnet-5":   (2.0, 10.0, 0.20, 2.50, 4.0),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75, 6.0),
    "claude-haiku-4-5":  (1.0, 5.0, 0.10, 1.25, 2.0),
}
# An id outside the table is priced by family; the Mythos 5.1 cache-read rate
# is unconfirmed, so Mythos takes Fable 5.1's. Anything else: Sonnet 5.
FAMILY = (("fable", "claude-fable-5-1"), ("mythos", "claude-fable-5-1"),
          ("opus", "claude-opus-5"), ("sonnet", "claude-sonnet-5"),
          ("haiku", "claude-haiku-4-5"))


def price_for(model):
    m = (model or "").lower().split("[")[0].strip()
    i = m.find("claude-")
    m = m[i:] if i >= 0 else m
    keys = [k for k in PRICES if m == k or m.startswith(k + "-")]
    if keys:
        return PRICES[max(keys, key=len)]
    for fam, k in FAMILY:
        if fam in m:
            return PRICES[k]
    return PRICES["claude-sonnet-5"]


def call_cost(model, inp, out, cache_read, cache_create, cache_create_1h=0):
    """USD for usage on one model: the 1-hour part of the cache writes at the
    1-hour rate, the rest of them at the 5-minute rate."""
    pin, pout, pcr, p5m, p1h = price_for(model)
    cc1h = min(cache_create_1h, cache_create)
    return (inp * pin + out * pout + cache_read * pcr
            + (cache_create - cc1h) * p5m + cc1h * p1h) / 1e6


def _n(v):
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def note_usage(calls, key, model, usage):
    """Record one transcript line's usage under its message key. Lines of one
    message repeat its usage; keep each field's largest value."""
    cc = usage.get("cache_creation")
    cc = cc if isinstance(cc, dict) else {}
    vals = [_n(usage.get("input_tokens")), _n(usage.get("output_tokens")),
            _n(usage.get("cache_read_input_tokens")),
            _n(usage.get("cache_creation_input_tokens")),
            _n(cc.get("ephemeral_1h_input_tokens"))]
    row = calls.get(key)
    if row is None:
        calls[key] = [model] + vals
    else:
        for i, v in enumerate(vals, 1):
            if v > row[i]:
                row[i] = v


SEQ = itertools.count()


def message_key(obj, msg):
    """The API message id; the request id or line uuid when a line has none."""
    return msg.get("id") or obj.get("requestId") or obj.get("uuid") or ("line", next(SEQ))


def totals(calls):
    """(tokens dict, cost USD, api calls) over deduplicated calls."""
    t = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    cost = 0.0
    for model, i, o, cr, cc, cc1h in calls.values():
        t["input"] += i
        t["output"] += o
        t["cache_read"] += cr
        t["cache_create"] += cc
        cost += call_cost(model, i, o, cr, cc, cc1h)
    return t, cost, len(calls)

def rewrites(calls, order):
    """Keys of the calls that re-wrote a cached prefix, in call order. Of two
    consecutive calls on one model, the later one re-wrote when it read less
    from the cache than the earlier one did: its cached prefix shrank."""
    out, prev = [], None
    for k in order:
        row = calls.get(k)
        if row is None:
            continue
        if prev is not None and row[0] == prev[0] and prev[3] > row[3]:
            out.append(k)
        prev = row
    return out


def rewrite_tokens(calls, order):
    """Cache-write tokens of the calls that rewrites() names."""
    return sum(calls[k][4] for k in rewrites(calls, order))
# ---- end of pricing -----------------------------------------------------------

# ---- main transcript -----------------------------------------------------------
tool_uses = Counter()
error_tools = Counter()
errors = 0
files_touched = Counter()
user_corrections = 0
subagent_count = 0
first_ts = None
last_ts = None
claude_version = None
entrypoint = None
model_counter = Counter()
flags = []
last_assistant_text = None
tail_lines = deque(maxlen=40)   # raw tail for failure-signature scan (P6)
calls = {}                      # message key -> [model, in, out, cache_read, cache_create, 1h part]
ctx_per_call = []               # context size of each API call, in transcript order

# Track tool_use IDs for error attribution
tool_id_to_name = {}
last_bash_commands = []
has_git_commit = False
has_git_push = False
has_deploy = False

try:
    with open(transcript_path) as f:
        for line in f:
            tail_lines.append(line)
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            ts = obj.get("timestamp")
            if ts and not first_ts:
                first_ts = ts
            if ts:
                last_ts = ts

            if not claude_version:
                claude_version = obj.get("version")
            if not entrypoint and obj.get("entrypoint"):
                entrypoint = obj.get("entrypoint")

            msg_type = obj.get("type")

            if msg_type == "assistant":
                msg = obj.get("message", {})
                if isinstance(msg, dict):
                    m = msg.get("model")
                    usage = msg.get("usage")
                    # One API call per message id; "<synthetic>" lines are
                    # Claude Code's own notices, not calls.
                    if isinstance(usage, dict) and m != "<synthetic>":
                        mkey = message_key(obj, msg)
                        if mkey not in calls:
                            if m:
                                model_counter[m] += 1
                            ctx_per_call.append(mkey)
                        note_usage(calls, mkey, m, usage)

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
                    if " ".join(text_parts).strip():
                        last_assistant_text = " ".join(text_parts).strip()
                    full_text = " ".join(text_parts).lower()
                    arch_keywords = ["architecture", "design decision", "trade-off", "tradeoff",
                                     "decided to", "alternative was"]
                    if any(kw in full_text for kw in arch_keywords):
                        if "architecture-discussion" not in flags:
                            flags.append("architecture-discussion")

except Exception as e:
    hook_err(1, "main transcript", e)       # the row is still written, from what was read

tokens, main_cost, api_calls = totals(calls)
ctx = [calls[k][1] + calls[k][3] + calls[k][4] for k in ctx_per_call if k in calls]
first_call_ctx = ctx[0] if ctx else None
peak_ctx = max(ctx) if ctx else None

# The last assistant line: from the helper the Stop gates share when it is
# installed, else from the pass above.
_helper = os.environ.get("LAST_MSG_HELPER", "")
if _helper and os.path.isfile(_helper):
    try:
        _r = subprocess.run([sys.executable, "-I", _helper, transcript_path],
                            capture_output=True, encoding="utf-8", errors="replace",
                            timeout=10)
        if _r.returncode != 0:
            hook_err(_r.returncode, "last-assistant-msg.py", _r.stderr)
        elif _r.stdout.strip():
            last_assistant_text = _r.stdout.strip()
    except Exception as e:
        hook_err(1, "last-assistant-msg.py", e)

# ---- subagent transcripts --------------------------------------------------------
# <transcript dir>/<session>/subagents/, nested levels included (workflow runs
# keep theirs under subagents/workflows/<run>/). Only agent-*.jsonl are agent
# transcripts; a workflow's journal.jsonl carries no usage. Lines without
# "usage" and "assistant" are skipped unparsed: 308 MB of subagent transcripts
# (the largest session on 2026-09-24) read in about 2 s.
sub_calls = {}
try:
    sub_dir = os.path.join(os.path.splitext(transcript_path)[0], "subagents")
    if os.path.isdir(sub_dir):
        for dp, _dn, fns in os.walk(sub_dir):
            for fn in fns:
                if not (fn.startswith("agent-") and fn.endswith(".jsonl")):
                    continue
                try:
                    with open(os.path.join(dp, fn), "rb") as sf:
                        for raw in sf:
                            if b'"usage"' not in raw or b'"assistant"' not in raw:
                                continue
                            try:
                                obj = json.loads(raw)
                            except ValueError:
                                continue
                            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                                continue
                            msg = obj.get("message")
                            if not isinstance(msg, dict):
                                continue
                            usage, m = msg.get("usage"), msg.get("model")
                            if isinstance(usage, dict) and m != "<synthetic>":
                                note_usage(sub_calls, message_key(obj, msg), m, usage)
                except OSError as e:
                    hook_err(1, "subagent transcript " + fn, e)
                    continue
except Exception as e:
    hook_err(1, "subagent transcripts", e)
subagent_tokens, sub_cost, subagent_api_calls = totals(sub_calls)

if not entrypoint:
    entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT") or None
nested = (os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "sdk-cli") or (entrypoint == "sdk-cli")

# Calculate duration
duration_s = 0
if first_ts and last_ts:
    try:
        t1 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        duration_s = int((t2 - t1).total_seconds())
    except Exception as e:
        hook_err(1, "duration from timestamps", e)

# ── Why did this session end? (P6, audit M4) ──────────────────────────────
# failure_reason: the first limit/API-error signature, in this order, found in
# the transcript TAIL. Tail-only keeps sessions that merely DISCUSS limits from
# matching on their own working text; a real kill signature is terminal, so it
# lives in the last lines. The catalog is the subagent auto-resume detector's
# LIMIT_SIGNS, verbatim and in its order (the detector was retired
# 2026-09-24): transcript JSON and rendered text, with the server-transient
# "temporarily limiting requests (not your usage limit) Rate limited" family
# added 2026-06-14.
LIMIT_SIGNS = ('"error":"rate_limit"', '"apiErrorStatus":429', '"status":429',
               "hit your session limit", "usage limit", "rate_limit_error",
               "Overloaded", "overloaded_error",
               "temporarily limiting", "not your usage limit", "Rate limited",
               '"apiErrorStatus":529', '"status":529', '"apiErrorStatus":503', '"status":503',
               "overloaded", "service_unavailable")
failure_reason = None
_signs = LIMIT_SIGNS
_tail_blob = "".join(tail_lines)
failure_reason = next((s for s in _signs if s in _tail_blob), None)

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

# Dominant model: the one that served the most API calls.
model = model_counter.most_common(1)[0][0] if model_counter else None

# Build output
top_files = [f for f, _ in files_touched.most_common(50)]

record = {
    "timestamp": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "session_id": session_id,
    "project": project,
    "cwd": cwd,
    # Capped AT WRITE (8h; longer = idle tmux) so no consumer has to remember
    # the cap; wall_clock_s keeps the raw span for forensics — the 07-22 OOM
    # rows ran to 19 days, which is real wall clock but not work time (P7).
    "duration_s": min(duration_s, 28800),
    "wall_clock_s": duration_s,
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
    "end_reason": os.environ.get("END_REASON") or None,
    "failure_reason": failure_reason,
    "last_assistant_line": " ".join(last_assistant_text.split())[:200] if last_assistant_text else None,
    "flags": flags,
    # Main transcript only, one count per API call; subagents are below.
    "tokens": tokens,
    "estimated_cost_usd": round(main_cost, 2),
    "api_calls": api_calls,
    "first_call_ctx": first_call_ctx,
    "peak_ctx": peak_ctx,
    # Cache writes of main-transcript calls whose cached prefix shrank against
    # the call before on the same model (design audit proposal 3).
    "rewrite_tokens": rewrite_tokens(calls, ctx_per_call),
    "subagent_tokens": subagent_tokens,
    "subagent_cost_usd": round(sub_cost, 2),
    "subagent_api_calls": subagent_api_calls,
    "entrypoint": entrypoint,
    "nested": nested,
}

print(json.dumps(record))
PYEOF
}

# The row goes through flock + tee into the day file. The python's stderr is
# captured, so a crash (or a syntax error after an edit) lands in the
# hook-error log instead of vanishing; its guarded steps log themselves.
{ metrics_err=$( { metrics 3>&- | flock "$OUTFILE.lock" tee -a "$OUTFILE" > /dev/null 3>&-; } 2>&1 1>&3 ); metrics_rc=$?; } 3>&1
[ "$metrics_rc" -eq 0 ] || hook_err "$metrics_rc" "metrics (python | flock tee)" "$metrics_err"

exit 0
