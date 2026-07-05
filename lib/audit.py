"""Audit trail — immutable changelog of all harness modifications."""

import json
import os
from datetime import datetime

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")
AUDIT_PATH = os.path.join(DATA_DIR, "audit/changelog.jsonl")


def log_change(action, target, details=None, source_proposal=None):
    """Append an entry to the audit trail."""
    os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
    entry = {
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": action,  # "hook-added", "memory-updated", "proposal-accepted", etc.
        "target": target,  # what was changed
        "details": details or {},
        "source_proposal": source_proposal,  # link to proposal ID if applicable
    }
    with open(AUDIT_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_audit(last_n=50):
    """Read last N audit entries."""
    entries = []
    if os.path.exists(AUDIT_PATH):
        with open(AUDIT_PATH) as f:
            for line in f:
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except:
                        pass
    return entries[-last_n:]


def print_audit(last_n=20):
    """Print audit trail."""
    entries = read_audit(last_n)
    if not entries:
        print("No audit entries.")
        return
    print(f"{'Timestamp':<22} {'Action':<20} {'Target'}")
    print("-" * 65)
    for e in entries:
        print(f"{e['timestamp']:<22} {e['action']:<20} {e.get('target', '?')}")
