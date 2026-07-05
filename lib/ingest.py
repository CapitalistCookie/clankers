"""Universal event ingestion — accepts events from any source, writes to data store."""

import json
import os
import fcntl
from datetime import datetime

from events import validate_event, create_event, generate_session_id

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")
SESSIONS_DIR = os.path.join(DATA_DIR, "raw/sessions")


def _write_event(event):
    """Write an event to the daily JSONL file with flock."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    date = event.get("timestamp", "")[:10] or datetime.utcnow().strftime("%Y-%m-%d")
    outfile = os.path.join(SESSIONS_DIR, f"{date}.jsonl")
    lockfile = outfile + ".lock"

    line = json.dumps(event) + "\n"
    with open(lockfile, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        with open(outfile, "a") as f:
            f.write(line)
        fcntl.flock(lf, fcntl.LOCK_UN)

    return event.get("session_id", "")


def ingest_event(event_data):
    """Validate and store an event. Returns (success, session_id, errors)."""
    valid, errors, normalized = validate_event(event_data)
    if not valid:
        return False, "", errors

    session_id = _write_event(normalized)
    return True, session_id, []


def ingest_from_args(event, agent, project, cwd=None, session_id=None,
                     duration=None, outcome=None, summary=None,
                     files_changed=None, exit_code=None, git_diff_stats=None,
                     agent_version=None):
    """Create and ingest an event from CLI arguments."""
    kwargs = {}
    if cwd:
        kwargs["cwd"] = cwd
    if session_id:
        kwargs["session_id"] = session_id
    if duration is not None:
        kwargs["duration_s"] = int(duration)
    if outcome:
        kwargs["outcome"] = outcome
    if summary:
        kwargs["summary"] = summary
    if files_changed:
        if isinstance(files_changed, str):
            kwargs["files_changed"] = [f.strip() for f in files_changed.split(",") if f.strip()]
        else:
            kwargs["files_changed"] = files_changed
    if exit_code is not None:
        kwargs["exit_code"] = int(exit_code)
    if git_diff_stats:
        if isinstance(git_diff_stats, str):
            kwargs["git_diff_stats"] = json.loads(git_diff_stats)
        else:
            kwargs["git_diff_stats"] = git_diff_stats
    if agent_version:
        kwargs["agent_version"] = agent_version

    event_data = create_event(event, agent, project, **kwargs)
    success, sid, errors = ingest_event(event_data)
    return success, sid, errors


def ingest_from_stdin():
    """Read JSON event from stdin and ingest."""
    import sys
    try:
        raw = sys.stdin.read().strip()
        if not raw:
            return False, "", ["Empty stdin"]
        event_data = json.loads(raw)
        return ingest_event(event_data)
    except json.JSONDecodeError as e:
        return False, "", [f"Invalid JSON: {e}"]


def ingest_batch(jsonl_input):
    """Ingest multiple events from JSONL input. Returns (success_count, error_count)."""
    success = 0
    errors = 0
    for line in jsonl_input.strip().split("\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            ok, _, errs = ingest_event(event)
            if ok:
                success += 1
            else:
                errors += 1
        except:
            errors += 1
    return success, errors
