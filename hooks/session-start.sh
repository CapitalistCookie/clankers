#!/usr/bin/env bash
# SessionStart hook: the cold-start brief (contract of 2026-09-24).
#
# additionalContext is at most 1,200 bytes of plain text, in this shape:
#   clanker: <project> · <archetype> · <branch>@<short-sha> · <N> dirty
#   NOW: <STATUS.md "## NOW" section: first ≤800 B, cut at a line boundary;
#        no NOW heading -> first ≤600 B of the file; no STATUS.md -> no line>
#   state: STATUS.md (<bytes> B) · index: <first router file> · alerts: <N> open for this project (`clanker alert list`)
#   git: <git log --oneline -3, one per line>
# Over budget: the NOW block is cut first, then git drops to one line.
# Unregistered cwd: "clanker: unregistered · <basename>". NOW/state key on
# file existence, not on registration.
#
# What this replaced: head -30 STATUS.md + git log -5 + alert lines (19.9 KB
# on one repo), a briefing import whose module never shipped, a memory
# self-heal for the memory dir disabled 2026-08-08, a backgrounded codebase
# indexer that wrote into that dir with its stdout still on this hook's JSON
# channel, and one jq per alert file (9.8 s per start over ~1,100 alerts).
#
# Kept: the /clear hand-off of the previous transcript to session-end.sh
# (telemetry), the P7 heartbeat stub row, and the CLAUDE_ENV_FILE exports of
# CLANKER_PROJECT (read by prompt-check.sh and skill-tracker.sh) and
# CLANKER_ARCHETYPE.
#
# PERF: one python3 interpreter started with -I (isolated: skips the user
# site's .pth files, the bulk of interpreter start-up here), no jq, two git
# calls, and a single pass over the alert dir for the count.
# FAIL-OPEN: every piece is guarded; whatever lines succeeded are emitted and
# the script always exits 0.
set -uo pipefail

CLANKER_HOOK_INPUT="$(cat 2>/dev/null || true)"
CLANKER_HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
export CLANKER_HOOK_INPUT CLANKER_HOOK_DIR

python3 -I - <<'PY' 2>/dev/null || true
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


def load_registry(path):
    """{name: {archetype, path}} from the registry's `projects:` block, or None
    when there is no registry file. registry.py writes block-style YAML, read
    here line by line: -I keeps PyYAML (user site) out, and this is far cheaper."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    projects, in_proj, cur, name_ind, key_ind = {}, False, None, None, None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        ind = len(raw) - len(raw.lstrip(" "))
        if ind == 0:
            in_proj, cur, name_ind, key_ind = (s == "projects:"), None, None, None
            continue
        if not in_proj:
            continue
        if name_ind is None:
            name_ind = ind
        if ind == name_ind:
            m = re.match(r"""^(['"]?)(.+?)\1:(?:\s+.*)?$""", s)
            cur, key_ind = (m.group(2) if m else None), None
            if cur is not None:
                projects[cur] = {}
            continue
        if cur is None or ind < name_ind:
            continue
        if key_ind is None:
            key_ind = ind
        if ind != key_ind:
            continue                                  # continuation of a long value
        m = re.match(r"^(archetype|path):\s*(.*)$", s)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
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
        except Exception:
            pass
    return projects


def match_project(reg, project_dir):
    """(name, root) of the registered project containing project_dir: the longest
    registered path (explicit `path:` or ~/projects/<name>) wins; else a name
    equal to the dir's basename, root unknown; else (None, None)."""
    real = os.path.realpath(project_dir)
    best, best_root = None, None
    for name, meta in reg.items():
        rp = os.path.realpath(os.path.expanduser(
            meta.get("path") or os.path.join(HOME, "projects", name)))
        if real == rp or real.startswith(rp.rstrip(os.sep) + os.sep):
            if best_root is None or len(rp) > len(best_root):
                best, best_root = name, rp
    if best is None:
        base = os.path.basename(os.path.normpath(project_dir))
        if base in reg:
            best = base
    return best, best_root


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
except ValueError:
    hook_in = {}
cwd = str(hook_in.get("cwd") or "")
source = str(hook_in.get("source") or "")
session_id = str(hook_in.get("session_id") or "")
project_dir = E.get("CLAUDE_PROJECT_DIR") or cwd
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
    except Exception:
        pass

# --- 1. project + archetype -----------------------------------------------------
reg, project, root = None, None, None
try:
    reg = load_registry(REGISTRY)
    if reg:
        project, root = match_project(reg, project_dir)
except Exception:
    project, root = None, None
base = os.path.basename(os.path.normpath(project_dir)) if project_dir else ""
name = project or base
archetype = "unknown"
if project:
    archetype = (reg.get(project) or {}).get("archetype") or "unknown"
root = root or project_dir

# --- 2. heartbeat stub row (P7): a session must exist in telemetry before it
# ends; SessionEnd's full row supersedes it (last write wins in consumers). ---
if session_id:
    try:
        stub_project = project
        if not stub_project:
            pr = os.path.join(HOME, "projects") + os.sep
            stub_project = cwd.split(pr)[-1].split(os.sep)[0] if pr in cwd else "global"
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
                }) + "\n")
    except Exception:
        pass

# --- 3. env vars for downstream hooks -------------------------------------------
envf = E.get("CLAUDE_ENV_FILE")
if envf and name and os.path.isfile(REGISTRY):
    try:
        with open(envf, "a") as f:
            f.write(f"export CLANKER_PROJECT={shlex.quote(name)}\n")
            f.write(f"export CLANKER_ARCHETYPE={shlex.quote(archetype)}\n")
    except Exception:
        pass

# --- 4. the brief ---------------------------------------------------------------
clanker_line = "clanker: " + (f"{project} · {archetype}" if project
                              else f"unregistered · {base or '?'}")
log3 = []
try:
    st = git(root, "status", "--porcelain=v2", "--branch")
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
except Exception:
    pass

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
except Exception:
    pass

index = "none"
try:
    index = next((f for f in INDEX_FILES if os.path.isfile(os.path.join(root, f))), "none")
except Exception:
    pass
try:
    n_alerts = f"{count_alerts(name)} open for this project" if name else "0 open for this project"
except Exception:
    n_alerts = "? (unreadable)"
state_line = ("state: " + (f"STATUS.md ({status_size} B)" if status_size is not None
                           else "no STATUS.md")
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
except Exception:
    ctx = clip("\n".join(x for x in (clanker_line, state_line) if x), BUDGET)

print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                         "additionalContext": ctx}}))
PY

exit 0
