"""Alert system — health checks and alert management."""

import json
import os
import subprocess
from datetime import datetime

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")
ALERTS_DIR = os.path.join(DATA_DIR, "alerts")
REGISTRY_PATH = os.environ.get("CLANKER_REGISTRY", os.path.expanduser("~/projects/.clanker.yaml"))


def count_alerts():
    """Count active alerts."""
    if not os.path.isdir(ALERTS_DIR):
        return 0
    return len([f for f in os.listdir(ALERTS_DIR) if f.endswith(".json")])


def _create_alert(alert_id, severity, source, message, details=None):
    """Create an alert file.

    Re-raised alerts keep their original first_seen: the 15-min cron rewrites
    the file on every pass, so without this a chronically-ignored alert is
    indistinguishable from a fresh one (mtime and timestamp always look new).
    """
    os.makedirs(ALERTS_DIR, exist_ok=True)
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    path = os.path.join(ALERTS_DIR, f"{alert_id}.json")
    first_seen = now
    try:
        with open(path) as f:
            first_seen = json.load(f).get("first_seen", first_seen)
    except (OSError, json.JSONDecodeError):
        pass
    alert = {
        "id": alert_id,
        "timestamp": now,
        "first_seen": first_seen,
        "type": "health-check",
        "severity": severity,
        "source": source,
        "message": message,
        "details": details or {},
        "status": "active",
    }
    with open(path, "w") as f:
        json.dump(alert, f, indent=2)
    return alert


def _dismiss_alert(alert_id):
    """Dismiss an alert by deleting its file."""
    path = os.path.join(ALERTS_DIR, f"{alert_id}.json")
    if os.path.exists(path):
        os.remove(path)
        return True
    # Try partial match
    for f in os.listdir(ALERTS_DIR):
        if f.startswith(alert_id) and f.endswith(".json"):
            os.remove(os.path.join(ALERTS_DIR, f))
            return True
    return False


def _run_health_checks():
    """Run all health checks and create/dismiss alerts as needed."""
    results = {}

    # 1. Disk usage
    try:
        out = subprocess.check_output(["df", "/", "--output=pcent"], text=True)
        pct = int(out.strip().split("\n")[-1].replace("%", ""))
        results["disk"] = {"status": "warning" if pct > 80 else "ok", "value": pct}
        if pct > 80:
            _create_alert("disk-usage", "warning", "disk-check",
                         f"Disk usage at {pct}%", {"usage_pct": pct})
        else:
            _dismiss_alert("disk-usage")
    except:
        results["disk"] = {"status": "error", "value": None}

    # 2. Unpushed commits
    projects_dir = os.path.expanduser("~/projects")
    unpushed = {}
    if os.path.isdir(projects_dir):
        for d in os.listdir(projects_dir):
            path = os.path.join(projects_dir, d)
            if os.path.isdir(os.path.join(path, ".git")):
                try:
                    count = int(subprocess.check_output(
                        ["git", "-C", path, "rev-list", "--count", "@{upstream}..HEAD"],
                        stderr=subprocess.DEVNULL, text=True
                    ).strip())
                    if count > 20:
                        unpushed[d] = count
                except:
                    pass
    if unpushed:
        msg = ", ".join(f"{k}: {v}" for k, v in unpushed.items())
        _create_alert("unpushed-commits", "warning", "git-check",
                     f"Unpushed commits: {msg}", unpushed)
    else:
        _dismiss_alert("unpushed-commits")
    results["unpushed"] = {"status": "warning" if unpushed else "ok", "details": unpushed}

    # 3. Stale tmux sessions (Claude running but no output for 6h)
    # Lightweight check — just verify tmux is up
    try:
        subprocess.check_output(["tmux", "ls"], stderr=subprocess.DEVNULL, text=True)
        results["tmux"] = {"status": "ok"}
    except:
        _create_alert("tmux-down", "critical", "tmux-check", "tmux server is not running")
        results["tmux"] = {"status": "critical"}

    # 4. Tunnels — check each unit that exists on this host, by name. The old
    # check watched only 'cloudflared' while the dashboard rides
    # 'clanker-tunnel'; both being up let it pass for the wrong reason.
    for unit in ("clanker-tunnel", "cloudflared"):
        try:
            out = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True, text=True
            ).stdout.strip()
            if out == "active":
                _dismiss_alert(f"{unit}-down")
                results[unit] = {"status": "ok"}
            elif out in ("inactive", "failed", "activating", "deactivating"):
                _create_alert(f"{unit}-down", "critical", "tunnel-check",
                              f"{unit} tunnel is not running (systemctl: {out})")
                results[unit] = {"status": "critical"}
            else:
                # unit not installed on this host — absence is not an outage
                results[unit] = {"status": "absent"}
        except Exception:
            results[unit] = {"status": "unknown"}

    # 5. Stale lock files
    lock_file = os.path.expanduser("~/.claude/scheduled_tasks.lock")
    if os.path.exists(lock_file):
        try:
            with open(lock_file) as f:
                lock = json.load(f)
            pid = lock.get("pid", 0)
            try:
                os.kill(pid, 0)
                results["locks"] = {"status": "ok"}
            except ProcessLookupError:
                _create_alert("stale-lock", "info", "lock-check",
                             f"Stale lock: scheduled_tasks.lock (PID {pid} dead)")
                results["locks"] = {"status": "info"}
        except:
            results["locks"] = {"status": "ok"}
    else:
        results["locks"] = {"status": "ok"}
        _dismiss_alert("stale-lock")

    # 6. Optional ECC-derived alerts — ONLY when the operator opted in (env/registry).
    #    When disabled this block is a no-op, so default behaviour is unchanged.
    try:
        from ecc import enabled as _ecc_enabled
        if _ecc_enabled():
            from ecc import parked as _ecc_parked, overlap as _ecc_overlap
            att = [r for r in _ecc_parked.scan_dir() if r.get("state") == "attention"]
            if att:
                _create_alert("ecc-parked", "warning", "ecc-parked",
                              f"{len(att)} Claude session(s) parked/waiting for input",
                              {"sessions": [r["session"] for r in att[:10]]})
            else:
                _dismiss_alert("ecc-parked")
            results["ecc_parked"] = {"status": "warning" if att else "ok", "value": len(att)}
            try:
                from analyze import load_sessions
                ov = _ecc_overlap.file_overlaps(load_sessions(last_days=7))
                if ov:
                    _create_alert("ecc-overlap", "info", "ecc-overlap",
                                  f"{len(ov)} file(s) edited by >1 active session",
                                  {"files": [o["file"] for o in ov[:10]]})
                else:
                    _dismiss_alert("ecc-overlap")
                results["ecc_overlap"] = {"status": "info" if ov else "ok", "value": len(ov)}
            except Exception:
                pass
    except Exception:
        pass

    return results


def handle_alert(action, alert_id=None, message=None, cron=False, json_output=False):
    """Handle alert subcommands."""
    if action == "check":
        results = _run_health_checks()
        if cron:
            # Output as JSONL for cron logging
            record = {
                "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "checks": results,
            }
            print(json.dumps(record))
        else:
            print("=== Health Check Results ===")
            for check, result in results.items():
                status = result.get("status", "?")
                icon = "✓" if status == "ok" else "✗" if status in ("critical", "error") else "⚠"
                print(f"  {icon} {check}: {status}")
                if result.get("details"):
                    print(f"    {result['details']}")
                if result.get("value") is not None:
                    print(f"    value: {result['value']}")

    elif action == "list":
        if not os.path.isdir(ALERTS_DIR):
            print("No alerts.")
            return
        files = sorted(f for f in os.listdir(ALERTS_DIR) if f.endswith(".json"))
        if not files:
            print("No active alerts.")
            return
        for f in files:
            with open(os.path.join(ALERTS_DIR, f)) as fh:
                alert = json.load(fh)
            if json_output:
                print(json.dumps(alert))
            else:
                print(f"  [{alert.get('severity', '?')}] {alert.get('message', '?')}")
                print(f"    ID: {alert.get('id')}  Source: {alert.get('source')}  Time: {alert.get('timestamp')}")

    elif action == "dismiss":
        if not alert_id:
            print("Usage: clanker alert dismiss --id <alert-id>")
            return
        if _dismiss_alert(alert_id):
            print(f"Dismissed: {alert_id}")
        else:
            print(f"Alert not found: {alert_id}")

    elif action == "create":
        if not message:
            print("Usage: clanker alert create --message 'description'")
            return
        alert_id = f"manual-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
        _create_alert(alert_id, "info", "manual", message)
        print(f"Created: {alert_id}")
