"""Generate dashboard data from clanker's data stores."""

import json
import os
import glob
from collections import defaultdict, Counter
from datetime import datetime, timedelta

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")

# Per-model token pricing ($/M: input, output, cache_read, cache_write). Recomputed
# at display so the estimate is correct for known-model (recent) sessions; unknown
# model -> Opus, since historical records predate model tracking and usage is mostly Opus.
_PRICING = {
    "opus": (15.0, 75.0, 1.50, 18.75),
    "sonnet": (3.0, 15.0, 0.30, 3.75),
    "haiku": (0.80, 4.0, 0.08, 1.00),
}


def _session_cost(s):
    t = s.get("tokens") or {}
    if not t:
        return s.get("estimated_cost_usd", 0) or 0
    m = (s.get("model") or "").lower()
    pin, pout, pcr, pcw = (_PRICING["haiku"] if "haiku" in m
                           else _PRICING["sonnet"] if "sonnet" in m
                           else _PRICING["opus"])
    return (t.get("input", 0) / 1e6 * pin + t.get("output", 0) / 1e6 * pout +
            t.get("cache_read", 0) / 1e6 * pcr + t.get("cache_create", 0) / 1e6 * pcw)


def generate_dashboard_data():
    """Generate all data needed for the dashboard as a single JSON object."""
    from analyze import load_sessions, MAX_DURATION_S
    from registry import Registry
    from alerts import count_alerts
    from propose import _read_ledger
    from wiki import get_wiki_stats

    reg = Registry()
    sessions = load_sessions(last_days=90)
    # Re-attribute each session to its canonical project from the stored cwd, using
    # the same git-aware resolver the hook uses. This is what makes a session run
    # outside ~/projects (e.g. ~/yon) show as its own project instead of 'global',
    # and collapses worktree dirs into their parent repo — retroactively, from data
    # already on disk. resolve_project is memoized (~100 distinct cwds).
    try:
        from projects import resolve_project
        for s in sessions:
            cwd = s.get("cwd")
            if cwd:
                s["project"] = resolve_project(cwd)
    except Exception:
        pass
    proposals = _read_ledger()

    data = {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {},
        "projects": [],
        "sessions_timeline": [],
        "tool_breakdown": {},
        "error_trends": [],
        "proposals": [],
        "alerts": count_alerts(),
        "wiki": get_wiki_stats(),
        "temporal": {"by_hour": {}, "by_dow": {}},
        "model_versions": {},
        "recent_sessions": [],
        "token_types": {},
        "per_project_timeline": {},
        "per_project_tools": {},
        "per_project_temporal": {},
    }

    # Summary + prior period for deltas
    now = datetime.utcnow()
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT")
    two_weeks_ago = (now - timedelta(days=14)).strftime("%Y-%m-%dT")

    current_week = [s for s in sessions if s.get("timestamp", "") >= week_ago]
    prior_week = [s for s in sessions if two_weeks_ago <= s.get("timestamp", "") < week_ago]

    def _period_stats(sess):
        return {
            "sessions": len(sess),
            "hours": round(sum(min(s.get("duration_s", 0), MAX_DURATION_S) for s in sess) / 3600, 1),
            "errors": sum(s.get("errors", 0) for s in sess),
            "cost": round(sum(_session_cost(s) for s in sess), 2),
            "tokens_m": round(sum(sum(s.get("tokens", {}).values()) for s in sess) / 1e6, 1),
        }

    cur = _period_stats(current_week)
    prev = _period_stats(prior_week)

    total_duration = sum(min(s.get("duration_s", 0), MAX_DURATION_S) for s in sessions)
    total_errors = sum(s.get("errors", 0) for s in sessions)
    total_tools = sum(sum(s.get("tool_uses", {}).values()) for s in sessions)
    total_cost = sum(_session_cost(s) for s in sessions)
    total_tokens = sum(
        sum(s.get("tokens", {}).values()) for s in sessions
    )

    def _delta(cur_val, prev_val):
        if prev_val == 0:
            return None
        return round((cur_val - prev_val) / prev_val * 100, 1)

    data["summary"] = {
        "total_sessions": len(sessions),
        "total_hours": round(total_duration / 3600, 1),
        "total_errors": total_errors,
        "total_tools": total_tools,
        "total_cost": round(total_cost, 2),
        "total_tokens_m": round(total_tokens / 1e6, 1),
        "projects": len(reg.projects),
        "pending_proposals": sum(1 for p in proposals.values() if p.get("status") == "pending"),
        "deltas": {
            "sessions": _delta(cur["sessions"], prev["sessions"]),
            "hours": _delta(cur["hours"], prev["hours"]),
            "errors": _delta(cur["errors"], prev["errors"]),
            "cost": _delta(cur["cost"], prev["cost"]),
        },
    }

    # Per-project breakdown
    by_project = defaultdict(lambda: {"sessions": 0, "errors": 0, "time": 0, "tools": 0})
    per_project_daily = defaultdict(lambda: defaultdict(lambda: {"sessions": 0, "errors": 0, "hours": 0}))
    per_project_tools = defaultdict(Counter)
    per_project_temporal = defaultdict(lambda: defaultdict(int))

    for s in sessions:
        p = s.get("project", "global")
        by_project[p]["sessions"] += 1
        by_project[p]["errors"] += s.get("errors", 0)
        by_project[p]["time"] += min(s.get("duration_s", 0), MAX_DURATION_S)
        by_project[p]["tools"] += sum(s.get("tool_uses", {}).values())

        # Per-project tools
        for tool, count in s.get("tool_uses", {}).items():
            per_project_tools[p][tool] += count

        # Per-project daily timeline
        date = s.get("timestamp", "")[:10]
        if date:
            per_project_daily[p][date]["sessions"] += 1
            per_project_daily[p][date]["errors"] += s.get("errors", 0)
            per_project_daily[p][date]["hours"] += min(s.get("duration_s", 0), MAX_DURATION_S) / 3600

        # Per-project temporal
        ts = s.get("timestamp", "")
        if len(ts) >= 13:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                per_project_temporal[p][str(dt.hour)] += 1
            except Exception:
                pass

    for proj, d in sorted(by_project.items(), key=lambda x: -x[1]["errors"]):
        arch = reg.get_archetype(proj)
        err_rate = d["errors"] / d["sessions"] if d["sessions"] > 0 else 0
        avg_h = d["time"] / d["sessions"] / 3600 if d["sessions"] > 0 else 0
        cost = d["errors"] * avg_h
        data["projects"].append({
            "name": proj,
            "archetype": arch,
            "sessions": d["sessions"],
            "errors": d["errors"],
            "error_rate": round(err_rate, 1),
            "hours": round(d["time"] / 3600, 1),
            "avg_hours": round(avg_h, 1),
            "tools": d["tools"],
            "cost": round(cost, 1),
        })

    # Sessions timeline (all available, up to 90 days)
    daily = defaultdict(lambda: {"sessions": 0, "errors": 0, "hours": 0})
    for s in sessions:
        date = s.get("timestamp", "")[:10]
        if date:
            daily[date]["sessions"] += 1
            daily[date]["errors"] += s.get("errors", 0)
            daily[date]["hours"] += min(s.get("duration_s", 0), MAX_DURATION_S) / 3600

    for date in sorted(daily.keys()):
        d = daily[date]
        data["sessions_timeline"].append({
            "date": date,
            "sessions": d["sessions"],
            "errors": d["errors"],
            "hours": round(d["hours"], 1),
        })

    # Per-project timeline
    for proj, dates in per_project_daily.items():
        data["per_project_timeline"][proj] = []
        for date in sorted(dates.keys()):
            d = dates[date]
            data["per_project_timeline"][proj].append({
                "date": date,
                "sessions": d["sessions"],
                "errors": d["errors"],
                "hours": round(d["hours"], 1),
            })

    # Per-project tools
    for proj, tools in per_project_tools.items():
        data["per_project_tools"][proj] = dict(tools.most_common(15))

    # Per-project temporal
    for proj, hours in per_project_temporal.items():
        data["per_project_temporal"][proj] = dict(hours)

    # Tool breakdown (global)
    tools = Counter()
    for s in sessions:
        for tool, count in s.get("tool_uses", {}).items():
            tools[tool] += count
    data["tool_breakdown"] = dict(tools.most_common(15))

    # Token type breakdown
    token_types = Counter()
    for s in sessions:
        for tok_type, count in (s.get("tokens") or {}).items():
            token_types[tok_type] += count
    data["token_types"] = dict(token_types)

    # Temporal patterns
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for s in sessions:
        ts = s.get("timestamp", "")
        if len(ts) >= 13:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                h = str(dt.hour)
                data["temporal"]["by_hour"][h] = data["temporal"]["by_hour"].get(h, 0) + 1
                dow = dow_names[dt.weekday()]
                data["temporal"]["by_dow"][dow] = data["temporal"]["by_dow"].get(dow, 0) + 1
            except Exception:
                pass

    # Model versions
    versions = Counter()
    for s in sessions:
        v = s.get("claude_version") or s.get("agent_version") or "unknown"
        versions[v] += 1
    data["model_versions"] = dict(versions.most_common(5))

    # Proposals
    for pid, p in sorted(proposals.items(), key=lambda x: x[1].get("timestamp", ""), reverse=True):
        data["proposals"].append({
            "id": pid,
            "project": p.get("project"),
            "type": p.get("type"),
            "description": p.get("description", "")[:120],
            "status": p.get("status"),
            "timestamp": p.get("timestamp", "")[:10],
        })

    # Recent sessions (last 10)
    recent = sorted(sessions, key=lambda s: s.get("timestamp", ""), reverse=True)[:10]
    for s in recent:
        data["recent_sessions"].append({
            "date": s.get("timestamp", "")[:16],
            "project": s.get("project"),
            "duration_m": min(s.get("duration_s", 0), MAX_DURATION_S) // 60,
            "errors": s.get("errors", 0),
            "tools": sum(s.get("tool_uses", {}).values()),
            "outcome": s.get("outcome", "unknown"),
        })

    return data
