"""Garbage collection — archive old data, expire alerts, prune proposals."""

import os
import json
import gzip
import shutil
from datetime import datetime, timedelta

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")


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

    # 2. Expire resolved alerts older than 7 days (alerts are deleted on dismiss, so this catches stale ones)
    alerts_dir = os.path.join(DATA_DIR, "alerts")
    expired = 0
    cutoff_7 = (datetime.utcnow() - timedelta(days=7)).timestamp()
    if os.path.isdir(alerts_dir):
        for f in os.listdir(alerts_dir):
            if f.endswith(".json"):
                path = os.path.join(alerts_dir, f)
                if os.path.getmtime(path) < cutoff_7:
                    if not dry_run:
                        os.remove(path)
                    expired += 1
    results["alerts_expired"] = expired

    # 3. Archive health check logs older than 30 days
    health_dir = os.path.join(DATA_DIR, "raw/health")
    health_archive = os.path.join(DATA_DIR, "archive/health")
    cutoff_30 = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    health_archived = 0
    if os.path.isdir(health_dir):
        os.makedirs(health_archive, exist_ok=True)
        for f in sorted(os.listdir(health_dir)):
            if f.endswith(".jsonl") and f[:10] < cutoff_30:
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

    # 5. Memory-router maintenance (sharded-router design 2026-07-19): refresh
    # the generated registry snapshot and lint-sweep memory namespaces so
    # on-disk violations and orphan counts surface weekly, not never.
    # 2026-07-22 (audit M3, P5b/c): the sweep now covers REGISTERED PROJECT
    # namespaces too, and a FAIL raises a real alert instead of a line in cron
    # stdout that nobody reads — the system was measuring this debt weekly
    # and telling no one.
    if not dry_run:
        try:
            import subprocess
            import sys as _sys
            _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import memorycmd
            memorycmd.router_gen()
            lint = os.path.expanduser("~/.claude/hooks/memory-lint.sh")
            targets = {"global": memorycmd.GLOBAL_MEM}
            try:
                from memoryns import memory_root, ns_dir
                from registry import Registry
                reg = Registry()
                for name in sorted(reg.projects):
                    path = reg.get_path(name)
                    mdir = os.path.join(ns_dir(memory_root(path)), "memory")
                    if os.path.isfile(os.path.join(mdir, "MEMORY.md")):
                        targets[name] = mdir
            except Exception:
                pass          # registry trouble degrades to the global-only sweep
            failing = {}
            if os.path.exists(lint):
                for name, mdir in sorted(targets.items()):
                    if not os.path.isdir(mdir):
                        continue
                    r = subprocess.run(["bash", lint, "--doctor", mdir],
                                       capture_output=True, text=True, timeout=120)
                    if r.returncode != 0:
                        out = (r.stdout or "") + (r.stderr or "")
                        first = next((l.strip() for l in out.splitlines() if l.strip()), "")
                        failing[name] = first[:200]
                results["memory_doctor"] = ("pass" if not failing
                                            else "FAIL: " + ", ".join(sorted(failing)))
                results["memory_namespaces_swept"] = len(targets)
                try:
                    from alerts import _create_alert, _dismiss_alert
                    if failing:
                        _create_alert(
                            "memory-doctor", "warning", "gc",
                            f"memory doctor FAILING in {len(failing)} namespace(s): "
                            f"{', '.join(sorted(failing))} — triage: clanker memory doctor",
                            details=failing)
                    else:
                        _dismiss_alert("memory-doctor")
                except Exception:
                    pass
        except Exception as e:
            results["memory_doctor"] = f"error: {e}"

    return results
