"""Autonomy scoreboard.

Replaces the err_rate>20 / avg_h>3 proposal loop (rejected 55/55, computed on
corrupted re-logged data) with the signal that actually matters for the stated
goal — making Claude Code run longer WITHOUT the human:

  - hands-off %      : sessions that finished with ZERO human corrections
  - corrections/sess : how often the human had to step in (the autonomy tax)
  - clean-outcome %  : sessions that reached a durable result (commit/push/deploy)
  - stuck %          : sessions that ended empty/abandoned

It ranks projects by where Claude most needs the human, and trends the
intervention rate week-over-week so you can tell whether autonomy work (auto-nudge,
catalog->guards) is actually moving the needle.

Signal honesty: `user_corrections` is a heuristic (human turns starting with
no/stop/wrong/revert, plus tool denials) and `outcome` is inferred — both are
proxies, not ground truth. They're directional, and far cleaner than the old
err_rate signal (which counted benign `grep`/`test` exit codes as "errors").
Runs on deduped real sessions via analyze.load_sessions.
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta

from analyze import load_sessions, MAX_DURATION_S

CLEAN_OUTCOMES = {"commit", "push", "deploy"}   # reached a durable result
STUCK_OUTCOMES = {"abandoned", "empty"}          # ended without progress


def _stats(sessions):
    n = len(sessions)
    if not n:
        return None
    corr = sum(s.get("user_corrections", 0) for s in sessions)
    return {
        "sessions": n,
        "corrections_total": corr,
        "corrections_per_session": corr / n,
        "hands_off_pct": sum(1 for s in sessions if s.get("user_corrections", 0) == 0) / n * 100,
        "clean_outcome_pct": sum(1 for s in sessions if s.get("outcome") in CLEAN_OUTCOMES) / n * 100,
        "stuck_pct": sum(1 for s in sessions if s.get("outcome") in STUCK_OUTCOMES) / n * 100,
        "subagents_total": sum(s.get("subagent_count", 0) for s in sessions),
        "hours": sum(min(s.get("duration_s", 0), MAX_DURATION_S) for s in sessions) / 3600,
    }


def report(last_days=30, json_output=False):
    sessions = load_sessions(last_days=last_days)
    overall = _stats(sessions)
    if not overall:
        print("No session data.")
        return

    # week-over-week intervention trend
    now = datetime.utcnow()
    wk = (now - timedelta(days=7)).strftime("%Y-%m-%dT")
    wk2 = (now - timedelta(days=14)).strftime("%Y-%m-%dT")
    cur = _stats([s for s in sessions if s.get("timestamp", "") >= wk])
    prev = _stats([s for s in sessions if wk2 <= s.get("timestamp", "") < wk])

    # per-project: where does Claude most need the human?
    by_proj = defaultdict(list)
    for s in sessions:
        by_proj[s.get("project", "global")].append(s)
    rows = []
    for proj, ss in by_proj.items():
        st = _stats(ss)
        if st and st["sessions"] >= 3:           # ignore thin samples
            rows.append((proj, st))
    rows.sort(key=lambda r: -r[1]["corrections_per_session"])

    if json_output:
        print(json.dumps({
            "overall": overall,
            "current_week": cur, "prior_week": prev,
            "by_project": {p: st for p, st in rows},
        }))
        return

    print(f"=== Autonomy Scoreboard (last {last_days}d) ===")
    print(f"  Sessions: {overall['sessions']}   Hours: {overall['hours']:.0f}   "
          f"Subagents dispatched: {overall['subagents_total']}")
    print(f"  Hands-off (0 corrections):  {overall['hands_off_pct']:.0f}% of sessions")
    print(f"  Corrections / session:      {overall['corrections_per_session']:.1f}  (the autonomy tax)")
    print(f"  Clean outcome (commit/push/deploy): {overall['clean_outcome_pct']:.0f}%")
    print(f"  Stuck (empty/abandoned):    {overall['stuck_pct']:.0f}%")

    if cur and prev:
        d = cur["corrections_per_session"] - prev["corrections_per_session"]
        arrow = "↓ improving" if d < 0 else "↑ worsening" if d > 0 else "→ flat"
        print(f"\n  Intervention trend: {prev['corrections_per_session']:.1f} → "
              f"{cur['corrections_per_session']:.1f} corr/session  ({arrow})")

    print(f"\n  Where Claude most needs you (by corrections/session, n>=3):")
    print(f"  {'Project':<28}{'Sess':>5}{'Corr/S':>8}{'HandsOff':>9}{'Stuck':>7}")
    print(f"  {'-'*56}")
    for proj, st in rows[:12]:
        print(f"  {proj:<28}{st['sessions']:>5}{st['corrections_per_session']:>8.1f}"
              f"{st['hands_off_pct']:>8.0f}%{st['stuck_pct']:>6.0f}%")

    print(f"\n  → Highest corr/session projects are the best targets for a guard hook")
    print(f"    or a pre-flight check. Trend should fall as autonomy work lands.")
