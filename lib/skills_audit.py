"""Skill listing audit and hide — `clanker skills audit` / `clanker skills hide`.

Every listed skill puts its name and description into the context of every
session in its scope. `audit` sets that cost against use: per repo (and the
user layer), each skill's description bytes, its uses and last use, and a
verdict:

  never   no recorded use
  stale   last use more than --stale-days ago (default 90)
  active  used within --stale-days

Uses come from two independent counters, shown side by side (they overlap,
so they are never summed):
  tracker  $CLANKER_DATA/raw/skills/*.jsonl, one row per Skill tool call
           (clanker's skill-tracker hook; project = the session's project)
  cc       Claude Code's own `skillUsage` in ~/.claude.json
           ({usageCount, lastUsedAt}), where present

`hide <repo> <skill...>` writes `skillOverrides: {"<skill>": "off"}` (or
--mode) into <repo>/.claude/settings.local.json, merged into what is there.
Claude Code 2.1.280 schema: skillOverrides maps a skill name to "on",
"name-only" (listed without its description), "user-invocable-only" (hidden
from the model, /name still works) or "off" (hidden from both); it is read
from the merged settings, so the local file applies. hide warns when that file
is not gitignored in the repo. Hiding is always an explicit command.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

MODES = ("off", "user-invocable-only", "name-only", "on")
STALE_DAYS = 90
USER = "(user)"


def default_claude_json() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~")
    return os.path.join(base, ".claude.json")


def _frontmatter(path: str) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(200_000)
    except OSError:
        return {}
    from harness_lint import frontmatter
    return frontmatter(text) or {}


def inventory(claude_dir: str, projects: dict) -> list[dict]:
    """Every SKILL.md in the user layer and in each repo's .claude/skills."""
    rows = []
    targets = [(USER, os.path.join(claude_dir, "skills"))]
    targets += [(n, os.path.join(projects[n], ".claude", "skills")) for n in sorted(projects)]
    for repo, root in targets:
        for path in sorted(glob.glob(os.path.join(root, "*", "SKILL.md"))):
            d = os.path.basename(os.path.dirname(path))
            if d.startswith("."):
                continue
            fm = _frontmatter(path)
            desc = " ".join(str(fm.get("description") or "").split())
            rows.append({"repo": repo, "skill": d, "name": str(fm.get("name") or d), "path": path,
                         "desc_bytes": len(desc.encode("utf-8"))})
    return rows


def tracker_usage(data_dir: str) -> dict:
    """{skill: {"uses": n, "last": datetime, "projects": {project: n}}} from raw/skills."""
    out = {}
    for f in sorted(glob.glob(os.path.join(data_dir, "raw", "skills", "*.jsonl"))):
        try:
            with open(f, errors="replace") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    skill = row.get("skill")
                    if not skill:
                        continue
                    ts = _parse_ts(row.get("timestamp"))
                    u = out.setdefault(skill, {"uses": 0, "last": None, "projects": {}})
                    u["uses"] += 1
                    p = row.get("project") or "global"
                    u["projects"][p] = u["projects"].get(p, 0) + 1
                    if ts and (u["last"] is None or ts > u["last"]):
                        u["last"] = ts
        except OSError:
            continue
    return out


def cc_usage(claude_json: str) -> dict:
    """{skill: {"uses": n, "last": datetime}} from ~/.claude.json skillUsage."""
    try:
        with open(claude_json) as f:
            su = (json.load(f) or {}).get("skillUsage") or {}
    except (OSError, ValueError):
        return {}
    out = {}
    for name, v in su.items():
        if not isinstance(v, dict):
            continue
        last = v.get("lastUsedAt")
        out[name] = {"uses": int(v.get("usageCount") or 0),
                     "last": datetime.fromtimestamp(last / 1000, tz=timezone.utc)
                     if isinstance(last, (int, float)) else None}
    return out


def _parse_ts(v):
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def audit(claude_dir: str, projects: dict, data_dir: str, claude_json: str,
          now: datetime | None = None, stale_days: int = STALE_DAYS) -> list[dict]:
    """The inventory joined to both usage counters, with a verdict per skill."""
    now = now or datetime.now(timezone.utc)
    tr, cc = tracker_usage(data_dir), cc_usage(claude_json)
    rows = []
    for s in inventory(claude_dir, projects):
        keys = {s["skill"], s["name"]}
        t = [tr[k] for k in keys if k in tr]
        c = [cc[k] for k in keys if k in cc]
        lasts = [u["last"] for u in t + c if u.get("last")]
        last = max(lasts) if lasts else None
        t_uses = sum(u["uses"] for u in t)
        c_uses = max((u["uses"] for u in c), default=0)
        if not t_uses and not c_uses:
            verdict = "never"
        elif last is None or (now - last).days > stale_days:
            verdict = "stale"
        else:
            verdict = "active"
        rows.append({**s, "tracker_uses": t_uses, "cc_uses": c_uses,
                     "last_use": last.strftime("%Y-%m-%d") if last else None,
                     "days_since": (now - last).days if last else None, "verdict": verdict})
    order = {"never": 0, "stale": 1, "active": 2}
    rows.sort(key=lambda r: (r["repo"] != USER, r["repo"], order[r["verdict"]], -r["desc_bytes"]))
    return rows


def render(rows: list[dict], stale_days: int = STALE_DAYS) -> str:
    if not rows:
        return "No skills found."
    by = {"never": 0, "stale": 0, "active": 0}
    cost = {"never": 0, "stale": 0, "active": 0}
    for r in rows:
        by[r["verdict"]] += 1
        cost[r["verdict"]] += r["desc_bytes"]
    repos = len({r["repo"] for r in rows})
    lines = [f"Skills: {len(rows)} in {repos} scopes — never {by['never']}, stale (>{stale_days} d) "
             f"{by['stale']}, active {by['active']}",
             f"Description bytes: {sum(cost.values()):,} total; never {cost['never']:,}, "
             f"stale {cost['stale']:,}, active {cost['active']:,}", ""]
    w_repo = max(len("REPO"), max(len(r["repo"]) for r in rows))
    w_sk = max(len("SKILL"), max(len(r["skill"]) for r in rows))
    lines.append(f"{'REPO':<{w_repo}}  {'SKILL':<{w_sk}}  {'DESC_B':>6}  {'TRACKER':>7}  {'CC':>5}  "
                 f"{'LAST USE':<10}  VERDICT")
    for r in rows:
        lines.append(f"{r['repo']:<{w_repo}}  {r['skill']:<{w_sk}}  {r['desc_bytes']:>6}  "
                     f"{r['tracker_uses']:>7}  {r['cc_uses']:>5}  {r['last_use'] or '-':<10}  {r['verdict']}")
    return "\n".join(lines)


# ─── hide ────────────────────────────────────────────────────────────────────

def is_gitignored(repo: str, rel: str) -> bool | None:
    """True/False from `git check-ignore`; None when repo is not a git repo."""
    try:
        r = subprocess.run(["git", "-C", repo, "check-ignore", "-q", rel], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    return None


def hide(repo_path: str, skills: list[str], mode: str = "off", dry_run: bool = False,
         out=None, err=None) -> int:
    """Merge skillOverrides {skill: mode} into <repo>/.claude/settings.local.json."""
    out = out or sys.stdout
    err = err or sys.stderr
    if mode not in MODES:
        print(f"skills hide: --mode must be one of {', '.join(MODES)}", file=err)
        return 2
    if not os.path.isdir(repo_path):
        print(f"skills hide: repo not found: {repo_path}", file=err)
        return 1
    target = os.path.join(repo_path, ".claude", "settings.local.json")
    data = {}
    if os.path.exists(target):
        try:
            with open(target) as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            print(f"skills hide: {target} is not valid JSON ({e}); nothing written", file=err)
            return 1
        if not isinstance(data, dict):
            print(f"skills hide: {target} is not a JSON object; nothing written", file=err)
            return 1
    overrides = data.get("skillOverrides") or {}
    if not isinstance(overrides, dict):
        print(f"skills hide: skillOverrides in {target} is not an object; nothing written", file=err)
        return 1
    known = {os.path.basename(os.path.dirname(p)) for p in
             glob.glob(os.path.join(repo_path, ".claude", "skills", "*", "SKILL.md"))}
    for s in skills:
        if s not in known:
            print(f"skills hide: note: {s} is not a skill in {repo_path}/.claude/skills "
                  "(a user, bundled or plugin skill is fine)", file=err)
    new = dict(overrides)
    for s in skills:
        new[s] = mode
    data = dict(data, skillOverrides=new)
    ignored = is_gitignored(repo_path, ".claude/settings.local.json")
    if ignored is False:
        print(f"WARNING: .claude/settings.local.json is not gitignored in {repo_path}; "
              "it can be committed by mistake (add it to .gitignore)", file=err)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if dry_run:
        print(f"[dry-run] would write {target}:", file=out)
        print(text, end="", file=out)
        return 0
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, target)
    for s in skills:
        print(f"hid {s} ({mode}) in {target}", file=out)
    return 0
