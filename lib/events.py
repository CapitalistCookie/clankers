"""Universal event schema — defines and validates clanker events."""

import hashlib
import json
from datetime import datetime

# Required fields for all events
REQUIRED_FIELDS = {"event", "timestamp", "agent", "project"}

# Valid event types
EVENT_TYPES = {"session_start", "session_end", "heartbeat"}

# All known fields (required + optional)
KNOWN_FIELDS = {
    # Required
    "event", "timestamp", "agent", "project", "cwd",
    # Identity
    "session_id",
    # Metrics
    "duration_s", "outcome", "agent_version",
    # Rich metrics (native hooks)
    "tool_uses", "errors", "error_tools", "files_touched",
    "files_touched_count", "user_corrections", "subagent_count", "flags",
    # LLM-reported (skill-based)
    "summary", "files_changed",
    # Wrapper metrics
    "exit_code", "git_diff_stats",
    # Internal
    "status", "source",
}


def generate_session_id(timestamp, project, agent):
    """Generate a deterministic session ID from timestamp + project + agent."""
    raw = f"{timestamp}:{project}:{agent}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def validate_event(event_data):
    """Validate an event dict. Returns (is_valid, errors, normalized_event)."""
    errors = []

    # Check required fields
    for field in REQUIRED_FIELDS:
        if field not in event_data or not event_data[field]:
            errors.append(f"Missing required field: {field}")

    # Check event type
    event_type = event_data.get("event", "")
    if event_type and event_type not in EVENT_TYPES:
        errors.append(f"Unknown event type: {event_type}")

    if errors:
        return False, errors, event_data

    # Normalize
    normalized = dict(event_data)

    # Generate session_id if not provided
    if "session_id" not in normalized or not normalized["session_id"]:
        normalized["session_id"] = generate_session_id(
            normalized["timestamp"],
            normalized["project"],
            normalized["agent"]
        )

    # Default optional fields
    normalized.setdefault("cwd", "")
    normalized.setdefault("duration_s", 0)
    normalized.setdefault("outcome", "unknown")
    normalized.setdefault("agent_version", "unknown")
    normalized.setdefault("tool_uses", {})
    normalized.setdefault("errors", 0)
    normalized.setdefault("error_tools", {})
    normalized.setdefault("files_touched", [])
    normalized.setdefault("files_touched_count", 0)
    normalized.setdefault("user_corrections", 0)
    normalized.setdefault("subagent_count", 0)
    normalized.setdefault("flags", [])
    normalized.setdefault("summary", "")
    normalized.setdefault("files_changed", [])
    normalized.setdefault("exit_code", None)
    normalized.setdefault("git_diff_stats", {})
    normalized.setdefault("source", "ingest")

    return True, [], normalized


def create_event(event_type, agent, project, **kwargs):
    """Create a new event with defaults."""
    event = {
        "event": event_type,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent": agent,
        "project": project,
        **kwargs,
    }
    valid, errors, normalized = validate_event(event)
    if not valid:
        raise ValueError(f"Invalid event: {errors}")
    return normalized
