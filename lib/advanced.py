"""Tier 3 advanced features — stubs with working interfaces.

These features need more data or external systems to be fully functional.
They provide the CLI interface and basic logic now, with TODO markers for
full implementation.
"""

import json
import os
from datetime import datetime, timedelta
from collections import defaultdict, Counter

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")


def _global_memory_dir():
    from memoryns import GLOBAL_MEMORY  # $HOME-derived, never a hardcoded slug
    return GLOBAL_MEMORY


def resource_monitor():
    """Check VM resource usage — CPU, RAM, swap, disk I/O."""
    import subprocess
    results = {}

    # CPU load
    try:
        with open("/proc/loadavg") as f:
            load = f.read().split()
        results["load_1m"] = float(load[0])
        results["load_5m"] = float(load[1])
        results["load_15m"] = float(load[2])
    except:
        pass

    # Memory
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = int(parts[1].strip().split()[0])
                    meminfo[key] = val
        total = meminfo.get("MemTotal", 0) / 1024 / 1024
        available = meminfo.get("MemAvailable", 0) / 1024 / 1024
        swap_total = meminfo.get("SwapTotal", 0) / 1024 / 1024
        swap_free = meminfo.get("SwapFree", 0) / 1024 / 1024
        results["ram_total_gb"] = round(total, 1)
        results["ram_available_gb"] = round(available, 1)
        results["ram_used_pct"] = round((1 - available / total) * 100, 1) if total > 0 else 0
        results["swap_total_gb"] = round(swap_total, 1)
        results["swap_used_gb"] = round(swap_total - swap_free, 1)
    except:
        pass

    # Disk
    try:
        out = subprocess.check_output(["df", "/", "--output=pcent,avail"], text=True)
        lines = out.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            results["disk_used_pct"] = int(parts[0].replace("%", ""))
            results["disk_avail_gb"] = round(int(parts[1]) / 1024 / 1024, 1)
    except:
        pass

    return results


def model_version_analysis(last_days=30):
    """Analyze sessions by Claude Code version."""
    from analyze import load_sessions
    sessions = load_sessions(last_days=last_days)

    by_version = defaultdict(lambda: {"count": 0, "errors": 0, "tools": 0})
    for s in sessions:
        v = s.get("claude_version") or "unknown"   # value may be explicit null
        by_version[v]["count"] += 1
        by_version[v]["errors"] += s.get("errors", 0)
        by_version[v]["tools"] += sum(s.get("tool_uses", {}).values())

    print(f"{'Version':<15} {'Sessions':>8} {'Errors':>8} {'Err/Sess':>8}")
    print("-" * 44)
    for v in sorted(by_version, key=lambda x: -by_version[x]["count"]):
        d = by_version[v]
        rate = d["errors"] / d["count"] if d["count"] > 0 else 0
        print(f"{v:<15} {d['count']:>8} {d['errors']:>8} {rate:>8.1f}")


def temporal_analysis(last_days=30):
    """Analyze sessions by time-of-day and day-of-week."""
    from analyze import load_sessions
    sessions = load_sessions(last_days=last_days)

    by_hour = defaultdict(lambda: {"count": 0, "errors": 0})
    by_dow = defaultdict(lambda: {"count": 0, "errors": 0})
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    for s in sessions:
        ts = s.get("timestamp", "")
        if len(ts) >= 13:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                hour = dt.hour
                dow = dt.weekday()
                by_hour[hour]["count"] += 1
                by_hour[hour]["errors"] += s.get("errors", 0)
                by_dow[dow]["count"] += 1
                by_dow[dow]["errors"] += s.get("errors", 0)
            except:
                pass

    print("=== By Hour (UTC) ===")
    print(f"{'Hour':>4} {'Sessions':>8} {'Errors':>8} {'Err/Sess':>8}")
    for h in range(24):
        if h in by_hour:
            d = by_hour[h]
            rate = d["errors"] / d["count"] if d["count"] > 0 else 0
            bar = "#" * min(d["count"], 30)
            print(f"{h:>4} {d['count']:>8} {d['errors']:>8} {rate:>8.1f}  {bar}")

    print("\n=== By Day of Week ===")
    for dow in range(7):
        if dow in by_dow:
            d = by_dow[dow]
            rate = d["errors"] / d["count"] if d["count"] > 0 else 0
            bar = "#" * min(d["count"], 30)
            print(f"{dow_names[dow]:>4} {d['count']:>8} {d['errors']:>8} {rate:>8.1f}  {bar}")


def harness_version_snapshot():
    """Snapshot current harness state for version tracking."""
    import glob

    snapshot = {
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hooks": len(glob.glob(os.path.expanduser("~/.claude/hooks/*.sh")) +
                     glob.glob(os.path.expanduser("~/.claude/hooks/*.py"))),
        "skills": len(glob.glob(os.path.expanduser("~/.claude/skills/*/"))),
        "memory_files": len(glob.glob(os.path.join(_global_memory_dir(), "*.md"))),
        "projects": 0,
        "archetypes": 0,
    }

    reg_path = os.environ.get("CLANKER_REGISTRY", os.path.expanduser("~/projects/.clanker.yaml"))
    if os.path.exists(reg_path):
        import yaml
        with open(reg_path) as f:
            reg = yaml.safe_load(f) or {}
        snapshot["projects"] = len(reg.get("projects", {}))
        snapshot["archetypes"] = len(reg.get("archetypes", {}))

    # Append to version history
    history_path = os.path.join(DATA_DIR, "audit/harness-versions.jsonl")
    os.makedirs(os.path.dirname(history_path), exist_ok=True)
    with open(history_path, "a") as f:
        f.write(json.dumps(snapshot) + "\n")

    return snapshot


def skill_effectiveness(last_days=30):
    """Analyze which skills fire and their impact."""
    import glob
    from collections import Counter

    # Load skill invocation data
    skills_dir = os.path.join(DATA_DIR, "raw/skills")
    invocations = Counter()
    by_project = defaultdict(Counter)

    if os.path.isdir(skills_dir):
        cutoff = (datetime.utcnow() - timedelta(days=last_days)).strftime("%Y-%m-%d")
        for f in sorted(glob.glob(os.path.join(skills_dir, "*.jsonl"))):
            if os.path.basename(f)[:10] < cutoff:
                continue
            try:
                with open(f) as fh:
                    for line in fh:
                        if line.strip():
                            entry = json.loads(line)
                            skill = entry.get("skill", "unknown")
                            project = entry.get("project", "global")
                            invocations[skill] += 1
                            by_project[project][skill] += 1
            except:
                pass

    if invocations:
        print(f"=== Skill Usage (last {last_days} days) ===\n")
        print(f"{'Skill':<35} {'Invocations':>12}")
        print("-" * 50)
        for skill, count in invocations.most_common():
            print(f"{skill:<35} {count:>12}")

        if by_project:
            print(f"\n=== By Project ===\n")
            for project in sorted(by_project):
                skills = by_project[project]
                print(f"  {project}: {', '.join(f'{s}({c})' for s, c in skills.most_common(5))}")
    else:
        print("No skill invocation data yet.")
        print("Skill tracking hook is installed — data will accumulate as sessions run.")
        print()

    print(f"\n=== Known Skills ===\n")
    for skill_dir in sorted(glob.glob(os.path.expanduser("~/.claude/skills/*/"))):
        name = os.path.basename(skill_dir.rstrip("/"))
        print(f"  {name}")
    for skill_dir in sorted(glob.glob(os.path.expanduser("~/.claude/plugins/cache/claude-plugins-official/superpowers/*/skills/*/"))):
        name = os.path.basename(skill_dir.rstrip("/"))
        print(f"  superpowers:{name}")


def export_harness(output_path=None):
    """Export full harness state to a tarball."""
    import tarfile
    if not output_path:
        output_path = f"/tmp/clanker-export-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.tar.gz"

    with tarfile.open(output_path, "w:gz") as tar:
        # Registry
        reg_path = os.environ.get("CLANKER_REGISTRY", os.path.expanduser("~/projects/.clanker.yaml"))
        if os.path.exists(reg_path):
            tar.add(reg_path, "registry.yaml")

        # Clanker data
        data_dir = os.environ.get("CLANKER_DATA", "/data/clanker")
        for subdir in ["raw", "proposals", "wiki", "alerts", "reports", "audit"]:
            path = os.path.join(data_dir, subdir)
            if os.path.isdir(path):
                tar.add(path, f"data/{subdir}")

        # Hooks and skills
        hooks_dir = os.path.expanduser("~/.claude/hooks")
        if os.path.isdir(hooks_dir):
            tar.add(hooks_dir, "hooks")

        skills_dir = os.path.expanduser("~/.claude/skills")
        if os.path.isdir(skills_dir):
            tar.add(skills_dir, "skills")

        # Memory
        memory_dir = _global_memory_dir()
        if os.path.isdir(memory_dir):
            tar.add(memory_dir, "memory")

    print(f"Exported to: {output_path}")
    print(f"Size: {os.path.getsize(output_path) / 1024 / 1024:.1f} MB")
    return output_path


def import_harness(archive_path):
    """Import harness state from a tarball with merge logic."""
    import tarfile
    import shutil

    if not os.path.exists(archive_path):
        print(f"Archive not found: {archive_path}")
        return

    extract_dir = "/tmp/clanker-import"
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir)
    os.makedirs(extract_dir)

    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(extract_dir)

    print(f"Extracted to: {extract_dir}")
    print()

    data_dir = os.environ.get("CLANKER_DATA", "/data/clanker")

    # List what's in the archive
    imported = []
    for item in os.listdir(extract_dir):
        path = os.path.join(extract_dir, item)
        if os.path.isdir(path):
            size = sum(os.path.getsize(os.path.join(r, f)) for r, _, files in os.walk(path) for f in files)
            imported.append(f"  {item}/: {size // 1024}KB")
        else:
            imported.append(f"  {item}: {os.path.getsize(path) // 1024}KB")

    print("Archive contents:")
    for item in imported:
        print(item)
    print()

    # Merge strategy: append session data, preserve latest proposals, merge alerts
    data_src = os.path.join(extract_dir, "data")
    if os.path.isdir(data_src):
        # Sessions: append (no conflict — different dates)
        sessions_src = os.path.join(data_src, "raw", "sessions")
        sessions_dst = os.path.join(data_dir, "raw", "sessions")
        if os.path.isdir(sessions_src):
            merged = 0
            for f in os.listdir(sessions_src):
                src = os.path.join(sessions_src, f)
                dst = os.path.join(sessions_dst, f)
                if os.path.exists(dst):
                    # Append new entries
                    with open(src) as sf, open(dst, "a") as df:
                        df.write(sf.read())
                    merged += 1
                else:
                    shutil.copy2(src, dst)
                    merged += 1
            print(f"Sessions: merged {merged} files")

        # Proposals: append (dedup by ID handled by last-write-wins)
        proposals_src = os.path.join(data_src, "proposals", "ledger.jsonl")
        proposals_dst = os.path.join(data_dir, "proposals", "ledger.jsonl")
        if os.path.exists(proposals_src):
            with open(proposals_src) as sf, open(proposals_dst, "a") as df:
                df.write(sf.read())
            print("Proposals: merged")

        # Wiki: skip (don't overwrite local wiki with imported)
        print("Wiki: skipped (use 'clanker compile' to regenerate)")

    # Registry: skip (local registry is authoritative)
    print("Registry: skipped (local registry is authoritative)")

    # Cleanup
    shutil.rmtree(extract_dir)
    print(f"\nImport complete. Cleaned up {extract_dir}")
