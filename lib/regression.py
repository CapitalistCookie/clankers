"""Regression detection — track metrics before/after harness changes."""

import json
import os
from datetime import datetime, timedelta
from collections import defaultdict

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")
LEDGER_PATH = os.path.join(DATA_DIR, "proposals/ledger.jsonl")


def check_regressions(days_after=14):
    """Check implemented proposals for regression/improvement.

    For each implemented proposal, compare baseline metrics to current metrics.
    """
    from analyze import load_sessions
    from propose import _read_ledger, _append_ledger

    proposals = _read_ledger()
    implemented = {pid: p for pid, p in proposals.items()
                   if p.get("status") == "implemented" and p.get("implemented_at")}

    if not implemented:
        print("No implemented proposals to check.")
        return

    print("=== Regression Check ===\n")
    updated = 0

    for pid, prop in implemented.items():
        project = prop.get("project")
        baseline = prop.get("baseline_metric", {})
        impl_date = prop.get("implemented_at", "")

        if not impl_date or not baseline:
            continue

        # Check if enough time has passed
        try:
            impl_dt = datetime.fromisoformat(impl_date.replace("Z", "+00:00"))
            elapsed = (datetime.now(tz=impl_dt.tzinfo) - impl_dt).days
        except:
            elapsed = 0

        if elapsed < days_after:
            print(f"  {pid}: implemented {elapsed}d ago — waiting for {days_after}d window")
            continue

        # Load sessions after implementation
        all_sessions = load_sessions(last_days=elapsed)
        proj_sessions = [s for s in all_sessions if s.get("project") == project]

        if not proj_sessions:
            print(f"  {pid}: no sessions for {project} since implementation")
            continue

        # Compute current metrics
        current = {}
        if "error_rate" in baseline:
            current["error_rate"] = round(
                sum(s.get("errors", 0) for s in proj_sessions) / len(proj_sessions), 1
            )
            baseline_val = baseline["error_rate"]
            change = current["error_rate"] - baseline_val
            pct = (change / baseline_val * 100) if baseline_val > 0 else 0
            direction = "improved" if change < 0 else "regressed" if change > 0 else "unchanged"
            print(f"  {pid} [{project}]:")
            print(f"    Error rate: {baseline_val} → {current['error_rate']} ({pct:+.0f}%) — {direction}")

        if "avg_hours" in baseline:
            avg_h = sum(min(s.get("duration_s", 0), 28800) for s in proj_sessions) / len(proj_sessions) / 3600
            current["avg_hours"] = round(avg_h, 1)
            baseline_val = baseline["avg_hours"]
            change = current["avg_hours"] - baseline_val
            pct = (change / baseline_val * 100) if baseline_val > 0 else 0
            direction = "improved" if change < 0 else "regressed" if change > 0 else "unchanged"
            print(f"    Avg hours: {baseline_val} → {current['avg_hours']} ({pct:+.0f}%) — {direction}")

        # Update the proposal with actual metrics
        if current and not prop.get("actual_metric"):
            update = {**prop, "actual_metric": current}
            _append_ledger(update)
            updated += 1

    if updated:
        print(f"\nUpdated {updated} proposal(s) with actual metrics.")
    print()


def detect_anomalies(lookback_days=7, compare_days=30):
    """Detect metric anomalies — significant changes from historical baseline."""
    from analyze import load_sessions

    recent = load_sessions(last_days=lookback_days)
    historical = load_sessions(last_days=compare_days)

    if len(recent) < 3 or len(historical) < 10:
        print("Not enough data for anomaly detection.")
        return

    # Compare error rates by project
    def project_stats(sessions):
        by_project = defaultdict(lambda: {"count": 0, "errors": 0})
        for s in sessions:
            p = s.get("project", "global")
            by_project[p]["count"] += 1
            by_project[p]["errors"] += s.get("errors", 0)
        return by_project

    recent_stats = project_stats(recent)
    hist_stats = project_stats(historical)

    print("=== Anomaly Detection ===\n")
    anomalies = []

    for project in set(list(recent_stats.keys()) + list(hist_stats.keys())):
        r = recent_stats.get(project, {"count": 0, "errors": 0})
        h = hist_stats.get(project, {"count": 0, "errors": 0})

        if r["count"] < 2 or h["count"] < 5:
            continue

        recent_rate = r["errors"] / r["count"]
        hist_rate = h["errors"] / h["count"]

        if hist_rate > 0 and recent_rate > hist_rate * 2:
            anomalies.append({
                "project": project,
                "type": "error_spike",
                "recent_rate": round(recent_rate, 1),
                "historical_rate": round(hist_rate, 1),
                "change_pct": round((recent_rate - hist_rate) / hist_rate * 100),
            })
            print(f"  ⚠ {project}: error rate {hist_rate:.1f} → {recent_rate:.1f} ({anomalies[-1]['change_pct']:+d}%)")
        elif hist_rate > 0 and recent_rate < hist_rate * 0.5:
            print(f"  ✓ {project}: error rate improved {hist_rate:.1f} → {recent_rate:.1f}")

    if not anomalies:
        print("  No anomalies detected — all projects within normal range.")

    return anomalies
