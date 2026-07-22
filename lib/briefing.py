"""Project briefings — dynamic 'what's happening now' for SessionStart."""

import json
import os
import subprocess
from datetime import datetime, timedelta

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")


def generate_briefing(project, project_path=None):
    """Generate a project briefing for SessionStart injection."""
    if not project_path:
        project_path = os.path.expanduser(f"~/projects/{project}")

    sections = []

    # 1. Git status
    if os.path.isdir(os.path.join(project_path, ".git")):
        try:
            branch = subprocess.check_output(
                ["git", "-C", project_path, "branch", "--show-current"],
                stderr=subprocess.DEVNULL, text=True
            ).strip()
            commits = subprocess.check_output(
                ["git", "-C", project_path, "log", "--oneline", "-5"],
                stderr=subprocess.DEVNULL, text=True
            ).strip()
            dirty = subprocess.check_output(
                ["git", "-C", project_path, "status", "--short"],
                stderr=subprocess.DEVNULL, text=True
            ).strip()
            try:
                unpushed = subprocess.check_output(
                    ["git", "-C", project_path, "rev-list", "--count", "@{upstream}..HEAD"],
                    stderr=subprocess.DEVNULL, text=True
                ).strip()
            except subprocess.CalledProcessError:
                unpushed = "0"  # no upstream configured: don't drop the whole section

            git_section = f"Branch: {branch}"
            if int(unpushed or "0") > 0:
                git_section += f" ({unpushed} unpushed)"
            if dirty:
                dirty_count = len(dirty.split("\n"))
                git_section += f" | {dirty_count} dirty files"
            git_section += f"\nRecent commits:\n{commits}"
            sections.append(("Git", git_section))
        except:
            pass

    # 2. Recent sessions
    from analyze import load_sessions
    sessions = load_sessions(last_days=7)
    proj_sessions = [s for s in sessions if s.get("project") == project]
    if proj_sessions:
        recent = proj_sessions[-3:]  # last 3
        session_lines = []
        for s in recent:
            ts = s.get("timestamp", "")[:16]
            dur = ("live" if s.get("outcome") == "open"
                   else f"{min(s.get('duration_s', 0), 28800) // 60}m")
            errs = s.get("errors", 0)
            session_lines.append(f"  {ts} | {dur} | {errs} errors")
        sections.append(("Recent Sessions", "\n".join(session_lines)))

    # 3. Open alerts (P12): a project-tagged alert belongs to exactly one
    # briefing; untagged alerts are global and shown everywhere, labeled
    # honestly. ignored_days (stamped by the health cron) is surfaced so a
    # chronically-ignored warning reads as such.
    alerts_dir = os.path.join(DATA_DIR, "alerts")
    if os.path.isdir(alerts_dir):
        mine, global_msgs = [], []
        for f in os.listdir(alerts_dir):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(alerts_dir, f)) as fh:
                        alert = json.load(fh)
                    aproj = alert.get("project")
                    if aproj and aproj != project:
                        continue
                    ig = alert.get("ignored_days") or 0
                    line = "  - " + ("[ignored %dd] " % ig if ig else "") + alert.get("message", "")
                    (mine if aproj else global_msgs).append(line)
                except Exception:
                    pass
        if mine:
            sections.append((f"Alerts ({project})", "\n".join(sorted(mine))))
        if global_msgs:
            sections.append(("System Alerts (global)", "\n".join(sorted(global_msgs))))

    # 4. Pending proposals for this project
    from propose import _read_ledger
    proposals = _read_ledger()
    proj_proposals = [p for p in proposals.values()
                      if p.get("project") == project and p.get("status") == "pending"]
    if proj_proposals:
        prop_lines = [f"  - {p.get('description', '?')[:80]}" for p in proj_proposals]
        sections.append(("Pending Proposals", "\n".join(prop_lines)))

    # 5. Handoff from last session
    handoff_path = os.path.join(DATA_DIR, "wiki", "projects", f"{project}-handoff.md")
    if os.path.exists(handoff_path):
        with open(handoff_path) as f:
            handoff = f.read().strip()
        if handoff:
            # Truncate to 500 chars
            if len(handoff) > 500:
                handoff = handoff[:500] + "..."
            sections.append(("Last Session Handoff", handoff))

    # 6. Contract drift (harness overhaul M4 — the self-healing surface: doctor's
    # findings appear at every session start until fixed)
    try:
        from adopt import doctor_summary
        drift = doctor_summary(project)
        if drift:
            sections.append(("Contract", f"  ⚠ {drift}"))
    except Exception:
        pass

    # Build briefing
    if not sections:
        return None

    briefing = f"=== Project Briefing: {project} ===\n"
    for title, content in sections:
        briefing += f"\n{title}:\n{content}\n"

    return briefing
