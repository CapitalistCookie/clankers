"""Harness lint — `clanker doctor --harness`.

The always-loaded harness (global settings, hooks, skills, agents, CLAUDE.md
files, STATUS.md NOW sections, rules) regrows unless something counts it. Each
check yields findings of one line each: the check, the offending path, what is
wrong. `run()` returns them; `main()` prints a table (or JSON) and returns 1
when there is any finding.

Checks:
  a global-project-ref  project names or repo paths in ~/.claude settings,
                        hooks, skills, agents, CLAUDE.md and the statusline
                        script (full-line code comments are skipped)
  b skill-description   a SKILL.md `description:` over 300 chars
                        (registered repos and ~/.claude/skills)
  c claude-md-size      a CLAUDE.md over 6,144 B (global and per repo)
  d status-now          a STATUS.md without a `## NOW` section, or a NOW
                        block over 800 B (the start brief cuts it there)
  e hook-timeout        a hook entry with a timeout over 60 s
  f hook-script         a hook command whose script is missing or not
                        executable ($CLAUDE_PROJECT_DIR = the repo)
  g rules-scope         a .claude/rules/*.md without `paths:`, or with a
                        glob that matches no file in the repo
  h alerts-dir          more than 200 files in $CLANKER_DATA/alerts
  i global-env          project-specific env keys in the global settings
  j hook-errors         hook-error rows logged in the last 24 h
                        ($CLANKER_DATA/raw/health/hook-errors-*.jsonl)

Operator config (repo law 5) is the registry file's top-level `harness_lint:`
mapping; every key is optional:

  harness_lint:
    hook_timeout_allow: [governance-gates-autorun.sh]  # command substrings allowed over 60 s
    name_allow: [some-project]      # project names allowed in global files
    env_prefixes: [PWB_, RESEARCH_] # env key prefixes that mark a project
    skip_repos: [some-project]      # repos this lint does not read

The clanker repo itself and the ~/.claude repo are never "foreign" projects in
check a: the harness names itself.
"""

from __future__ import annotations

import fnmatch
import glob
import json
import os
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

SKILL_DESC_MAX = 300
CLAUDE_MD_MAX = 6144
NOW_MAX = 800
HOOK_TIMEOUT_MAX = 60
ALERTS_MAX = 200
HOOK_ERRORS_WINDOW_H = 24
MAX_SCAN_BYTES = 1_000_000
DEFAULT_ENV_PREFIXES = ("PWB_", "RESEARCH_")

CHECKS = [
    ("a", "global-project-ref", "project names or repo paths in global harness files"),
    ("b", "skill-description", f"skill description over {SKILL_DESC_MAX} chars"),
    ("c", "claude-md-size", f"CLAUDE.md over {CLAUDE_MD_MAX:,} B"),
    ("d", "status-now", f"STATUS.md without ## NOW, or NOW over {NOW_MAX} B"),
    ("e", "hook-timeout", f"hook timeout over {HOOK_TIMEOUT_MAX} s"),
    ("f", "hook-script", "hook script missing or not executable"),
    ("g", "rules-scope", "rule without paths:, or a dead glob"),
    ("h", "alerts-dir", f"alerts dir over {ALERTS_MAX} files"),
    ("i", "global-env", "project-specific env key in global settings"),
    ("j", "hook-errors", f"hook errors in the last {HOOK_ERRORS_WINDOW_H} h"),
]
CHECK_NAMES = {cid: name for cid, name, _ in CHECKS}

REPO_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_INTERPRETERS = {"bash", "sh", "zsh", "dash", "python", "python3", "node", "deno", "bun",
                 "perl", "ruby", "uv", "uvx"}
_CODE_EXT = {".sh", ".bash", ".zsh", ".py"}
_NOW_RE = re.compile(r"^##[ \t]+NOW\b[ \t]*(.*)$", re.I)
_H12_RE = re.compile(r"^#{1,2}[ \t]")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T]+(\d{1,2}):(\d{2}))?")
_REPO_PATH_RE = re.compile(r"(?:~|\$HOME|\$\{HOME\}|/home/[A-Za-z0-9._-]+)/projects/([A-Za-z0-9._-]+)")


@dataclass
class Finding:
    check: str        # check id letter
    path: str
    message: str

    @property
    def name(self) -> str:
        return CHECK_NAMES.get(self.check, self.check)


# ─── inputs ──────────────────────────────────────────────────────────────────

def default_claude_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def default_registry_path() -> str:
    return os.environ.get("CLANKER_REGISTRY", os.path.expanduser("~/projects/.clanker.yaml"))


def default_data_dir() -> str:
    return os.environ.get("CLANKER_DATA", "/data/clanker")


def load_config(registry_path: str) -> dict:
    """The `harness_lint:` block of the registry file, with defaults."""
    cfg = {}
    try:
        import yaml
        with open(registry_path) as f:
            cfg = (yaml.safe_load(f) or {}).get("harness_lint") or {}
    except (OSError, ImportError, ValueError):
        cfg = {}
    except Exception:
        cfg = {}
    return {
        "hook_timeout_allow": [str(x) for x in cfg.get("hook_timeout_allow") or []],
        "name_allow": [str(x) for x in cfg.get("name_allow") or []],
        "env_prefixes": [str(x) for x in (cfg.get("env_prefixes") or DEFAULT_ENV_PREFIXES)],
        "skip_repos": [str(x) for x in cfg.get("skip_repos") or []],
    }


def load_projects(registry_path: str) -> dict:
    """{name: path} for every registered (or discovered) repo that exists on disk."""
    from registry import Registry
    reg = Registry(registry_path)
    out = {}
    for name in reg.projects:
        path = reg.get_path(name)
        if path and os.path.isdir(path):
            out[name] = path
    return out


def _read(path: str) -> str | None:
    try:
        if os.path.getsize(path) > MAX_SCAN_BYTES:
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if b"\0" in raw[:4096]:
        return None
    return raw.decode("utf-8", "replace")


def _tilde(path: str, home: str) -> str:
    return "~" + path[len(home):] if home and (path == home or path.startswith(home + "/")) else path


def frontmatter(text: str) -> dict | None:
    """The YAML frontmatter of a markdown file as a dict, or None."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    block = text[3:end].strip("\n")
    try:
        import yaml
        data = yaml.safe_load(block)
        return data if isinstance(data, dict) else {}
    except Exception:
        data = {}
        for line in block.splitlines():
            m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
            if m:
                data[m.group(1)] = m.group(2).strip().strip("'\"")
        return data


# ─── check a: project references in global files ────────────────────────────

def global_files(claude_dir: str) -> list[str]:
    """The global harness files check a reads."""
    files = []
    for name in ("settings.json", "settings.local.json", "CLAUDE.md"):
        p = os.path.join(claude_dir, name)
        if os.path.isfile(p):
            files.append(p)
    settings = _load_json(os.path.join(claude_dir, "settings.json")) or {}
    status = (settings.get("statusLine") or {}).get("command") or ""
    for tok in status.split():
        tok = os.path.expanduser(tok)
        if os.path.isfile(tok) and tok not in files:
            files.append(tok)
    seen = set()
    for sub in ("hooks", "skills", "agents"):
        root = os.path.join(claude_dir, sub)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            real = os.path.realpath(dirpath)
            if real in seen:
                dirnames[:] = []
                continue
            seen.add(real)
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith(".") and d != "__pycache__")
            for fn in sorted(filenames):
                if fn.startswith(".") or ".bak" in fn or fn.endswith((".pyc", ".log", ".lock")):
                    continue
                files.append(os.path.join(dirpath, fn))
    return files


def _strip_comments(path: str, text: str) -> str:
    ext = os.path.splitext(path)[1]
    if ext in _CODE_EXT or text.startswith("#!"):
        return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    if ext in (".js", ".ts", ".mjs"):
        return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))
    if ext == ".md":
        return re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return text


def _own_projects(projects: dict, claude_dir: str) -> set:
    own_paths = {REPO_ROOT, os.path.realpath(claude_dir)}
    return {n for n, p in projects.items() if os.path.realpath(p) in own_paths}


def check_global_refs(claude_dir: str, projects: dict, cfg: dict, home: str) -> list[Finding]:
    skip = _own_projects(projects, claude_dir) | set(cfg["name_allow"])
    names = sorted((n for n in projects if n not in skip and len(n) >= 3), key=len, reverse=True)
    name_res = [(n, re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(n) + r"(?![A-Za-z0-9_-])"))
                for n in names]
    own_paths = {REPO_ROOT, os.path.realpath(claude_dir)}
    path_needles = []
    for n, p in projects.items():
        if n in skip or os.path.realpath(p) in own_paths:
            continue
        path_needles.append(p)
        if home and p.startswith(home + "/"):
            path_needles.append("~" + p[len(home):])
    own_names = {os.path.basename(p) for p in own_paths}
    out = []
    for f in global_files(claude_dir):
        text = _read(f)
        if not text:
            continue
        text = _strip_comments(f, text)
        hit_names = [n for n, rx in name_res if rx.search(text)]
        hit_paths = sorted({m.group(0) for m in _REPO_PATH_RE.finditer(text)
                            if not m.group(1).startswith(".")
                            and m.group(1) not in own_names and m.group(1) not in skip})
        hit_paths += sorted({p for p in path_needles
                             if p not in hit_paths and re.search(re.escape(p) + r"(?![A-Za-z0-9_.-])", text)})
        if not hit_names and not hit_paths:
            continue
        parts = []
        if hit_names:
            parts.append("names: " + ", ".join(hit_names[:6]) + (f" (+{len(hit_names) - 6})" if len(hit_names) > 6 else ""))
        if hit_paths:
            parts.append("paths: " + ", ".join(hit_paths[:3]) + (f" (+{len(hit_paths) - 3})" if len(hit_paths) > 3 else ""))
        out.append(Finding("a", f, "; ".join(parts)))
    return out


# ─── checks b, c, d: skills, CLAUDE.md, STATUS.md ────────────────────────────

def skill_files(claude_dir: str, projects: dict) -> list[str]:
    files = sorted(glob.glob(os.path.join(claude_dir, "skills", "*", "SKILL.md")))
    for name in sorted(projects):
        files += sorted(glob.glob(os.path.join(projects[name], ".claude", "skills", "*", "SKILL.md")))
    out, seen = [], set()
    for f in files:
        r = os.path.realpath(f)
        if r not in seen:
            seen.add(r)
            out.append(f)
    return out


def skill_description(path: str) -> str | None:
    text = _read(path)
    if text is None:
        return None
    fm = frontmatter(text) or {}
    d = fm.get("description")
    return None if d is None else " ".join(str(d).split())


def check_skill_descriptions(claude_dir: str, projects: dict) -> list[Finding]:
    out = []
    for f in skill_files(claude_dir, projects):
        d = skill_description(f)
        if d is not None and len(d) > SKILL_DESC_MAX:
            out.append(Finding("b", f, f"description {len(d)} chars (> {SKILL_DESC_MAX})"))
    return out


def check_claude_md(claude_dir: str, projects: dict) -> list[Finding]:
    cands = [os.path.join(claude_dir, "CLAUDE.md")]
    for name in sorted(projects):
        cands += [os.path.join(projects[name], "CLAUDE.md"),
                  os.path.join(projects[name], ".claude", "CLAUDE.md")]
    out, seen = [], set()
    for f in cands:
        r = os.path.realpath(f)
        if r in seen or not os.path.isfile(f):
            continue
        seen.add(r)
        size = os.path.getsize(f)
        if size > CLAUDE_MD_MAX:
            out.append(Finding("c", f, f"{size:,} B (> {CLAUDE_MD_MAX:,})"))
    return out


def now_block(text: str) -> str | None:
    """The `## NOW` block as the start brief renders it (heading suffix, then
    the non-blank body lines), or None when there is no NOW heading."""
    lines = text.splitlines()
    fence, heads, h12 = False, [], []
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        if s.startswith("```") or s.startswith("~~~"):
            fence = not fence
            continue
        if fence or not _H12_RE.match(ln):
            continue
        h12.append(i)
        m = _NOW_RE.match(ln)
        if m:
            heads.append((i, m.group(1)))
    if not heads:
        return None
    dated = []
    for n, (i, suffix) in enumerate(heads):
        m = _DATE_RE.search(suffix)
        if m:
            dated.append((tuple(int(x) if x else -1 for x in m.groups()), -n, i, suffix))
    start, suffix = max(dated)[2:] if dated else heads[0]
    end = next((j for j in h12 if j > start), len(lines))
    suffix = suffix.strip().lstrip(":—–- \t").strip()
    body = [ln.rstrip() for ln in lines[start + 1:end] if ln.strip()]
    return "\n".join(([suffix] if suffix else []) + body)


def check_status_now(projects: dict) -> list[Finding]:
    out = []
    for name in sorted(projects):
        f = os.path.join(projects[name], "STATUS.md")
        text = _read(f) if os.path.isfile(f) else None
        if text is None:
            continue
        block = now_block(text)
        if block is None:
            out.append(Finding("d", f, "no `## NOW` section"))
        else:
            n = len(block.encode("utf-8"))
            if n > NOW_MAX:
                out.append(Finding("d", f, f"## NOW is {n:,} B (> {NOW_MAX}; the start brief cuts it)"))
    return out


# ─── checks e, f: hooks ──────────────────────────────────────────────────────

def _load_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def settings_files(claude_dir: str, projects: dict) -> list[tuple[str, str | None]]:
    """[(settings path, project dir or None for the global layer)]."""
    out = []
    for name in ("settings.json", "settings.local.json"):
        p = os.path.join(claude_dir, name)
        if os.path.isfile(p):
            out.append((p, None))
    seen = {os.path.realpath(p) for p, _ in out}
    for name in sorted(projects):
        for fn in ("settings.json", "settings.local.json"):
            p = os.path.join(projects[name], ".claude", fn)
            r = os.path.realpath(p)
            if os.path.isfile(p) and r not in seen:
                seen.add(r)
                out.append((p, projects[name]))
    return out


def hook_entries(settings: dict):
    """(event, matcher, hook dict) for every hook entry in a settings dict."""
    for event, matchers in ((settings or {}).get("hooks") or {}).items():
        if not isinstance(matchers, list):
            continue
        for m in matchers:
            if not isinstance(m, dict):
                continue
            for h in m.get("hooks") or []:
                if isinstance(h, dict):
                    yield event, m.get("matcher", ""), h


def _short(cmd: str, home: str = "", n: int = 80) -> str:
    """A hook command on one line: home as ~, and head…tail when long (the
    script name is usually at the end)."""
    cmd = " ".join(cmd.split())
    if home:
        cmd = cmd.replace(home + "/", "~/")
    if len(cmd) <= n:
        return cmd
    head = n // 3
    return cmd[:head] + "…" + cmd[-(n - head - 1):]


def check_hook_timeouts(claude_dir: str, projects: dict, cfg: dict, home: str = "") -> list[Finding]:
    out = []
    for path, _proj in settings_files(claude_dir, projects):
        for event, matcher, h in hook_entries(_load_json(path) or {}):
            t = h.get("timeout")
            if not isinstance(t, (int, float)) or t <= HOOK_TIMEOUT_MAX:
                continue
            cmd = str(h.get("command") or h.get("url") or h.get("prompt") or "")
            if any(a in cmd for a in cfg["hook_timeout_allow"]):
                continue
            where = f"{event}[{matcher}]" if matcher else event
            out.append(Finding("e", path, f"{where} timeout {t:g} s (> {HOOK_TIMEOUT_MAX}): {_short(cmd, home)}"))
    return out


_SKIP_CMDS = {"echo", "printf", "true", "false", "exit", "test", "[", ":", "cat", "exec", "source", "."}
_PROJECT_DIR_RE = re.compile(r"\$\{CLAUDE_PROJECT_DIR(?::-[^}]*)?\}|\$CLAUDE_PROJECT_DIR\b")


def hook_scripts(command: str, project_dir: str | None, home: str) -> list[tuple[str, bool]]:
    """[(script path, must_be_executable)] for each simple command in a hook
    command line (`a && b; c`). A relative script resolves against the last
    `cd` or the repo; a command that names no file (echo, a bare PATH program)
    or depends on a run-time variable yields nothing."""
    cmd = command
    if project_dir:
        cmd = _PROJECT_DIR_RE.sub(lambda _m: project_dir, cmd)
    if home:
        cmd = cmd.replace("${HOME}", home).replace("$HOME", home)
    try:
        toks = shlex.split(cmd, comments=False)
    except ValueError:
        return []
    chains, cur = [], []
    for t in toks:
        if t in ("&&", "||", ";", "|"):
            chains.append(cur)
            cur = []
        elif len(t) > 1 and t.endswith(";"):
            cur.append(t[:-1])
            chains.append(cur)
            cur = []
        else:
            cur.append(t)
    chains.append(cur)
    out, cwd = [], project_dir
    for c in chains:
        words, skip_next = [], False
        for t in c:
            if skip_next:
                skip_next = False
                continue
            if re.match(r"^\d*[<>]{1,2}&?$", t):          # `>` `2>` `>>` then a target
                skip_next = True
                continue
            if re.match(r"^\d*[<>]", t):                  # `2>/dev/null`
                continue
            words.append(os.path.expanduser(t) if t.startswith("~") else t)
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words.pop(0)
        if words and os.path.basename(words[0]) == "env":
            words.pop(0)
            while words and (words[0].startswith("-") or "=" in words[0]):
                words.pop(0)
        if not words:
            continue
        first = words[0]
        if first == "cd":
            if len(words) > 1 and "$" not in words[1]:
                cwd = words[1] if os.path.isabs(words[1]) else (os.path.join(cwd, words[1]) if cwd else None)
            continue
        if first in _SKIP_CMDS:
            continue
        script, need_x = None, False
        if os.path.basename(first) in _INTERPRETERS:
            args = [w for w in words[1:] if not w.startswith("-")]
            if args and os.path.basename(first).startswith("python") and "-m" in words[1:]:
                args = []                                   # python -m module: no file
            script = args[0] if args else None
        elif "/" in first:
            script, need_x = first, True
        if not script or "$" in script:
            continue
        if not os.path.isabs(script):
            if not cwd:
                continue
            script = os.path.join(cwd, script)
        out.append((os.path.normpath(script), need_x))
    return out


def check_hook_scripts(claude_dir: str, projects: dict, home: str) -> list[Finding]:
    out = []
    for path, proj in settings_files(claude_dir, projects):
        for event, matcher, h in hook_entries(_load_json(path) or {}):
            if h.get("type", "command") != "command":
                continue
            where = f"{event}[{matcher}]" if matcher else event
            for script, need_x in hook_scripts(str(h.get("command") or ""), proj, home):
                if not os.path.exists(script):
                    out.append(Finding("f", path, f"{where}: {_tilde(script, home)} is missing"))
                elif need_x and not os.access(script, os.X_OK):
                    out.append(Finding("f", path, f"{where}: {_tilde(script, home)} is not executable"))
    return out


# ─── check g: rules ──────────────────────────────────────────────────────────

def glob_regex(pattern: str) -> re.Pattern:
    """A path glob (`**`, `*`, `?`, `{a,b}`) as a regex over repo-relative paths."""
    p = pattern.strip().lstrip("/")
    if p.startswith("./"):
        p = p[2:]
    out, i = [], 0
    while i < len(p):
        c = p[i]
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "{":
            j = p.find("}", i)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                out.append("(?:" + "|".join(re.escape(x) for x in p[i + 1:j].split(",")) + ")")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    body = "".join(out)
    if p.endswith("/"):
        body += ".*"
    return re.compile("^" + body + "$")


def repo_files(repo: str) -> list[str]:
    """Tracked and untracked-unignored files, repo-relative (git), else a walk."""
    try:
        r = subprocess.run(["git", "-C", repo, "ls-files", "-z", "--cached", "--others",
                            "--exclude-standard"], capture_output=True, timeout=30)
        if r.returncode == 0:
            return [x for x in r.stdout.decode("utf-8", "replace").split("\0") if x]
    except (OSError, subprocess.TimeoutExpired):
        pass
    files = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__", ".venv")]
        for fn in filenames:
            files.append(os.path.relpath(os.path.join(dirpath, fn), repo))
            if len(files) > 200_000:
                return files
    return files


def glob_matches(pattern: str, files: list[str]) -> bool:
    rx = glob_regex(pattern)
    if any(rx.match(f) for f in files):
        return True
    if "/" not in pattern.strip("/"):
        return any(fnmatch.fnmatchcase(os.path.basename(f), pattern) for f in files)
    return False


def check_rules(claude_dir: str, projects: dict) -> list[Finding]:
    out = []
    targets = [(os.path.join(claude_dir, "rules"), None)]
    targets += [(os.path.join(projects[n], ".claude", "rules"), projects[n]) for n in sorted(projects)]
    for rules_dir, repo in targets:
        rule_files = sorted(glob.glob(os.path.join(rules_dir, "*.md")))
        if not rule_files:
            continue
        files = None
        for f in rule_files:
            fm = frontmatter(_read(f) or "") or {}
            paths = fm.get("paths")
            if not paths:
                out.append(Finding("g", f, "no `paths:` (the rule loads in every session)"))
                continue
            if repo is None:
                continue
            if isinstance(paths, str):
                paths = [x.strip() for x in paths.split(",") if x.strip()]
            if files is None:
                files = repo_files(repo)
            dead = [str(p) for p in paths if not glob_matches(str(p), files)]
            if dead:
                out.append(Finding("g", f, "dead glob: " + ", ".join(dead[:4])
                                   + (f" (+{len(dead) - 4})" if len(dead) > 4 else "")))
    return out


# ─── checks h, i, j: alerts, env, hook errors ────────────────────────────────

def check_alerts(data_dir: str) -> list[Finding]:
    d = os.path.join(data_dir, "alerts")
    try:
        n = sum(1 for e in os.scandir(d) if e.is_file())
    except OSError:
        return []
    return [Finding("h", d, f"{n:,} files (> {ALERTS_MAX})")] if n > ALERTS_MAX else []


def check_global_env(claude_dir: str, projects: dict, cfg: dict) -> list[Finding]:
    skip = _own_projects(projects, claude_dir) | set(cfg["name_allow"])
    names = [n.lower().replace("-", "_") for n in projects if n not in skip and len(n) >= 3]
    out = []
    for fn in ("settings.json", "settings.local.json"):
        p = os.path.join(claude_dir, fn)
        env = ((_load_json(p) or {}).get("env") or {}) if os.path.isfile(p) else {}
        for key in sorted(env):
            if any(key.startswith(pre) for pre in cfg["env_prefixes"]):
                out.append(Finding("i", p, f"env {key}: a project-specific prefix"))
                continue
            k = "_" + key.lower() + "_"
            hit = next((n for n in names if "_" + n + "_" in k), None)
            if hit:
                out.append(Finding("i", p, f"env {key}: names the project {hit}"))
    return out


def _parse_ts(v):
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, tz=timezone.utc)
    if isinstance(v, str) and v:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def check_hook_errors(data_dir: str, now: datetime | None = None) -> tuple[list[Finding], str | None]:
    """(findings, note). The note says when there is no log to read."""
    now = now or datetime.now(timezone.utc)
    files = sorted(glob.glob(os.path.join(data_dir, "raw", "health", "hook-errors-*.jsonl")))
    files += sorted(glob.glob(os.path.join(data_dir, "raw", "hook-errors*.jsonl")))
    if not files:
        return [], "no hook-error log yet"
    cutoff = now - timedelta(hours=HOOK_ERRORS_WINDOW_H)
    count, by_hook = 0, {}
    for f in files:
        try:
            with open(f, errors="replace") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    ts = _parse_ts(row.get("ts", row.get("timestamp")))
                    if ts is None or ts < cutoff:
                        continue
                    count += 1
                    hook = str(row.get("hook") or "?")
                    by_hook[hook] = by_hook.get(hook, 0) + 1
        except OSError:
            continue
    if not count:
        return [], None
    top = ", ".join(f"{h}×{n}" for h, n in sorted(by_hook.items(), key=lambda kv: -kv[1])[:3])
    where = os.path.join(data_dir, "raw", "health", "hook-errors-*.jsonl")
    return [Finding("j", where, f"{count} hook error(s) in the last {HOOK_ERRORS_WINDOW_H} h ({top})")], None


# ─── driver ──────────────────────────────────────────────────────────────────

def run(claude_dir: str | None = None, registry_path: str | None = None, data_dir: str | None = None,
        projects: dict | None = None, home: str | None = None, now: datetime | None = None) -> dict:
    """Run every check. Returns {findings: [Finding], counts: {id: n},
    notes: {id: text}, errors: {id: text}}."""
    claude_dir = claude_dir or default_claude_dir()
    registry_path = registry_path or default_registry_path()
    data_dir = data_dir or default_data_dir()
    home = home if home is not None else os.path.expanduser("~")
    cfg = load_config(registry_path)
    if projects is None:
        projects = load_projects(registry_path)
    projects = {n: p for n, p in projects.items() if n not in cfg["skip_repos"]}
    findings, errors, notes = [], {}, {}
    steps = [
        ("a", lambda: check_global_refs(claude_dir, projects, cfg, home)),
        ("b", lambda: check_skill_descriptions(claude_dir, projects)),
        ("c", lambda: check_claude_md(claude_dir, projects)),
        ("d", lambda: check_status_now(projects)),
        ("e", lambda: check_hook_timeouts(claude_dir, projects, cfg, home)),
        ("f", lambda: check_hook_scripts(claude_dir, projects, home)),
        ("g", lambda: check_rules(claude_dir, projects)),
        ("h", lambda: check_alerts(data_dir)),
        ("i", lambda: check_global_env(claude_dir, projects, cfg)),
    ]
    for cid, fn in steps:
        try:
            findings += fn()
        except Exception as e:          # one broken check must not hide the others
            errors[cid] = f"{type(e).__name__}: {e}"
    try:
        fj, note = check_hook_errors(data_dir, now)
        findings += fj
        if note:
            notes["j"] = note
    except Exception as e:
        errors["j"] = f"{type(e).__name__}: {e}"
    counts = {cid: 0 for cid, _, _ in CHECKS}
    for f in findings:
        counts[f.check] += 1
    return {"findings": findings, "counts": counts, "notes": notes, "errors": errors,
            "repos": len(projects)}


def render(result: dict, home: str | None = None) -> str:
    """The summary and the findings table as text."""
    home = home if home is not None else os.path.expanduser("~")
    findings, counts = result["findings"], result["counts"]
    lines = [f"Harness lint: {len(findings)} finding(s) over {result.get('repos', 0)} repos + the global layer", ""]
    lines.append(f"{'CHECK':<22} {'N':>4}  WHAT")
    for cid, name, what in CHECKS:
        extra = ""
        if cid in result["errors"]:
            extra = f"  [check errored: {result['errors'][cid]}]"
        elif cid in result["notes"]:
            extra = f"  [{result['notes'][cid]}]"
        lines.append(f"{cid} {name:<20} {counts.get(cid, 0):>4}  {what}{extra}")
    if findings:
        rows = [(f"{f.check} {f.name}", _tilde(f.path, home), f.message) for f in
                sorted(findings, key=lambda f: (f.check, f.path, f.message))]
        w1 = max(len(r[0]) for r in rows)
        w2 = min(max(len(r[1]) for r in rows), 72)
        lines += ["", f"{'CHECK':<{w1}}  {'PATH':<{w2}}  FINDING"]
        for c, p, m in rows:
            lines.append(f"{c:<{w1}}  {p:<{w2}}  {m}")
    return "\n".join(lines)


def to_json(result: dict) -> str:
    return json.dumps({"findings": [dict(asdict(f), name=f.name) for f in result["findings"]],
                       "counts": {CHECK_NAMES[k]: v for k, v in result["counts"].items()},
                       "notes": result["notes"], "errors": result["errors"],
                       "repos": result.get("repos"), "total": len(result["findings"])}, indent=1)


def summary_line(result: dict) -> tuple[bool, str]:
    """(ok, one doctor row) for plain `clanker doctor`."""
    n = len(result["findings"])
    if result["errors"]:
        return False, f"Harness lint errored in check(s) {', '.join(sorted(result['errors']))} — run: clanker doctor --harness"
    if not n:
        return True, "Harness lint: clean"
    parts = ", ".join(f"{CHECK_NAMES[c]} {k}" for c, k in result["counts"].items() if k)
    return False, f"Harness lint: {n} finding(s) ({parts}) — run: clanker doctor --harness"


def main(json_out: bool = False) -> int:
    """`clanker doctor --harness`: print, then 1 on any finding or check error."""
    import sys
    result = run()
    print(to_json(result) if json_out else render(result))
    for cid, err in result["errors"].items():
        print(f"harness lint: check {cid} ({CHECK_NAMES[cid]}) errored: {err}", file=sys.stderr)
    return 1 if (result["findings"] or result["errors"]) else 0
