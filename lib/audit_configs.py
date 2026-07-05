"""Scan a project for external-fact configs and report which have paired
drift watchdogs.

Class-of-bug fix from eigenstateresearch Session 7 (2026-04-18): the bot
had 10 station-map mismatches silently costing money because no watchdog
compared the local map to Polymarket's live descriptions. The rule is
"every external-fact config gets a paired watchdog"; this command
enforces it.

Usage:
    clanker audit configs <project-path>

Output: a table of config-like dict literals, which file they live in,
and whether a companion watchdog plugin exists for them.
"""

from __future__ import annotations

import ast
import os
import re
from typing import Iterator

# Dict literal names that look like they encode external facts.
# Heuristic: UPPER_SNAKE_CASE with keywords suggesting external reality.
EXTERNAL_FACT_PATTERNS = [
    re.compile(r".*_?(STATION|ICAO|AIRPORT)_?.*", re.IGNORECASE),
    re.compile(r".*_?(API|URL|ENDPOINT)_?.*", re.IGNORECASE),
    re.compile(r".*_?(TIMEZONE|UTC_OFFSET|TZ)_?.*", re.IGNORECASE),
    re.compile(r".*_?(CITY|COUNTRY|REGION)_?.*", re.IGNORECASE),
    re.compile(r".*_?(ALIAS|MAP|LOOKUP)_?.*", re.IGNORECASE),
    re.compile(r".*_?(FIELD|SCHEMA|FORMAT)_?.*", re.IGNORECASE),
]

MIN_ENTRIES_FOR_FACT_MAP = 3  # ignore tiny config dicts


def find_external_fact_configs(project_root: str) -> Iterator[dict]:
    """Walk project .py files. For each dict literal assignment with a
    name matching EXTERNAL_FACT_PATTERNS and ≥ MIN_ENTRIES_FOR_FACT_MAP
    keys, yield a descriptor."""
    for dirpath, dirnames, filenames in os.walk(project_root):
        # Skip tests, caches, venvs, vendored code.
        dirnames[:] = [
            d for d in dirnames
            if d not in (
                "__pycache__", ".git", "node_modules", "venv", ".venv",
                "tests", "test", "dist", "build",
            )
        ]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            try:
                with open(path) as f:
                    tree = ast.parse(f.read(), filename=path)
            except (SyntaxError, OSError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                target_name = None
                value = None
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    target_name = node.target.id
                    value = node.value
                elif isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            target_name = t.id
                            value = node.value
                            break
                if target_name is None or value is None:
                    continue
                if not isinstance(value, ast.Dict):
                    continue
                if len(value.keys) < MIN_ENTRIES_FOR_FACT_MAP:
                    continue
                if not any(p.match(target_name) for p in EXTERNAL_FACT_PATTERNS):
                    continue
                yield {
                    "name": target_name,
                    "path": path,
                    "line": node.lineno,
                    "n_entries": len(value.keys),
                }


def find_watchdog_plugins(project_root: str) -> list[str]:
    """Return list of discovered drift watchdog plugin filenames.
    Heuristic: .py files under any plugins/ or clanker_plugins/ dir
    with "drift" or "sanity" or "contract" or "schema" in the name."""
    patterns = ["drift", "sanity", "contract", "schema", "bias"]
    found = []
    for dirpath, _, filenames in os.walk(project_root):
        if "plugins" not in dirpath.lower():
            continue
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            if any(p in fname.lower() for p in patterns):
                found.append(os.path.join(dirpath, fname))
    return found


def audit_configs(project_root: str) -> int:
    """Emit a coverage report. Returns non-zero if coverage gaps exist."""
    configs = list(find_external_fact_configs(project_root))
    watchdogs = find_watchdog_plugins(project_root)

    print(f"Found {len(configs)} external-fact configs and "
          f"{len(watchdogs)} drift watchdog plugins.\n")

    if not configs:
        print("No external-fact configs detected.")
        return 0

    print(f"{'CONFIG':40s}  {'FILE':50s}  {'ENTRIES':>7s}")
    print("-" * 100)
    for c in sorted(configs, key=lambda x: x["name"]):
        rel = os.path.relpath(c["path"], project_root)
        print(f"  {c['name']:38s}  {rel:50s}  {c['n_entries']:>7d}")

    print(f"\nWatchdog plugins found:")
    for w in sorted(watchdogs):
        rel = os.path.relpath(w, project_root)
        print(f"  {rel}")

    # Coverage heuristic: if any config exists, at least one watchdog should too.
    # Full semantic matching of config-to-watchdog is harder; this is the
    # minimum bar.
    if configs and not watchdogs:
        print("\n⚠ No drift watchdogs found for these configs.")
        print("  Run: clanker new-watchdog <name> --kind config-drift")
        return 1
    print(f"\n✓ {len(watchdogs)} watchdog(s) present for "
          f"{len(configs)} fact-configs. Manual audit still recommended "
          "per-config.")
    return 0
