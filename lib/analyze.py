"""Analysis pipeline — cost-weighted rankings, anomaly detection."""

import json
import os
import glob
from collections import defaultdict
from datetime import datetime, timedelta

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")
SESSIONS_DIR = os.path.join(DATA_DIR, "raw/sessions")

# S1: memoize parsed session records. Keyed on (newest sessions-dir mtime, file
# count, last_days, dedup, cutoff-date) and invalidated automatically when a
# session file is appended-to or added (the mtime/count fingerprint changes) or
# the day rolls over — so the dashboard refresher stops re-reading + re-parsing
# 90 days of JSONL on every warm cycle.
_sessions_memo = {}


def _sessions_signature():
    """Cheap stat-only fingerprint of SESSIONS_DIR: (newest mtime, file count).
    Changes whenever a session file is appended-to or a new one appears."""
    try:
        files = glob.glob(os.path.join(SESSIONS_DIR, "*.jsonl"))
        newest = max((os.path.getmtime(f) for f in files), default=0.0)
        return (newest, len(files))
    except OSError:
        return (0.0, 0)


def _prune_sessions_memo():
    """Keep the memo tiny — only a couple of (last_days, dedup) combos are ever
    used, but each new fingerprint adds a key; drop all but the newest few."""
    if len(_sessions_memo) > 8:
        for k in list(_sessions_memo)[:-4]:
            del _sessions_memo[k]

# Cap session duration at 8 hours — longer durations are idle tmux sessions,
# not continuous work. This prevents inflated cost metrics.
MAX_DURATION_S = 28800  # 8 hours

# Normalize project names from bootstrap data to registry names
PROJECT_ALIASES = {
    "Quanta-AI-V1": "quanta-ai", "quantaaiv1": "quanta-ai", "quanta_ai_v1": "quanta-ai",
    "Eigenstate-V2": "eigenstate", "eigenstatev2": "eigenstate",
    "Research": "eigenstateresearch", "research": "eigenstateresearch",
    "FlowStudio": "flowstudio",
    "Zergrush": "zergrush",
    "Mac-Mini": "macmini",
    "Titrin": "titrin",
}

# Worktree/branch directories (e.g. ~/projects/<base>-<wave-tag>, ~/projects/<base>-worktrees)
# are the SAME project for analytics. Collapse "<base>-anything" → "<base>" for these bases.
# Without this, every worktree spawns a phantom project (e.g. polymarkethftinfrastructure-g2-handshake).
WORKTREE_BASES = (
    "polymarkethftinfrastructure",
    "hftlogger-rust",
    "hftbacktester",
    "hftlogger",
)

# Fixture/test rows must never reach analytics. The suite is data-isolated via
# tests/conftest.py (CLANKER_DATA → tmpdir, 2026-07-19), but historical pytest
# runs wrote these into the live store, and any future writer that skips the
# fixture would too — belt-and-suspenders at read time.
TEST_PROJECTS = {"test-project", "test-stdin"}


def normalize_project(name):
    """Normalize a project name to its canonical registry name.

    Applies, in order: (1) explicit bootstrap aliases, (2) worktree-family
    collapse so `<base>-<worktree-suffix>` and `<base>-worktrees` map to `<base>`.
    """
    if not name:
        return "global"
    name = PROJECT_ALIASES.get(name, name)
    for base in WORKTREE_BASES:
        if name == f"{base}-worktrees" or name.startswith(f"{base}-"):
            return base
    return name


def load_sessions(last_days=7, dedup=True):
    """Load session metrics from JSONL files.

    KEYSTONE FIX: the SessionEnd/Stop hooks re-fire on every `--resume` and
    re-parse the ENTIRE growing transcript, appending a fresh cumulative record
    each time. One real session was logged up to 354×, so the raw log inflates
    661 real sessions into ~9,070 phantom rows (and every downstream metric with
    it). We dedup by session_id keeping the LAST record — which is the true final
    state of that session (the latest re-parse covers the whole transcript).

    Set dedup=False only to inspect the raw event stream.
    """
    cutoff = datetime.utcnow() - timedelta(days=last_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    sig = _sessions_signature()
    key = (sig[0], sig[1], last_days, dedup, cutoff_str)
    cached = _sessions_memo.get(key)
    if cached is not None:
        return list(cached)   # shallow copy so a caller's in-place sort can't corrupt the memo

    records = []
    for f in sorted(glob.glob(os.path.join(SESSIONS_DIR, "*.jsonl"))):
        basename = os.path.basename(f).replace(".jsonl", "")
        if basename < cutoff_str:
            continue
        try:
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        s = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    s["project"] = normalize_project(s.get("project", "global"))
                    if s["project"] in TEST_PROJECTS:
                        continue
                    records.append(s)
        except OSError:
            pass

    if not dedup:
        _sessions_memo[key] = records
        _prune_sessions_memo()
        return list(records)

    # Dedup by session_id, last-write-wins. Files are date-sorted and lines are
    # appended chronologically, so the final occurrence of an id is its latest
    # (most complete) state. Records without a session_id (some ingested/bootstrap
    # events) can't be deduped — keep them all.
    by_id = {}
    anon = []
    for s in records:
        sid = s.get("session_id")
        if sid:
            by_id[sid] = s
        else:
            anon.append(s)
    result = list(by_id.values()) + anon
    _sessions_memo[key] = result
    _prune_sessions_memo()
    return list(result)


def run_analysis(mode, by="project", last_days=7, json_output=False):
    """Run analysis and print results."""
    if mode == "daily":
        sessions = load_sessions(last_days=1)
    elif mode == "weekly":
        sessions = load_sessions(last_days=7)
    elif mode == "errors":
        sessions = load_sessions(last_days=30)
        sessions.sort(key=lambda s: -s.get("errors", 0))
        _print_error_report(sessions[:20], json_output)
        return
    elif mode == "slow":
        sessions = load_sessions(last_days=30)
        sessions.sort(key=lambda s: -s.get("duration_s", 0))
        _print_slow_report(sessions[:20], json_output)
        return
    else:
        sessions = load_sessions(last_days=last_days)

    if not sessions:
        print("No sessions found.")
        return

    if by == "project":
        _print_project_report(sessions, mode, json_output)
    elif by == "tool":
        _print_tool_report(sessions, json_output)


def _print_project_report(sessions, mode, json_output):
    by_project = defaultdict(lambda: {"count": 0, "errors": 0, "time": 0, "tools": 0})
    for s in sessions:
        p = s.get("project", "global")
        by_project[p]["count"] += 1
        by_project[p]["errors"] += s.get("errors", 0)
        by_project[p]["time"] += min(s.get("duration_s", 0), MAX_DURATION_S)
        by_project[p]["tools"] += sum(s.get("tool_uses", {}).values())

    total_errors = sum(d["errors"] for d in by_project.values())
    total_time = sum(d["time"] for d in by_project.values())

    print(f"=== {mode.upper()} Report ===")
    print(f"Sessions: {len(sessions)} | Time: {total_time/3600:.1f}h | Errors: {total_errors}")
    print()

    # Cost-weighted ranking
    ranked = []
    for p, d in by_project.items():
        avg_time = d["time"] / d["count"] if d["count"] > 0 else 0
        cost = d["errors"] * (avg_time / 3600)
        err_rate = d["errors"] / d["count"] if d["count"] > 0 else 0
        avg_h = avg_time / 3600
        ranked.append((p, d, cost, err_rate, avg_h))
    ranked.sort(key=lambda x: -x[2])

    if json_output:
        for p, d, cost, err_rate, avg_h in ranked:
            print(json.dumps({"project": p, **d, "cost": round(cost, 1),
                              "err_rate": round(err_rate, 1), "avg_hours": round(avg_h, 1)}))
    else:
        print(f"{'Project':<20} {'Sess':>5} {'Errs':>6} {'Err/S':>6} {'AvgH':>6} {'Cost':>8}")
        print("-" * 58)
        for p, d, cost, err_rate, avg_h in ranked:
            print(f"{p:<20} {d['count']:>5} {d['errors']:>6} {err_rate:>6.1f} {avg_h:>6.1f} {cost:>8.1f}")

        # Recommendations
        print()
        print("=== Recommendations ===")
        has_recs = False
        for p, d, cost, err_rate, avg_h in ranked:
            if cost > 5:
                print(f"HIGH: {p} — {cost:.0f} error-hours. Investigate error patterns.")
                has_recs = True
            if avg_h > 3:
                print(f"MEDIUM: {p} — sessions average {avg_h:.1f}h. Consider task decomposition.")
                has_recs = True
        if not has_recs:
            print("No actionable recommendations — all projects within normal parameters.")


def _print_error_report(sessions, json_output):
    print(f"{'Date':<12} {'Project':<20} {'Errors':>8} {'Duration':>10}")
    print("-" * 55)
    for s in sessions:
        dur = f"{s.get('duration_s', 0) // 60}m"
        print(f"{s.get('timestamp', '')[:10]:<12} {s.get('project', '?'):<20} {s.get('errors', 0):>8} {dur:>10}")


def _print_slow_report(sessions, json_output):
    print(f"{'Date':<12} {'Project':<20} {'Duration':>10} {'Errors':>8}")
    print("-" * 55)
    for s in sessions:
        dur = f"{s.get('duration_s', 0) / 3600:.1f}h"
        print(f"{s.get('timestamp', '')[:10]:<12} {s.get('project', '?'):<20} {dur:>10} {s.get('errors', 0):>8}")


def _print_tool_report(sessions, json_output):
    tools = defaultdict(lambda: {"uses": 0, "errors": 0})
    for s in sessions:
        for tool, count in s.get("tool_uses", {}).items():
            tools[tool]["uses"] += count
        for tool, count in s.get("error_tools", {}).items():
            tools[tool]["errors"] += count

    print(f"{'Tool':<15} {'Uses':>8} {'Errors':>8} {'Error%':>8}")
    print("-" * 42)
    for tool in sorted(tools, key=lambda t: -tools[t]["uses"]):
        d = tools[tool]
        pct = (d["errors"] / d["uses"] * 100) if d["uses"] > 0 else 0
        print(f"{tool:<15} {d['uses']:>8} {d['errors']:>8} {pct:>7.1f}%")
