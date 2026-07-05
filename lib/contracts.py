"""Contract/dependency tracking — detect cross-service contract violations."""

import os
import json
import re
from collections import defaultdict


def scan_contracts(project_path):
    """Scan a project for API contracts (endpoints, events, types).

    Looks for:
    - HTTP endpoints (Express/FastAPI route decorators)
    - WebSocket events
    - Pub/sub channels
    - Shared type definitions
    """
    contracts = {
        "http": [],
        "websocket": [],
        "pubsub": [],
        "types": [],
    }

    if not os.path.isdir(project_path):
        return contracts

    # Track seen contracts to dedup
    seen_http = set()
    seen_ws = set()
    seen_pubsub = set()

    for root, dirs, files in os.walk(project_path):
        # Skip node_modules, .git, build dirs, test dirs
        dirs[:] = [d for d in dirs if d not in (
            'node_modules', '.git', '__pycache__', 'build', 'target', '.next',
            'test', 'tests', '__tests__', 'e2e', 'fixtures', 'mocks',
        )]

        for fname in files:
            if not any(fname.endswith(ext) for ext in ('.py', '.ts', '.js', '.tsx')):
                continue

            filepath = os.path.join(root, fname)
            try:
                with open(filepath) as f:
                    content = f.read()
            except:
                continue

            rel_path = os.path.relpath(filepath, project_path)

            # Skip test/spec files
            if any(p in fname.lower() for p in ['test', 'spec', 'mock', 'fixture']):
                continue

            # HTTP endpoints (FastAPI + Express) — dedup by method+path
            for match in re.finditer(r'(?:@(?:app|router)\.|(?:app|router)\.)(get|post|put|delete|patch)\(["\']([^"\']+)["\']', content):
                key = (match.group(1).upper(), match.group(2))
                if key not in seen_http:
                    seen_http.add(key)
                    contracts["http"].append({
                        "method": key[0],
                        "path": key[1],
                        "file": rel_path,
                    })

            # WebSocket events — dedup by event name
            for match in re.finditer(r'(?:emit|on|socket\.)\(["\']([^"\']+)["\']', content):
                event = match.group(1)
                if not event.startswith('/') and len(event) < 50 and event not in seen_ws:
                    seen_ws.add(event)
                    contracts["websocket"].append({
                        "event": event,
                        "file": rel_path,
                    })

            # Redis pub/sub — dedup by channel
            for match in re.finditer(r'(?:publish|subscribe|channel)\(["\']([^"\']+)["\']', content):
                channel = match.group(1)
                if channel not in seen_pubsub:
                    seen_pubsub.add(channel)
                    contracts["pubsub"].append({
                        "channel": channel,
                        "file": rel_path,
                    })

    return contracts


def check_contract_changes(project, base_ref="HEAD~1"):
    """Check if recent changes affect API contracts."""
    from registry import Registry
    reg = Registry()
    project_path = reg.get_path(project)

    if not os.path.isdir(os.path.join(project_path, ".git")):
        print(f"Not a git repo: {project_path}")
        return

    import subprocess

    # Get changed files
    try:
        diff = subprocess.check_output(
            ["git", "-C", project_path, "diff", "--name-only", base_ref],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except:
        print(f"Cannot diff against {base_ref}")
        return

    if not diff:
        print("No changes detected.")
        return

    changed_files = set(diff.split('\n'))

    # Scan contracts
    contracts = scan_contracts(project_path)

    # Check if any changed files define contracts
    affected = []
    for contract_type, items in contracts.items():
        for item in items:
            if item.get("file") in changed_files:
                affected.append({"type": contract_type, **item})

    if affected:
        print(f"=== Contract Changes in {project} ===\n")
        for a in affected:
            if a["type"] == "http":
                print(f"  HTTP {a['method']} {a['path']} ({a['file']})")
            elif a["type"] == "websocket":
                print(f"  WS event '{a['event']}' ({a['file']})")
            elif a["type"] == "pubsub":
                print(f"  Pub/Sub '{a['channel']}' ({a['file']})")
        print(f"\n{len(affected)} contract(s) may be affected by recent changes.")
        print("Check downstream consumers before deploying.")
    else:
        print(f"No contract changes detected in {project}.")
