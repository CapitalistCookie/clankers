"""Garbage collection — archive old data, expire alerts, prune proposals."""

import os
import json
import gzip
import re
import shutil
from datetime import datetime, timedelta

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")

# A health log's day: the whole name (2026-09-24.jsonl, the alert-check cron)
# or its last part (hook-errors-2026-09-24.jsonl, the hook-error log).
HEALTH_DAY_RE = re.compile(r"(?:^|-)(\d{4}-\d{2}-\d{2})\.jsonl$")


def run_gc(dry_run=False):
    """Run garbage collection across all data stores."""
    results = {}

    # 1. Archive session logs older than 90 days
    sessions_dir = os.path.join(DATA_DIR, "raw/sessions")
    archive_dir = os.path.join(DATA_DIR, "archive/sessions")
    cutoff_90 = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
    archived = 0
    if os.path.isdir(sessions_dir):
        os.makedirs(archive_dir, exist_ok=True)
        for f in sorted(os.listdir(sessions_dir)):
            if f.endswith(".jsonl") and f[:10] < cutoff_90:
                src = os.path.join(sessions_dir, f)
                dst = os.path.join(archive_dir, f + ".gz")
                if not dry_run:
                    with open(src, "rb") as fin, gzip.open(dst, "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                    os.remove(src)
                archived += 1
    results["sessions_archived"] = archived

    # 2. Alert expiry runs last: see the end of this function.

    # 3. Archive health logs older than 30 days. The day may end the name
    # (hook-errors-<day>.jsonl, 2026-09-24): f[:10] alone never archived those.
    health_dir = os.path.join(DATA_DIR, "raw/health")
    health_archive = os.path.join(DATA_DIR, "archive/health")
    cutoff_30 = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    health_archived = 0
    if os.path.isdir(health_dir):
        os.makedirs(health_archive, exist_ok=True)
        for f in sorted(os.listdir(health_dir)):
            m = HEALTH_DAY_RE.search(f)
            if m and m.group(1) < cutoff_30:
                src = os.path.join(health_dir, f)
                dst = os.path.join(health_archive, f + ".gz")
                if not dry_run:
                    with open(src, "rb") as fin, gzip.open(dst, "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                    os.remove(src)
                health_archived += 1
    results["health_archived"] = health_archived

    # 4. Compact proposal ledger (remove rejected proposals older than 30 days)
    ledger_path = os.path.join(DATA_DIR, "proposals/ledger.jsonl")
    pruned = 0
    if os.path.exists(ledger_path):
        cutoff_30_str = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT")
        entries = []
        with open(ledger_path) as f:
            for line in f:
                if line.strip():
                    try:
                        entry = json.loads(line)
                        # Keep if: pending, accepted, implemented, or rejected < 30 days ago
                        if entry.get("status") == "rejected" and entry.get("decided_at", "9") < cutoff_30_str:
                            pruned += 1
                        else:
                            entries.append(line)
                    except:
                        entries.append(line)
        if not dry_run and pruned > 0:
            with open(ledger_path, "w") as f:
                f.writelines(entries)
    results["proposals_pruned"] = pruned

    # 5. Memory maintenance is off (2026-09-24). Auto-memory is disabled since
    # 2026-08-08 (autoMemoryEnabled: false) and memory-lint.sh is retired, so
    # gc no longer regenerates ROUTER-AUTO.md in the memory dir, lint-sweeps
    # the namespaces or raises the memory-doctor alert: nothing may write to a
    # memory dir, and the sweep had nothing to run. `clanker memory
    # router-gen` still runs on demand.
    results["memory_doctor"] = ("skipped: auto-memory is off since 2026-08-08 "
                                "and memory-lint is retired")
    if not dry_run:
        # --- orphaned scheduled work -------------------------------------
        # The removal GATE (onboard.remove_project) prevents NEW orphans, but
        # cron installed outside clanker, or a project retired before the gate
        # existed, still needs finding. yon's nightly CI ran 48 days past the
        # repo's last commit because nothing ever looked.
        try:
            import schedules
            from registry import Registry
            try:
                reg = Registry()
                known = [p for p in (reg.get_path(n) for n in reg.projects) if p]
            except Exception:
                known = []
            active = [i for i in schedules.scan() if i["active"]]
            rogue = schedules.unowned(active, known_paths=known)
            results["schedules_orphaned"] = len(rogue)
            try:
                from alerts import _create_alert, _dismiss_alert
                if rogue:
                    repos = sorted({p for i in rogue for p in i["paths"]})
                    _create_alert(
                        "schedules-orphaned", "warning", "gc",
                        f"{len(rogue)} active scheduled item(s) owned by "
                        f"{len(repos)} unregistered repo(s): {', '.join(repos[:3])}"
                        f"{' …' if len(repos) > 3 else ''} — triage: "
                        f"clanker schedules audit",
                        details={p: sum(1 for i in rogue if p in i["paths"])
                                 for p in repos})
                else:
                    _dismiss_alert("schedules-orphaned")
            except Exception:
                pass
        except Exception as e:
            results["schedules_orphaned"] = f"error: {e}"

    # 6. Expire alerts 7 days after their NEWEST raise (alerts are deleted on
    # dismiss, so this catches stale ones): timestamp, else ts, else
    # first_seen; the file mtime only when none of them exists (2026-09-24).
    # mtime alone never expired anything (the 15-minute escalation pass rewrote
    # every alert), and first_seen expired the standing alerts producers
    # re-raise every 15 minutes: each came back the next pass with first_seen,
    # ignored_days and escalated_at reset, and escalated again 3 days later.
    # _escalate_ignored keeps first_seen as its clock. This step runs last so
    # an alert raised by this run (step 5 re-raises schedules-orphaned
    # weekly) is judged on that raise, not last week's.
    alerts_dir = os.path.join(DATA_DIR, "alerts")
    expired = 0
    if os.path.isdir(alerts_dir):
        import sys as _sys
        _lib = os.path.dirname(os.path.abspath(__file__))
        if _lib not in _sys.path:
            _sys.path.insert(0, _lib)
        from alerts import alert_last_raised, _utcnow
        cutoff_7 = _utcnow() - timedelta(days=7)
        for f in os.listdir(alerts_dir):
            if f.endswith(".json"):
                path = os.path.join(alerts_dir, f)
                try:
                    with open(path) as fh:
                        alert = json.load(fh)
                except (OSError, ValueError):
                    alert = {}                  # unreadable: only the mtime is left
                raised = alert_last_raised(alert, path)
                if raised is not None and raised < cutoff_7:
                    if not dry_run:
                        try:
                            os.remove(path)
                        except FileNotFoundError:
                            continue            # dismissed meanwhile
                    expired += 1
    results["alerts_expired"] = expired

    return results
