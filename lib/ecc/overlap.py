"""File-overlap-across-active-sessions detector — ported from ECC's
`session::store::StateStore::list_file_overlaps`.

Pure stdlib. Surfaces source files that are being touched by more than one
*in-flight* session at once, so a collision can be flagged before two sessions
clobber each other's edits.

Provenance: ecc2/src/session/store.rs (affaan-m/ECC). The Rust version queries a
SQLite `file_activity` table for the current session, then walks every *other*
session whose state still `supports_overlap` (Pending | Running | Idle | Stale —
i.e. not Completed/Failed/Stopped) and collects shared paths. Clanker has no
per-session SQLite activity table; its analog is the flat session record stream
(see `analyze.load_sessions`), where each record carries a `files_touched` list
and an `outcome`. So this port reframes the same idea over those records: group
sessions by the files they touched and report every file shared by >=2 sessions.

Active heuristic
----------------
A session is "active" (still in-flight) when its outcome has NOT reached a
terminal state. Clanker's terminal outcomes (see `lib/autonomy.py`) are:
  - CLEAN_OUTCOMES = {"commit", "push", "deploy"}  (reached a durable result)
  - STUCK_OUTCOMES = {"abandoned", "empty"}        (ended without progress)
Everything else — notably "unknown" and a missing/None outcome — is treated as
still running. This mirrors `session_state_supports_overlap`: Completed/Failed/
Stopped are excluded, Pending/Running/Idle/Stale are kept. Callers who track
state differently can pass their own `is_active(session) -> bool` predicate.

Public API:
    file_overlaps(sessions, active_only=True, ignore=None, is_active=None) -> list[dict]
    overlap_summary(sessions, **kw) -> dict
    is_active_default(session) -> bool
"""

import fnmatch

# Terminal outcomes — a session in any of these has stopped (durable result or
# dead end) and is NOT considered active. Kept in sync with lib/autonomy.py's
# CLEAN_OUTCOMES | STUCK_OUTCOMES. Compared case-insensitively.
TERMINAL_OUTCOMES = frozenset({"commit", "push", "deploy", "abandoned", "empty"})

# Default noise filter — paths matching any of these glob patterns are dropped
# before overlap is computed. These are files that routinely get touched by many
# unrelated sessions (locks, logs, caches, vendored deps, VCS internals) and so
# generate uninteresting "overlaps". Matched against both the full path and its
# basename, case-insensitively, with fnmatch glob semantics.
DEFAULT_IGNORE = (
    "*.lock",
    "*.log",
    "*/node_modules/*",
    "node_modules/*",
    "*/__pycache__/*",
    "__pycache__/*",
    "*.pyc",
    "*/.git/*",
    ".git/*",
)


def is_active_default(session):
    """True if `session` looks still-in-flight under the documented heuristic.

    Active == outcome has not reached a terminal state. A missing or None outcome
    (i.e. no outcome recorded yet) counts as active, matching the Rust treatment
    of Pending/Running sessions. Comparison is case-insensitive and tolerant of
    surrounding whitespace.
    """
    outcome = session.get("outcome")
    if outcome is None:
        return True
    return str(outcome).strip().lower() not in TERMINAL_OUTCOMES


def _normalize_ignore(ignore):
    """Resolve the `ignore` argument to a tuple of glob patterns.

    None -> the DEFAULT_IGNORE set; a string -> a single-pattern tuple; any other
    iterable -> a tuple of its items. An explicit empty iterable disables filtering.
    """
    if ignore is None:
        return DEFAULT_IGNORE
    if isinstance(ignore, str):
        return (ignore,)
    return tuple(ignore)


def _is_ignored(path, patterns):
    """True if `path` (or its basename) matches any ignore glob, case-insensitively."""
    if not patterns:
        return False
    lowered = path.lower()
    base = lowered.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(lowered, pat) or fnmatch.fnmatch(base, pat)
        for pat in (p.lower() for p in patterns)
    )


def _session_id(session, index):
    """Best-effort stable identifier for a session record."""
    return session.get("session_id") or session.get("id") or f"<session#{index}>"


def file_overlaps(sessions, active_only=True, ignore=None, is_active=None):
    """Files touched by >=2 sessions.

    Args:
        sessions: clanker session records (dicts). Relevant keys:
                  "session_id" (or "id"), "project", "files_touched" (list[str]),
                  "outcome". Missing keys degrade gracefully.
        active_only: when True (default) only sessions for which `is_active`
                     returns True are considered — i.e. report live collisions,
                     not files a finished session happened to share. When False,
                     every session counts regardless of outcome.
        ignore: glob pattern or iterable of patterns to drop from consideration
                (matched on full path and basename). None -> DEFAULT_IGNORE; an
                empty iterable -> no filtering.
        is_active: optional predicate `(session) -> bool` overriding the default
                   in-flight heuristic (`is_active_default`).

    Returns:
        A list of dicts, one per file shared by >=2 (qualifying) sessions:
            {
              "file":     str,            # the shared path
              "sessions": [session_id],   # ids that touched it, first-seen order
              "projects": [project],      # distinct projects, first-seen order
              "count":    int,            # == len(sessions)
            }
        Sorted by "count" descending, then by "file" ascending for a stable order.
    """
    active_pred = is_active or is_active_default
    patterns = _normalize_ignore(ignore)

    # Preserve first-seen order of sessions/projects per file (dict-as-ordered-set).
    file_sessions = {}   # path -> {session_id: None}
    file_projects = {}   # path -> {project: None}

    for index, session in enumerate(sessions or []):
        if active_only and not active_pred(session):
            continue

        sid = _session_id(session, index)
        project = session.get("project")

        seen_here = set()  # de-dup repeated paths within one session record
        for path in session.get("files_touched") or []:
            if not path or path in seen_here:
                continue
            seen_here.add(path)
            if _is_ignored(path, patterns):
                continue

            file_sessions.setdefault(path, {})[sid] = None
            if project is not None:
                file_projects.setdefault(path, {})[project] = None

    overlaps = []
    for path, sids in file_sessions.items():
        if len(sids) < 2:
            continue
        overlaps.append({
            "file": path,
            "sessions": list(sids.keys()),
            "projects": list(file_projects.get(path, {}).keys()),
            "count": len(sids),
        })

    overlaps.sort(key=lambda o: (-o["count"], o["file"]))
    return overlaps


def overlap_summary(sessions, **kw):
    """Roll up `file_overlaps` into a compact dict.

    Accepts the same keyword args as `file_overlaps` (active_only, ignore,
    is_active) and forwards them.

    Returns:
        {
          "n_overlapping_files":  int,   # files shared by >=2 sessions
          "n_sessions_involved":  int,   # distinct session ids across those files
          "top":                  list,  # the first few overlap dicts (highest count)
        }
    """
    top_n = kw.pop("top_n", 5)
    overlaps = file_overlaps(sessions, **kw)

    involved = set()
    for o in overlaps:
        involved.update(o["sessions"])

    return {
        "n_overlapping_files": len(overlaps),
        "n_sessions_involved": len(involved),
        "top": overlaps[:top_n],
    }
