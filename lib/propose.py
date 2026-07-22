"""Proposal ledger — generate, file manually, review, accept/reject/resolve."""

import json
import os
import re
import sys
from datetime import datetime

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")
LEDGER_PATH = os.path.join(DATA_DIR, "proposals/ledger.jsonl")


def _read_ledger():
    """Read all proposals, last-write-wins by ID. Corrupt lines are counted
    and reported to stderr, never silently swallowed (audit L7 — a bare
    except here was silent data loss in the improvement pipeline)."""
    proposals = {}
    bad = 0
    if os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                    proposals[p["id"]] = p
                except (json.JSONDecodeError, KeyError, TypeError):
                    bad += 1
    if bad:
        print(f"propose: WARNING — {bad} corrupt ledger line(s) skipped "
              f"({LEDGER_PATH})", file=sys.stderr)
    return proposals


def _append_ledger(entry):
    """Append an entry to the ledger."""
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def count_pending():
    """Count pending proposals."""
    return sum(1 for p in _read_ledger().values() if p.get("status") == "pending")


def expire_stale(days=30, before=None, reason=None):
    """Auto-expire pending proposals older than the cutoff (ledger hygiene,
    2026-07-05: 28 pre-overhaul analyzer proposals sat pending for weeks —
    a surface nobody reads is not a surface). Appends status-expired entries
    (last-write-wins). Returns the number expired."""
    from datetime import timedelta, timezone
    if before:
        cutoff = datetime.fromisoformat(before).replace(tzinfo=timezone.utc)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    n = 0
    for p in _read_ledger().values():
        if p.get("status") != "pending":
            continue
        ts = p.get("timestamp") or ""
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < cutoff:
            p["status"] = "expired"
            p["expired_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            p["expire_reason"] = reason or f"auto-expired: pending >{days}d unreviewed"
            _append_ledger(p)
            n += 1
    if n:
        print(f"expired {n} stale pending proposal(s)")
    return n


def add_proposal(project, description, impact, ptype="manual", source="manual"):
    """File a proposal by hand — schema-faithful to the generated entries
    (audit L7: the 07-22 audit had to append raw JSON to the ledger to file
    the very proposals this verb now files). Returns the id, or None on a
    duplicate."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    slug = re.sub(r"[^a-z0-9]+", "-", (description or "").lower()).strip("-")[:40] or "manual"
    prop_id = f"prop-{today}-{project}-{slug}"
    if prop_id in _read_ledger():
        print(f"propose: {prop_id} already exists — not duplicated", file=sys.stderr)
        return None
    _append_ledger({
        "id": prop_id,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "source_session": None,
        "project": project,
        "type": ptype,
        "description": description,
        "expected_impact": impact,
        "status": "pending",
        "decided_at": None,
        "decided_by": None,
        "implemented_at": None,
        "baseline_metric": None,
        "actual_metric": None,
        "notes": "",
    })
    print(f"FILED: {prop_id}")
    return prop_id


def resolve_proposal(query, status="implemented", note=""):
    """Mark a proposal implemented/rejected by exact id, else by a substring
    that matches exactly ONE non-terminal proposal. Appends the updated row
    (last-write-wins). Exit-code honest: 0 on success, 1 otherwise."""
    if status not in ("implemented", "accepted", "rejected"):
        print(f"propose: invalid --status '{status}'", file=sys.stderr)
        return 1
    props = _read_ledger()
    if query in props:
        matches = [query]
    else:
        matches = [pid for pid, p in props.items()
                   if query in pid and p.get("status") in ("pending", "accepted")]
    if not matches:
        print(f"propose: no open proposal matches '{query}'", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"propose: '{query}' is ambiguous ({len(matches)} matches):",
              file=sys.stderr)
        for pid in sorted(matches):
            print(f"  {pid}", file=sys.stderr)
        return 1
    pid = matches[0]
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    update = {**props[pid], "status": status, "decided_at": props[pid].get("decided_at") or now,
              "decided_by": props[pid].get("decided_by") or "user"}
    if status == "implemented":
        update["implemented_at"] = now
    if note:
        update["notes"] = (update.get("notes") or "")
        update["notes"] = (update["notes"] + " | " if update["notes"] else "") + note
    _append_ledger(update)
    print(f"{status.upper()}: {pid}")
    return 0


def generate_proposals(from_retro=False):
    """Generate improvement proposals from analysis data."""
    # Self-maintaining ledger: every weekly generation first sweeps out
    # proposals that sat pending >30 days (they described a harness that no
    # longer exists by then).
    try:
        expire_stale(days=30)
    except Exception:
        pass
    from analyze import load_sessions
    from collections import defaultdict

    sessions = load_sessions(last_days=7)
    if not sessions:
        print("No session data to analyze.")
        return

    existing = _read_ledger()
    by_project = defaultdict(lambda: {"count": 0, "errors": 0, "time": 0})
    for s in sessions:
        if s.get("outcome") == "open":
            continue   # P7 heartbeat stub — still running; no final numbers yet
        p = s.get("project", "global")
        by_project[p]["count"] += 1
        by_project[p]["errors"] += s.get("errors", 0)
        by_project[p]["time"] += min(s.get("duration_s", 0), 28800)  # cap at 8h

    proposals_made = 0
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.utcnow().strftime("%Y-%m-%d")

    for p, d in by_project.items():
        err_rate = d["errors"] / d["count"] if d["count"] > 0 else 0
        avg_h = d["time"] / d["count"] / 3600 if d["count"] > 0 else 0
        cost = d["errors"] * avg_h

        # Check for duplicates
        def has_similar(proj, ptype):
            for ex in existing.values():
                if ex.get("project") == proj and ex.get("type") == ptype and ex.get("status") == "pending":
                    return True
            return False

        if err_rate > 20 and not has_similar(p, "error-pattern"):
            prop_id = f"prop-{today}-{p}-errors"
            entry = {
                "id": prop_id,
                "timestamp": now,
                "source": "weekly-analysis",
                "source_session": None,
                "project": p,
                "type": "error-pattern",
                "description": f"{p} averages {err_rate:.0f} errors/session ({d['errors']} total in {d['count']} sessions). Investigate common failure modes.",
                "expected_impact": f"Reduce {p} errors by identifying and addressing top error patterns",
                "status": "pending",
                "decided_at": None,
                "decided_by": None,
                "implemented_at": None,
                "baseline_metric": {"error_rate": round(err_rate, 1), "window": f"{today}..{today}"},
                "actual_metric": None,
                "notes": "",
            }
            _append_ledger(entry)
            print(f"PROPOSED: [{p}] {entry['description']}")
            proposals_made += 1

        if avg_h > 3 and not has_similar(p, "time-sink"):
            prop_id = f"prop-{today}-{p}-time"
            entry = {
                "id": prop_id,
                "timestamp": now,
                "source": "weekly-analysis",
                "source_session": None,
                "project": p,
                "type": "time-sink",
                "description": f"{p} sessions average {avg_h:.1f}h. Consider pre-flight checks or task decomposition.",
                "expected_impact": f"Reduce average session time for {p}",
                "status": "pending",
                "decided_at": None,
                "decided_by": None,
                "implemented_at": None,
                "baseline_metric": {"avg_hours": round(avg_h, 1), "window": f"{today}..{today}"},
                "actual_metric": None,
                "notes": "",
            }
            _append_ledger(entry)
            print(f"PROPOSED: [{p}] {entry['description']}")
            proposals_made += 1

    if proposals_made == 0:
        print("No new proposals — all projects within normal parameters.")
    else:
        print(f"\n{proposals_made} proposal(s) generated.")


def review_proposals():
    """Interactive review of pending proposals."""
    proposals = _read_ledger()
    pending = {pid: p for pid, p in proposals.items() if p.get("status") == "pending"}

    if not pending:
        print("No pending proposals.")
        return

    print(f"=== {len(pending)} Pending Proposals ===\n")
    for i, (pid, p) in enumerate(pending.items(), 1):
        cost = ""
        if p.get("baseline_metric"):
            bm = p["baseline_metric"]
            if "error_rate" in bm:
                cost = f" (err_rate={bm['error_rate']})"
            elif "avg_hours" in bm:
                cost = f" (avg={bm['avg_hours']}h)"

        print(f"  {i}. [{p['project']}] {p['type']}: {p['description']}{cost}")
        print(f"     ID: {pid}")
        print()

    print("Actions: [a]ccept <num>, [r]eject <num>, [s]kip, [q]uit")
    # In non-interactive mode (piped), just list
    if not os.isatty(0):
        return

    while True:
        try:
            choice = input("\n> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if choice in ("q", "quit"):
            break
        elif choice in ("s", "skip"):
            continue
        elif choice.startswith("a"):
            parts = choice.split()
            if len(parts) >= 2:
                try:
                    idx = int(parts[1]) - 1
                    pid = list(pending.keys())[idx]
                    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                    update = {**pending[pid], "status": "accepted", "decided_at": now, "decided_by": "user"}
                    _append_ledger(update)
                    print(f"Accepted: {pid}")
                    del pending[pid]
                except (ValueError, IndexError):
                    print("Invalid number")
        elif choice.startswith("r"):
            parts = choice.split()
            if len(parts) >= 2:
                try:
                    idx = int(parts[1]) - 1
                    pid = list(pending.keys())[idx]
                    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                    update = {**pending[pid], "status": "rejected", "decided_at": now, "decided_by": "user"}
                    _append_ledger(update)
                    print(f"Rejected: {pid}")
                    del pending[pid]
                except (ValueError, IndexError):
                    print("Invalid number")

        if not pending:
            print("All proposals reviewed.")
            break
