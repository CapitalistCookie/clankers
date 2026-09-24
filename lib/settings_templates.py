"""Archetype settings templates — `clanker settings check` / `clanker settings apply`.

templates/settings/<name>.json holds the keys a repo must carry in its tracked
`.claude/settings.json`. A repo's template is its registry archetype
(research, production, tool, infra, frontend) unless the registry names
another one:

  settings_templates:
    overrides:
      some-repo: build          # e.g. a repo that uses claude.ai connectors

`check` reports, per repo: the template, whether the settings file exists and
is tracked, the template keys it misses (or holds with another value), the
keys it has beyond the template (information, not drift), and hook timeouts
over 60 s (the harness lint's limit and allowlist). Drift = a missing key or
an over-limit timeout; any drift exits 1.

`apply <repo...> --key K` writes the template's value of K into each repo's
`.claude/settings.json` with a minimal diff (one inserted line), and with
--commit makes one plain commit of that file alone (a dirty file is refused;
a failed commit restores the file). Nothing is ever applied to all repos at
once: the repos are named.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

REPO_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
REL = os.path.join(".claude", "settings.json")


def templates_dir() -> str:
    return os.environ.get("CLANKER_SETTINGS_TEMPLATES") or os.path.join(REPO_ROOT, "templates", "settings")


def template_names() -> list[str]:
    d = templates_dir()
    try:
        return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))
    except OSError:
        return []


def load_template(name: str) -> dict:
    with open(os.path.join(templates_dir(), name + ".json")) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"template {name} is not a JSON object")
    return data


def load_config(registry_path: str) -> dict:
    """The registry's `settings_templates:` block: {"overrides": {repo: template}}."""
    try:
        import yaml
        with open(registry_path) as f:
            block = (yaml.safe_load(f) or {}).get("settings_templates") or {}
    except Exception:
        block = {}
    return {"overrides": {str(k): str(v) for k, v in (block.get("overrides") or {}).items()}}


def template_for(repo: str, archetype: str, cfg: dict) -> str | None:
    name = cfg["overrides"].get(repo) or archetype
    return name if name in template_names() else None


def _load(path: str):
    """(dict or None, error or None) for a JSON settings file."""
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"invalid JSON: {e}"
    return (data, None) if isinstance(data, dict) else (None, "not a JSON object")


def is_tracked(repo: str, rel: str = REL) -> bool | None:
    try:
        r = subprocess.run(["git", "-C", repo, "ls-files", "--error-unmatch", rel],
                           capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0:
        return True
    return False if b"did not match" in r.stderr else None


def check_repo(repo: str, path: str, archetype: str, cfg: dict, lint_cfg: dict | None = None) -> dict:
    from harness_lint import hook_entries, HOOK_TIMEOUT_MAX
    lint_cfg = lint_cfg or {"hook_timeout_allow": []}
    row = {"repo": repo, "path": path, "archetype": archetype, "template": None, "file": False,
           "tracked": None, "missing": [], "differs": [], "extra": [], "local_only": [],
           "timeouts": [], "status": "ok", "error": None}
    tname = template_for(repo, archetype, cfg)
    row["template"] = tname
    settings_path = os.path.join(path, REL)
    data, err = _load(settings_path)
    row["file"] = os.path.isfile(settings_path)
    if row["file"]:
        row["tracked"] = is_tracked(path)
    if err:
        row["error"] = f"{REL}: {err}"
        row["status"] = "error"
        return row
    data = data or {}
    local, _ = _load(os.path.join(path, ".claude", "settings.local.json"))
    local = local or {}
    if tname is None:
        row["error"] = f"no template for archetype {archetype!r}"
        row["status"] = "no-template"
    else:
        tmpl = load_template(tname)
        for k, v in tmpl.items():
            if k not in data:
                row["missing"].append(k)
                if local.get(k) == v:
                    row["local_only"].append(k)
            elif data[k] != v:
                row["differs"].append(k)
        row["extra"] = sorted(k for k in data if k not in tmpl)
    for event, matcher, h in hook_entries(data):
        t = h.get("timeout")
        cmd = str(h.get("command") or "")
        if isinstance(t, (int, float)) and t > HOOK_TIMEOUT_MAX and \
                not any(a in cmd for a in lint_cfg.get("hook_timeout_allow") or []):
            row["timeouts"].append(f"{event}{'[' + matcher + ']' if matcher else ''} {t:g}s")
    if row["status"] == "ok" and (row["missing"] or row["differs"] or row["timeouts"]):
        row["status"] = "drift"
    return row


def check(repos: list[str] | None = None, registry_path: str | None = None) -> list[dict]:
    """check_repo for the named repos (None = every registered repo)."""
    import harness_lint
    from registry import Registry
    registry_path = registry_path or harness_lint.default_registry_path()
    cfg = load_config(registry_path)
    lint_cfg = harness_lint.load_config(registry_path)
    reg = Registry(registry_path)
    projects = harness_lint.load_projects(registry_path)
    names = sorted(projects) if repos is None else repos
    rows = []
    for n in names:
        if n not in projects:
            rows.append({"repo": n, "path": None, "archetype": None, "template": None, "file": False,
                         "tracked": None, "missing": [], "differs": [], "extra": [], "local_only": [],
                         "timeouts": [], "status": "error", "error": "not a registered repo on disk"})
            continue
        rows.append(check_repo(n, projects[n], reg.get_archetype(n), cfg, lint_cfg))
    return rows


def render(rows: list[dict]) -> str:
    if not rows:
        return "No repos."
    drift = sum(1 for r in rows if r["status"] != "ok")
    lines = [f"Settings templates: {len(rows)} repo(s), {len(rows) - drift} ok, {drift} with drift or errors", ""]
    w = max(4, max(len(r["repo"]) for r in rows))
    lines.append(f"{'REPO':<{w}}  {'ARCHETYPE':<10}  {'TEMPLATE':<10}  {'FILE':<9}  STATUS    DETAIL")
    for r in rows:
        f = "tracked" if r["tracked"] else ("untracked" if r["file"] else "none")
        detail = []
        if r["missing"]:
            detail.append("missing: " + ", ".join(r["missing"]) +
                          (f" (set in settings.local.json: {', '.join(r['local_only'])})" if r["local_only"] else ""))
        if r["differs"]:
            detail.append("differs: " + ", ".join(r["differs"]))
        if r["timeouts"]:
            detail.append("timeouts > 60 s: " + ", ".join(r["timeouts"]))
        if r["extra"]:
            detail.append("extra: " + ", ".join(r["extra"]))
        if r["error"]:
            detail.append(r["error"])
        lines.append(f"{r['repo']:<{w}}  {str(r['archetype'] or '-'):<10}  {str(r['template'] or '-'):<10}  "
                     f"{f:<9}  {r['status']:<8}  {'; '.join(detail)}")
    return "\n".join(lines)


# ─── apply ───────────────────────────────────────────────────────────────────

def _indent_of(text: str) -> str:
    for line in text.splitlines()[1:]:
        m = re.match(r'^([ \t]+)"', line)
        if m:
            return m.group(1)
    return "  "


def with_key(text: str, key: str, value) -> str:
    """`text` (a JSON object) with key=value: one inserted line after `{` when
    the key is absent and the file is multi-line, else a full re-dump."""
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("settings file is not a JSON object")
    if key in data and data[key] == value:
        return text
    expected = dict(data)
    expected[key] = value
    i = text.find("{")
    if key not in data and data and i >= 0 and text[i + 1:i + 2] == "\n":
        line = f"{_indent_of(text)}{json.dumps(key)}: {json.dumps(value)},"
        out = text[:i + 1] + "\n" + line + text[i + 1:]
    else:
        ind = _indent_of(text) if text.strip() else "  "
        out = json.dumps(expected, indent=len(ind.expandtabs(4)) or 2, ensure_ascii=False) + "\n"
    if json.loads(out) != expected:
        raise ValueError("edit check failed: the result is not the file plus the key")
    return out


def _git(repo: str, *args, timeout: int = 60):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout)


def commit_file(repo: str, rel: str, subject: str, body: str, tries: int = 10, wait: float = 5.0):
    """One plain commit of `rel` alone. (ok, short sha or error text).
    A held index.lock is waited out (`wait` s, `tries` times)."""
    last = ""
    for _ in range(tries):
        a = _git(repo, "add", "--", rel)
        if a.returncode == 0:
            c = _git(repo, "commit", "-q", "-m", subject, "-m", body, "--", rel, timeout=600)
            if c.returncode == 0:
                return True, _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
            last = (c.stderr or c.stdout).strip()
        else:
            last = a.stderr.strip()
        if "index.lock" not in last:
            break
        time.sleep(wait)
    return False, last or "git commit failed"


def apply_key(repo: str, path: str, archetype: str, key: str, cfg: dict, *, commit: bool = False,
              dry_run: bool = False, create: bool = False, out=None, err=None) -> int:
    """Write the template value of `key` into <path>/.claude/settings.json.
    Returns 0 when set (or already set), 1 on refusal or failure."""
    out = out or sys.stdout
    err = err or sys.stderr
    tname = template_for(repo, archetype, cfg)
    if tname is None:
        print(f"{repo}: no template for archetype {archetype!r}; nothing written", file=err)
        return 1
    tmpl = load_template(tname)
    if key not in tmpl:
        print(f"{repo}: template {tname} has no key {key!r}; nothing written", file=err)
        return 1
    target = os.path.join(path, REL)
    exists = os.path.isfile(target)
    if not exists and not create:
        print(f"{repo}: {REL} does not exist (use --create); nothing written", file=err)
        return 1
    if commit and exists:
        st = _git(path, "status", "--porcelain", "--", REL)
        if st.returncode != 0:
            print(f"{repo}: not a git repo ({st.stderr.strip()}); nothing written", file=err)
            return 1
        if st.stdout.strip():
            print(f"{repo}: {REL} has uncommitted changes; nothing written", file=err)
            return 1
    before = open(target).read() if exists else ""
    try:
        after = with_key(before or "{}\n", key, tmpl[key])
    except ValueError as e:
        print(f"{repo}: {e}; nothing written", file=err)
        return 1
    if after == before:
        print(f"{repo}: {key} already {json.dumps(tmpl[key])} ({target})", file=out)
        return 0
    if dry_run:
        print(f"[dry-run] {repo}: would set {key}={json.dumps(tmpl[key])} in {target}", file=out)
        return 0
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(after)
    msg = f"{repo}: set {key}={json.dumps(tmpl[key])} in {target} (template {tname})"
    if not commit:
        print(msg, file=out)
        return 0
    subject = f"settings: {key} {json.dumps(tmpl[key])} (clanker settings apply, {tname} template)"
    body = (f"The {tname} archetype template sets {key}={json.dumps(tmpl[key])} in the tracked "
            f"{REL}. Written by `clanker settings apply {repo} --key {key} --commit`.")
    if key == "disableClaudeAiConnectors":
        body += (" Claude Code 2.1.280 reads this key from project settings (any source set to true "
                 "wins): the claude.ai connectors (Claude Docs, Gmail, Calendar, Drive) are not "
                 "fetched or connected in this repo's sessions.")
    ok, info = commit_file(path, REL, subject, body)
    if not ok:
        _git(path, "reset", "-q", "--", REL)          # unstage: the path was clean before
        if exists:
            with open(target, "w") as f:
                f.write(before)
        else:
            os.remove(target)
        print(f"{repo}: commit failed, file restored: {info}", file=err)
        return 1
    print(f"{msg}; committed {info}", file=out)
    return 0
