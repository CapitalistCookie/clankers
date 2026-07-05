"""Parked / stuck-session detector — pure stdlib.

Ported from affaan-m/ECC `scripts/loop-status.js` (extractToolUses /
extractToolResultIds / getSessionId / getContentBlocks, the pending-tool-result
logic, and the ScheduleWakeup-overdue logic). See docs/ECC-feature-mining.md.

Walks Claude Code transcript JSONL files. For each session it reconstructs the
*pending* tool calls — a `tool_use` content block with NO matching `tool_result`
— plus the latest `ScheduleWakeup`. A session is flagged as needing attention
when a tool_use has been pending longer than a threshold (default 1800s, i.e. the
session is parked waiting on a tool result) or a scheduled wakeup is overdue.

Only the small pure algorithm is ported here; the JS watch loop, snapshot
writers, and CLI argument plumbing are out of scope (clanker owns those paths).

Public API:
    scan_transcript(path, now=None, pending_secs=1800) -> dict | None
    scan_dir(projects_dir=~/.claude/projects, now=None, pending_secs=1800) -> list[dict]
    extract_tool_uses(objs)        -> list[dict]   (helper, reused by clanker)
    extract_tool_result_ids(objs)  -> list[str]    (helper, reused by clanker)
"""

import json
import os
import time

# ScheduleWakeup grace multiplier: a wake is only "overdue" once now is past
# scheduledAt + delay * GRACE (mirrors the JS DEFAULT_WAKE_GRACE_MULTIPLIER).
WAKE_GRACE_MULTIPLIER = 2

DEFAULT_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

__all__ = [
    "scan_transcript",
    "scan_dir",
    "extract_tool_uses",
    "extract_tool_result_ids",
]


# --------------------------------------------------------------------------- #
# Timestamp parsing
# --------------------------------------------------------------------------- #

def _parse_ts(value):
    """Parse an ISO-8601 / epoch timestamp to epoch seconds (float). None on miss.

    Accepts ISO strings (with optional trailing 'Z'), epoch milliseconds, or
    epoch seconds. Returns None for anything unparseable — callers tolerate it.
    """
    if value is None:
        return None
    # Numeric: treat large values as ms, smaller as seconds.
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Pure integer string -> epoch ms (matches loop-status.js /^\d+$/ handling).
    if s.isdigit():
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    try:
        from datetime import datetime, timezone
        iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _entry_timestamp(entry):
    """Port of getEntryTimestamp — first parseable of several known fields."""
    if not isinstance(entry, dict):
        return None
    for getter in (
        entry.get("timestamp"),
        entry.get("createdAt"),
        entry.get("created_at"),
        (entry.get("message") or {}).get("timestamp") if isinstance(entry.get("message"), dict) else None,
    ):
        ts = _parse_ts(getter)
        if ts is not None:
            return ts
    return None


# --------------------------------------------------------------------------- #
# Content-block / tool-use / tool-result extraction (port of JS helpers)
# --------------------------------------------------------------------------- #

def _content_blocks(entry):
    """Port of getContentBlocks — message.content list + top-level content list."""
    blocks = []
    if isinstance(entry, dict):
        msg = entry.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            blocks.extend(msg["content"])
        if isinstance(entry.get("content"), list):
            blocks.extend(entry["content"])
    return blocks


def _entry_tool_uses(entry):
    """Port of extractToolUses for a single entry. Returns list of dicts:
    {id, name, input}. Covers content-block tool_use, top-level tool_use/toolUse,
    and a direct {type:'tool_use'} entry shape.
    """
    uses = []
    for block in _content_blocks(entry):
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
            uses.append({
                "id": block["id"],
                "name": block.get("name") or "unknown",
                "input": block.get("input") or {},
            })
    if isinstance(entry, dict):
        top = entry.get("tool_use") or entry.get("toolUse")
        if isinstance(top, dict) and top.get("id"):
            uses.append({
                "id": top["id"],
                "name": top.get("name") or "unknown",
                "input": top.get("input") or {},
            })
        if entry.get("type") == "tool_use" and entry.get("id"):
            uses.append({
                "id": entry["id"],
                "name": entry.get("name") or "unknown",
                "input": entry.get("input") or {},
            })
    return uses


def _entry_tool_result_ids(entry):
    """Port of extractToolResultIds for a single entry. Returns list of tool_use ids."""
    ids = []
    for block in _content_blocks(entry):
        if isinstance(block, dict) and block.get("type") == "tool_result":
            tid = block.get("tool_use_id") or block.get("toolUseId") or block.get("id")
            if tid:
                ids.append(tid)
    if isinstance(entry, dict):
        top = entry.get("tool_result") or entry.get("toolResult") or entry.get("toolUseResult")
        if isinstance(top, dict):
            tid = top.get("tool_use_id") or top.get("toolUseId") or top.get("id")
            if tid:
                ids.append(tid)
        if entry.get("type") == "tool_result":
            tid = entry.get("tool_use_id") or entry.get("toolUseId") or entry.get("id")
            if tid:
                ids.append(tid)
    return ids


def extract_tool_uses(objs):
    """Aggregate tool_use blocks across a list of parsed transcript objects.

    Returns a list of dicts {id, name, input, started_at} where started_at is the
    epoch-seconds timestamp of the entry the tool_use appeared in (the per-block
    pending-since reference), or None if that entry had no timestamp. Order is
    first-seen; duplicate ids keep the earliest occurrence.
    """
    seen = {}
    order = []
    for entry in objs:
        ts = _entry_timestamp(entry)
        for use in _entry_tool_uses(entry):
            uid = use["id"]
            if uid in seen:
                continue
            rec = {"id": uid, "name": use["name"], "input": use["input"], "started_at": ts}
            seen[uid] = rec
            order.append(rec)
    return order


def extract_tool_result_ids(objs):
    """Aggregate the tool_use ids that have a matching tool_result, across objs."""
    ids = []
    for entry in objs:
        ids.extend(_entry_tool_result_ids(entry))
    return ids


# --------------------------------------------------------------------------- #
# Session id
# --------------------------------------------------------------------------- #

def _session_id(entry, transcript_path):
    """Port of getSessionId — several known id fields, else the file basename."""
    if isinstance(entry, dict):
        sid = entry.get("sessionId") or entry.get("session_id")
        if not sid and isinstance(entry.get("session"), dict):
            sid = entry["session"].get("id")
        if not sid and isinstance(entry.get("message"), dict):
            sid = entry["message"].get("sessionId")
        if sid:
            return sid
    base = os.path.basename(transcript_path)
    return base[:-6] if base.endswith(".jsonl") else base


def _read_jsonl(path):
    """Read JSONL, tolerating malformed lines. Returns (objects, parse_errors)."""
    objs = []
    parse_errors = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                objs.append(json.loads(line))
            except Exception:
                parse_errors += 1
    return objs, parse_errors


def _wake_delay_seconds(inp):
    """Port of readDelaySeconds — positive delay from a ScheduleWakeup input."""
    if not isinstance(inp, dict):
        return None
    raw = (
        inp.get("delaySeconds")
        or inp.get("delay_seconds")
        or inp.get("seconds")
        or inp.get("delay")
    )
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


# --------------------------------------------------------------------------- #
# Core scan
# --------------------------------------------------------------------------- #

def scan_transcript(path, now=None, pending_secs=1800):
    """Classify one transcript. Returns a record dict or None if unparseable/empty.

    Record:
        {
          "session": <session id>,
          "state": "attention" | "ok",
          "reasons": [str, ...],
          "pending_tool": {"name": str, "age_secs": int} | None,
          "recommended_action": str,
        }

    A session is "attention" when a pending tool_use (no matching tool_result)
    has been waiting >= pending_secs, or its latest ScheduleWakeup is overdue.
    """
    now = time.time() if now is None else float(now)
    try:
        objs, _parse_errors = _read_jsonl(path)
    except OSError:
        return None
    if not objs:
        return None

    session = None
    last_assistant_ts = None
    latest_wake = None

    for entry in objs:
        session = _session_id(entry, path) or session
        ts = _entry_timestamp(entry)

        # Track the latest assistant-progress timestamp (port of
        # isAssistantProgressEntry): role=assistant, type=assistant, or any
        # entry emitting a tool_use. This is the pending-since fallback and the
        # "did the model make progress after the wake was due" reference.
        is_assistant = False
        if isinstance(entry, dict):
            is_assistant = (
                entry.get("type") == "assistant"
                or (isinstance(entry.get("message"), dict) and entry["message"].get("role") == "assistant")
                or bool(_entry_tool_uses(entry))
            )
        if ts is not None and is_assistant and (last_assistant_ts is None or ts > last_assistant_ts):
            last_assistant_ts = ts

        # Latest ScheduleWakeup (by occurrence order).
        for use in _entry_tool_uses(entry):
            if use["name"] == "ScheduleWakeup":
                delay = _wake_delay_seconds(use["input"])
                started = ts if ts is not None else last_assistant_ts
                if delay is not None and started is not None:
                    latest_wake = {
                        "scheduled_at": started,
                        "delay_seconds": delay,
                        "due_at": started + delay,
                    }

    if session is None:
        session = os.path.basename(path)
        session = session[:-6] if session.endswith(".jsonl") else session

    # Pending tool_use ids = used minus answered.
    uses = extract_tool_uses(objs)
    answered = set(extract_tool_result_ids(objs))

    pending = []  # list of (name, age_secs)
    for use in uses:
        if use["id"] in answered:
            continue
        # Pending-since: per-block timestamp, else last assistant timestamp.
        started = use["started_at"] if use["started_at"] is not None else last_assistant_ts
        age = int(max(0, now - started)) if started is not None else None
        pending.append({"name": use["name"], "age_secs": age, "started": started})

    reasons = []

    # Oldest pending tool (by age; None ages sort last) drives pending_tool.
    pending_with_age = [p for p in pending if p["age_secs"] is not None]
    pending_tool = None
    if pending_with_age:
        oldest = max(pending_with_age, key=lambda p: p["age_secs"])
        pending_tool = {"name": oldest["name"], "age_secs": oldest["age_secs"]}
        if oldest["age_secs"] >= pending_secs:
            reasons.append(
                "pending_tool_result: %s waiting %ds (>= %ds)"
                % (oldest["name"], oldest["age_secs"], pending_secs)
            )

    # ScheduleWakeup overdue (port of the JS logic): now past
    # scheduledAt + delay*GRACE, and no assistant progress at/after due_at.
    if latest_wake is not None:
        threshold = latest_wake["scheduled_at"] + latest_wake["delay_seconds"] * WAKE_GRACE_MULTIPLIER
        progressed_after_due = (
            last_assistant_ts is not None and last_assistant_ts >= latest_wake["due_at"]
        )
        if now >= threshold and not progressed_after_due:
            overdue = int(max(0, now - latest_wake["due_at"]))
            reasons.append("schedule_wakeup_overdue: due %ds ago" % overdue)

    state = "attention" if reasons else "ok"
    if state == "attention":
        if any(r.startswith("pending_tool_result") for r in reasons):
            action = "Open the transcript or interrupt the parked session; a tool result appears stale."
        else:
            action = "Open the transcript or interrupt the parked session; the scheduled wake is overdue."
    else:
        action = "No stale pending tool or overdue ScheduleWakeup detected."

    return {
        "session": session,
        "state": state,
        "reasons": reasons,
        "pending_tool": pending_tool,
        "recommended_action": action,
    }


def scan_dir(projects_dir=DEFAULT_PROJECTS_DIR, now=None, pending_secs=1800):
    """Walk projects_dir/**/*.jsonl, returning one record per readable transcript.

    Records are scan_transcript() dicts augmented with "path" and "mtime", sorted
    newest-mtime first. Transcripts that are empty/unparseable (scan_transcript
    returns None) are skipped. Filter the result by record["state"] as needed.
    """
    projects_dir = os.path.expanduser(projects_dir)
    paths = []
    for root, _dirs, files in os.walk(projects_dir):
        for name in files:
            if name.endswith(".jsonl"):
                full = os.path.join(root, name)
                try:
                    mtime = os.stat(full).st_mtime
                except OSError:
                    continue
                paths.append((mtime, full))

    paths.sort(key=lambda t: t[0], reverse=True)  # newest first

    records = []
    for mtime, full in paths:
        try:
            rec = scan_transcript(full, now=now, pending_secs=pending_secs)
        except Exception:
            rec = None
        if rec is None:
            continue
        rec = dict(rec)
        rec["path"] = full
        rec["mtime"] = mtime
        records.append(rec)
    return records


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROJECTS_DIR
    if os.path.isdir(target):
        recs = scan_dir(target)
        attention = [r for r in recs if r["state"] == "attention"]
        print("Scanned %d transcript(s); %d need attention." % (len(recs), len(attention)))
        for r in attention:
            print("  [%s] %s" % (r["state"], r["session"]))
            for reason in r["reasons"]:
                print("    - %s" % reason)
            print("    action: %s" % r["recommended_action"])
    else:
        rec = scan_transcript(target)
        print(json.dumps(rec, indent=2))
