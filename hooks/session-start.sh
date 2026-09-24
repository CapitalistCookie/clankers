#!/usr/bin/env bash
# SessionStart hook: the cold-start brief (contract of 2026-09-24).
#
# additionalContext is at most 1,200 bytes of plain text, in this shape:
#   clanker: <project> · <archetype> · <branch>@<short-sha> · <N> dirty
#   NOW: <STATUS.md "## NOW" section: first ≤800 B, cut at a line boundary;
#        no NOW heading -> first ≤600 B of the file; no STATUS.md -> no line>
#   state: STATUS.md (<bytes> B) · NOW edited <N> commits / <age> ago · index: <first router file> · alerts: <N> open for this project (`clanker alert list`)
#          (the NOW part: git's last commit that touched STATUS.md, how many
#          commits HEAD is past it and how long ago it was; "+ uncommitted
#          edits" when the file changed since; "NOW never committed" when no
#          commit has it; nothing outside git)
#   git: <git log --oneline -3, one per line>
# Over budget: the NOW block is cut first, then git drops to one line.
# Registered: a `projects:` entry in the registry, or a git repo directly under
# a project root (CLANKER_PROJECT_ROOTS, default ~/projects), which is how
# clanker's Registry.projects discovers repos; a discovered repo takes its yaml
# archetype when it has one, else "discovered". Anything else:
# "clanker: unregistered · <basename>". NOW/state key on file existence, not
# on registration.
#
# Nested runs (2026-09-24): every scripted `claude -p` carries
# CLAUDE_CODE_ENTRYPOINT=sdk-cli (Claude Code sets it for any non-interactive
# run, even one an interactive session launched), and hooks inherit it. Such a
# run gets its stub row tagged nested: true and no brief at all;
# CLANKER_INJECT_NESTED=1 prints the brief anyway.
#
# What this replaced: head -30 STATUS.md + git log -5 + alert lines (19.9 KB
# on one repo), a briefing import whose module never shipped, a memory
# self-heal for the memory dir disabled 2026-08-08, a backgrounded codebase
# indexer that wrote into that dir with its stdout still on this hook's JSON
# channel, and one jq per alert file (9.8 s per start over ~1,100 alerts).
#
# Kept: the /clear hand-off of the previous transcript to session-end.sh
# (telemetry), the P7 heartbeat stub row, and the CLAUDE_ENV_FILE exports of
# CLANKER_PROJECT (read by skill-tracker.sh) and CLANKER_ARCHETYPE.
#
# PERF: one python3 interpreter started with -I (isolated: skips the user
# site's .pth files, the bulk of interpreter start-up here), no jq, four git
# calls (status, log -3, and log -1 + rev-list --count for STATUS.md), and a
# single pass over the alert dir for the count.
# FAIL-OPEN: every piece is guarded; whatever lines succeeded are emitted and
# the script always exits 0.
# FAIL-VISIBLE (2026-09-24): a guarded step that fails, and a python run that
# exits nonzero, append a row to the hook-error log (block below) instead of
# vanishing: the briefing import stayed dead for two months unseen.
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

CLANKER_HOOK_INPUT="$(cat 2>/dev/null || true)"
CLANKER_HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
export CLANKER_HOOK_INPUT CLANKER_HOOK_DIR
HOOK_ERR_IN="$CLANKER_HOOK_INPUT"

brief() {
python3 -I - <<'PY'
# ---- hook-error log: the same block in every clanker python hook ----------------
# A failure that the hook swallows (the hook stays fail-open for the session)
# appends one JSON line {ts, hook, session_id, cwd, rc, stderr_tail} to
# $CLANKER_DATA/raw/health/hook-errors-<UTC day>.jsonl (default /data/clanker),
# where `clanker doctor --harness` counts it. hook_err never raises, never
# blocks and never writes to stdout. Usage: hook_err(rc, step, error text or
# exception); stderr_tail is "<step>: " plus the end of the text (of the
# traceback, for an exception), 300 characters at most. Set
# HOOK_ERR["session_id"] and HOOK_ERR["cwd"] once the payload is parsed.
import os as _he_os
import sys as _he_sys

HOOK_ERR = {"hook": (_he_os.path.basename(_he_sys.argv[0]) if _he_sys.argv
                     and _he_sys.argv[0] not in ("", "-", "-c") else "?"),
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

HOOK_ERR["hook"] = "session-start.sh"
import json, os, re, shlex, subprocess, sys, time

BUDGET, NOW_MAX, HEAD_MAX, NOW_MIN = 1200, 800, 600, 120
INDEX_FILES = ("INDEX.md", "00-START-HERE.md", "START-HERE.md", "ROUTER.md",
               "RESUME.md", "docs/ROUTING.md", "docs/INDEX.md", "docs/README.md",
               "STATE.md")
ELL = " …"
E = os.environ
HOME = os.path.expanduser("~")
DATA = E.get("CLANKER_DATA") or "/data/clanker"
REGISTRY = E.get("CLANKER_REGISTRY") or os.path.join(HOME, "projects", ".clanker.yaml")
HOOK_DIR = E.get("CLANKER_HOOK_DIR") or ""
NOW_RE = re.compile(r"^##[ \t]+NOW\b[ \t]*(.*)$", re.I)
H12_RE = re.compile(r"^#{1,2}[ \t]")
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T]+(\d{1,2}):(\d{2}))?")


def nbytes(s):
    return len(s.encode("utf-8"))


def clip(s, limit):
    """s cut to <= limit bytes, at a word boundary when one is near, plus an ellipsis."""
    if nbytes(s) <= limit:
        return s
    room = limit - nbytes(ELL)
    if room <= 0:
        return ""
    cut = s.encode("utf-8")[:room].decode("utf-8", "ignore")
    sp = cut.rfind(" ")
    if sp > len(cut) // 2:
        cut = cut[:sp]
    return cut.rstrip() + ELL


def fit(lines, limit, first_body=0):
    """Whole lines while they fit in `limit` bytes (newline-joined). The first
    line that does not fit ends the block; only when no body line has made it in
    yet is that line clipped instead (a single 1.1 KB paragraph would otherwise
    leave the block empty)."""
    out, used = [], 0
    for ln in lines:
        sep = 1 if out else 0
        if used + sep + nbytes(ln) <= limit:
            out.append(ln)
            used += sep + nbytes(ln)
            continue
        if len(out) <= first_body and limit - used - sep >= 40:
            c = clip(ln, limit - used - sep)
            if c:
                out.append(c)
        break
    return out


def now_section(text):
    """(lines, first_body) of the `## NOW` section, or None when there is no such
    heading. Several stacked NOW headings: the one whose heading carries the
    latest date wins (ties and undated headings: the first). The heading's own
    suffix, usually its date, leads the block."""
    lines = text.splitlines()
    fence, heads, h12 = False, [], []
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        if s.startswith("```") or s.startswith("~~~"):
            fence = not fence
            continue
        if fence or not H12_RE.match(ln):
            continue
        h12.append(i)
        m = NOW_RE.match(ln)
        if m:
            heads.append((i, m.group(1)))
    if not heads:
        return None
    dated = []
    for n, (i, suffix) in enumerate(heads):
        m = DATE_RE.search(suffix)
        if m:
            dated.append((tuple(int(x) if x else -1 for x in m.groups()), -n, i, suffix))
    start, suffix = (max(dated)[2:] if dated else heads[0])
    end = next((j for j in h12 if j > start), len(lines))
    suffix = suffix.strip().lstrip(":—–- \t").strip()
    body = [ln.rstrip() for ln in lines[start + 1:end] if ln.strip()]
    return ([suffix] if suffix else []) + body, (1 if suffix else 0)


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


def count_alerts(name):
    """Open alerts of this project — `project` field, or a message starting with
    "<name>:" — in ONE pass; files that never mention the name are not parsed."""
    adir = os.path.join(DATA, "alerts")
    if not os.path.isdir(adir):
        return 0
    needle, n = name.encode("utf-8"), 0
    with os.scandir(adir) as it:
        for e in it:
            if not e.name.endswith(".json"):
                continue
            try:
                with open(e.path, "rb") as f:
                    raw = f.read(1 << 16)
            except OSError:
                continue
            if needle not in raw:
                continue
            try:
                a = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(a, dict):
                continue
            if str(a.get("status") or "active").lower() in ("resolved", "dismissed", "closed"):
                continue
            if a.get("project") == name or str(a.get("message") or "").startswith(name + ":"):
                n += 1
    return n


GIT_ENV = {k: v for k, v in E.items() if k not in (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR", "GIT_NAMESPACE", "CLANKER_HOOK_INPUT")}
GIT_ENV["GIT_OPTIONAL_LOCKS"] = "0"      # status must never take index.lock from a live repo


def git(where, *args):
    r = subprocess.run(["git", "-C", where, *args], capture_output=True, timeout=3,
                       encoding="utf-8", errors="replace", env=GIT_ENV,
                       stdin=subprocess.DEVNULL)
    return r.stdout if r.returncode == 0 else None


# --- hook input -------------------------------------------------------------
try:
    hook_in = json.loads(E.get("CLANKER_HOOK_INPUT") or "{}")
    hook_in = hook_in if isinstance(hook_in, dict) else {}
except ValueError as e:
    hook_err(1, "parse hook input", e)
    hook_in = {}
cwd = str(hook_in.get("cwd") or "")
source = str(hook_in.get("source") or "")
session_id = str(hook_in.get("session_id") or "")
HOOK_ERR.update(session_id=session_id, cwd=cwd)
project_dir = E.get("CLAUDE_PROJECT_DIR") or cwd
entrypoint = E.get("CLAUDE_CODE_ENTRYPOINT") or ""
nested = entrypoint == "sdk-cli"
quiet = nested and not E.get("CLANKER_INJECT_NESTED")
if not project_dir:
    try:
        project_dir = os.getcwd()
    except OSError:
        project_dir = ""

# --- 0. /clear: hand the PREVIOUS session's transcript to session-end.sh -----
# The input's session_id/transcript are the NEW session's; the old transcript
# is the second-newest .jsonl in this project's Claude Code dir. KNOWN
# HEURISTIC (audit L3): with concurrent sessions in one dir it can pick a live
# sibling (harmless: its SessionEnd rewrites the row, last write wins).
if source == "clear":
    try:
        tdir = os.path.join(HOME, ".claude", "projects",
                            re.sub(r"[^A-Za-z0-9]", "-", project_dir))
        ts = sorted((os.path.join(tdir, f) for f in os.listdir(tdir) if f.endswith(".jsonl")),
                    key=os.path.getmtime, reverse=True)
        old = ts[1] if len(ts) >= 2 else (ts[0] if ts else "")
        end_hook = os.path.join(HOOK_DIR, "session-end.sh")
        if old and os.path.isfile(end_hook):
            old_id = os.path.basename(old)[:-len(".jsonl")]
            env = {k: v for k, v in E.items() if k != "CLANKER_HOOK_INPUT"}
            env.update(TRANSCRIPT=old, SESSION_ID=old_id, CWD=cwd)
            p = subprocess.Popen(["bash", end_hook], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=env, start_new_session=True)
            p.stdin.write(json.dumps({"session_id": old_id, "transcript_path": old,
                                      "cwd": cwd}).encode())
            p.stdin.close()
    except Exception as e:
        hook_err(1, "clear: hand the previous transcript to session-end", e)

# --- 1. project + archetype -----------------------------------------------------
reg, aliases = None, {}
resolved, kind, path, top, main = "global", "global", None, None, None
try:
    reg, aliases = load_registry(REGISTRY)
    resolved, kind, path, top, main = resolve_project(project_dir, reg, aliases)
except Exception as e:
    hook_err(1, "resolve project", e)
base = os.path.basename(os.path.normpath(project_dir)) if project_dir else ""
project = resolved if kind in ("registry", "discovered", "name") else None
name = project or base
archetype = "unknown"
if project:
    archetype = ((reg or {}).get(project) or {}).get("archetype") or (
        "discovered" if kind == "discovered" else "unknown")
if path and top and main == path:
    root = top            # the project's own checkout: a linked worktree keeps its tree
elif path:
    root = path
else:
    root = top or project_dir

# --- 2. heartbeat stub row (P7): a session must exist in telemetry before it
# ends; SessionEnd's full row supersedes it (last write wins in consumers). ---
if session_id:
    try:
        stub_project = resolved          # session-end.sh names the final row the same way
        sdir = os.path.join(DATA, "raw", "sessions")
        os.makedirs(sdir, exist_ok=True)
        out = os.path.join(sdir, time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl")
        import fcntl
        with open(out + ".lock", "a") as lk:          # the lock file session-end flocks
            deadline = time.monotonic() + 1.0         # bounded: session-end holds it while it parses
            while True:
                try:
                    fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.02)
            with open(out, "a") as f:
                f.write(json.dumps({
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "session_id": session_id,
                    "project": stub_project,
                    "cwd": cwd,
                    "outcome": "open",
                    "source": source or None,
                    "entrypoint": entrypoint or None,
                    "nested": nested,
                }) + "\n")
    except Exception as e:
        hook_err(1, "heartbeat stub row", e)

# --- 3. env vars for downstream hooks -------------------------------------------
envf = E.get("CLAUDE_ENV_FILE")
if envf and name and os.path.isfile(REGISTRY):
    try:
        with open(envf, "a") as f:
            f.write(f"export CLANKER_PROJECT={shlex.quote(name)}\n")
            f.write(f"export CLANKER_ARCHETYPE={shlex.quote(archetype)}\n")
    except Exception as e:
        hook_err(1, "CLAUDE_ENV_FILE exports", e)

# --- 4. the brief (none for a nested run) ---------------------------------------
if quiet:
    sys.exit(0)
clanker_line = "clanker: " + (f"{project} · {archetype}" if project
                              else f"unregistered · {base or '?'}")
log3, st = [], None
try:
    # relativePaths off: porcelain v2 paths are then relative to the repo top
    # (the NOW staleness check below finds STATUS.md by that path)
    st = git(root, "-c", "status.relativePaths=false", "status", "--porcelain=v2", "--branch")
    if st is not None:
        branch, sha, dirty = "?", "?", 0
        for ln in st.splitlines():
            if ln.startswith("# branch.oid "):
                oid = ln[len("# branch.oid "):].strip()
                sha = "initial" if oid.startswith("(") else oid[:7]
            elif ln.startswith("# branch.head "):
                h = ln[len("# branch.head "):].strip()
                branch = "detached" if h.startswith("(") else h
            elif ln and not ln.startswith("#"):
                dirty += 1
        clanker_line += f" · {branch}@{sha} · {dirty} dirty"
        lg = git(root, "log", "--oneline", "--no-decorate", "--no-color", "-3")
        log3 = [ln.rstrip() for ln in (lg or "").splitlines() if ln.strip()][:3]
except Exception as e:
    hook_err(1, "git status/log", e)

now_src, first_body, now_limit, now_lines, status_size = [], 0, 0, [], None
try:
    sp = os.path.join(root, "STATUS.md")
    if os.path.isfile(sp):
        with open(sp, "rb") as f:
            raw = f.read()
        status_size = len(raw)
        text = raw.decode("utf-8", "replace")
        sec = now_section(text)
        if sec is not None:
            (now_src, first_body), now_limit = sec, NOW_MAX
        else:
            now_src = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
            now_limit = HEAD_MAX
        now_lines = fit(now_src, now_limit, first_body)
except Exception as e:
    hook_err(1, "STATUS.md NOW", e)

# NOW staleness (design audit proposal 10): the last commit that touched
# STATUS.md, how many commits HEAD is past it, and how long ago it was.
now_age = ""
try:
    if status_size is not None and st is not None:
        rel = os.path.relpath(os.path.join(root, "STATUS.md"), top or root)
        st_lines = st.splitlines()
        if "? " + rel in st_lines:                        # untracked
            now_age = "NOW never committed"
        else:
            lg1 = git(root, "log", "-1", "--format=%H%x09%cr", "--", "STATUS.md")
            if lg1 is not None and not lg1.strip():
                now_age = "NOW never committed"
            elif lg1:
                h, cr = lg1.strip().split("\t", 1)
                n = (git(root, "rev-list", "--count", f"{h}..HEAD") or "").strip()
                if n.isdigit():
                    now_age = f"NOW edited {n} commit{'' if n == '1' else 's'} / {cr}"
                    if any(ln[:2] in ("1 ", "2 ", "u ")
                           and ln.split("\t")[0].rsplit(" ", 1)[-1] == rel for ln in st_lines):
                        now_age += " + uncommitted edits"
except Exception as e:
    hook_err(1, "NOW staleness (git log / rev-list)", e)

index = "none"
try:
    index = next((f for f in INDEX_FILES if os.path.isfile(os.path.join(root, f))), "none")
except Exception as e:
    hook_err(1, "index file", e)
try:
    n_alerts = f"{count_alerts(name)} open for this project" if name else "0 open for this project"
except Exception as e:
    hook_err(1, "count alerts", e)
    n_alerts = "? (unreadable)"
state_line = ("state: " + (f"STATUS.md ({status_size} B)" if status_size is not None
                           else "no STATUS.md")
              + (f" · {now_age}" if now_age else "")
              + f" · index: {index} · alerts: {n_alerts} (`clanker alert list`)")


def render(now, git_lines):
    out = [clanker_line]
    if now:
        out.append("NOW: " + "\n".join(now))
    out.append(state_line)
    if git_lines:
        out.append("git: " + "\n".join(git_lines))
    return "\n".join(out)


def now_in(git_lines):
    """The NOW block cut to what `git_lines` leave of the budget; [] below a
    useful minimum (a heading and a sentence)."""
    room = BUDGET - nbytes(render(None, git_lines)) - len("\nNOW: ")
    return fit(now_src, min(room, now_limit), first_body) if room >= NOW_MIN else []


try:
    ctx = render(now_lines, log3)
    if nbytes(ctx) > BUDGET and now_lines:            # 1st: cut the NOW block
        now_lines = now_in(log3)
        ctx = render(now_lines, log3)
    if nbytes(ctx) > BUDGET or (now_src and not now_lines and len(log3) > 1):
        # 2nd: git to one line. NOW, already cut to nothing, takes back the
        # room this frees: paragraph-long commit subjects (one repo's run to
        # ~480 B each) must not leave a brief with git and no NOW.
        now_lines = now_in(log3[:1]) if now_src else []
        ctx = render(now_lines, log3[:1])
    if nbytes(ctx) > BUDGET:                          # last resort: hard cap
        cut = ctx.encode("utf-8")[:BUDGET].decode("utf-8", "ignore")
        ctx = cut[:cut.rfind("\n")] if "\n" in cut else clip(ctx, BUDGET)
except Exception as e:
    hook_err(1, "render the brief", e)
    ctx = clip("\n".join(x for x in (clanker_line, state_line) if x), BUDGET)

print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                         "additionalContext": ctx}}))
PY
}

# The brief's JSON goes to stdout; the python's stderr is captured, so a crash
# (or a syntax error after an edit) lands in the hook-error log. Its guarded
# steps log themselves.
{ brief_err=$(brief 2>&1 1>&3 3>&-); brief_rc=$?; } 3>&1
[ "$brief_rc" -eq 0 ] || hook_err "$brief_rc" "brief (python)" "$brief_err"

exit 0
