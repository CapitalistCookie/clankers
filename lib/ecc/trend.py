"""Success-rate trend + failure-clustering analytics (ported from affaan-m/ECC).

Pure-stdlib port of three ECC modules into one regression-detector primitive:
  - scripts/lib/skill-evolution/tracker.js  — the run-record schema + outcomes
  - scripts/lib/skill-evolution/health.js   — 7d-vs-30d success rate, failure_trend
                                              (worsening/improving/stable), declining flag
  - scripts/lib/inspection.js               — normalizeFailureReason + failure clustering

ECC keys its run records by ``skill_id``; clanker generalises that to an opaque
``key`` (a skill name, a project, a strategy id — whatever the caller groups by).
Everything here is deterministic given the records and an explicit ``now`` — no
``datetime.now()`` is consulted unless ``now`` is omitted — so it is hermetically
testable. Nothing in clanker's core paths imports this unless ``ecc.enabled()``;
see ``lib/ecc/__init__.py``.

Run-record dict shape (``RUN_FIELDS``)::

    {
      "key":           str,            # grouping id (skill / project / strategy)
      "outcome":       "success" | "failure" | "partial",
      "user_feedback": "accepted" | "corrected" | "rejected" | None,
      "failure_reason": str | None,    # free-text; normalised before clustering
      "tokens":        int,            # tokens consumed (0 if unknown)
      "duration_ms":   int,            # wall-clock ms (0 if unknown)
      "recorded_at":   str,            # ISO-8601 timestamp, e.g. "2026-06-09T12:00:00Z"
    }

Records with an unparseable ``recorded_at`` are ignored for the time-windowed
math (best-effort, matching ECC's JSONL reader) but still validated by shape.
"""

from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Schema / vocab (mirrors tracker.js VALID_OUTCOMES / VALID_FEEDBACK).
# ---------------------------------------------------------------------------

RUN_FIELDS = (
    "key",
    "outcome",
    "user_feedback",
    "failure_reason",
    "tokens",
    "duration_ms",
    "recorded_at",
)

VALID_OUTCOMES = frozenset({"success", "failure", "partial"})
VALID_FEEDBACK = frozenset({"accepted", "corrected", "rejected"})

# inspection.js treats these as failures when clustering (it is laxer than the
# tracker's outcome vocab, which only knows "failure").
_FAILURE_OUTCOMES = frozenset({"failure", "failed", "error"})

_DAY_MS = 24 * 60 * 60 * 1000

# rate_7d must drop below this floor (in addition to a worsening trend) for a
# key to be flagged ``declining`` — a worsening trend that is still, say, 95%
# is not yet a regression worth paging on.
_DECLINING_FLOOR = 0.5


# ---------------------------------------------------------------------------
# Time helpers — parse ISO-8601 to epoch-ms (the unit ECC's Date.parse uses).
# ---------------------------------------------------------------------------

def _parse_ms(value):
    """ISO-8601 string -> epoch milliseconds, or ``None`` if unparseable.

    Accepts a trailing ``Z`` (UTC) which ``datetime.fromisoformat`` only learned
    natively in 3.11; we normalise it for portability. Naive timestamps are
    assumed UTC, matching how ECC's records are written.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _now_ms(now):
    """Resolve the reference instant to epoch-ms; default to wall-clock UTC."""
    if now is None:
        return int(datetime.now(timezone.utc).timestamp() * 1000)
    ms = _parse_ms(now)
    if ms is None:
        raise ValueError(f"invalid now timestamp: {now!r}")
    return ms


def _round_rate(value):
    """Round a rate to 4 dp (mirrors health.js roundRate), passing through None."""
    if value is None:
        return None
    return round(value * 10000) / 10000


# ---------------------------------------------------------------------------
# Core success-rate math (ports health.js calculate/filter/trend functions).
# ---------------------------------------------------------------------------

def _within_days(records, now_ms, days):
    """Records whose recorded_at falls in ``(now - days, now]``."""
    cutoff = now_ms - days * _DAY_MS
    out = []
    for rec in records:
        ms = _parse_ms(rec.get("recorded_at"))
        if ms is not None and cutoff <= ms <= now_ms:
            out.append(rec)
    return out


def _success_rate(records):
    """Fraction of records whose outcome == 'success', or None if empty."""
    if not records:
        return None
    successes = sum(1 for r in records if r.get("outcome") == "success")
    return _round_rate(successes / len(records))


def _corrections_rate(records):
    """Fraction of records whose user_feedback == 'corrected', or None if empty."""
    if not records:
        return None
    corrected = sum(1 for r in records if r.get("user_feedback") == "corrected")
    return _round_rate(corrected / len(records))


def _failure_trend(rate_7d, rate_30d, warn_delta):
    """Classify the 7d-vs-30d delta (ports health.js getFailureTrend).

    Returns 'worsening' if the recent rate fell by >= warn_delta, 'improving' if
    it rose by >= warn_delta, else 'stable'. Undefined windows are 'stable'.
    """
    if rate_7d is None or rate_30d is None:
        return "stable"
    delta = _round_rate(rate_7d - rate_30d)
    if delta <= -warn_delta:
        return "worsening"
    if delta >= warn_delta:
        return "improving"
    return "stable"


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def success_rate_trend(records, key=None, now=None, warn_delta=0.15):
    """7d-vs-30d success-rate trend for one key (or all records if key is None).

    Args:
        records: list of run-record dicts (see ``RUN_FIELDS``).
        key: if given, only records whose ``key`` matches are considered.
        now: ISO-8601 reference instant; defaults to wall-clock UTC.
        warn_delta: magnitude of the 7d-30d rate delta that counts as a trend.

    Returns a dict::

        {
          "key": key,                 # echoed back (may be None for "all")
          "rate_7d": float | None,    # success rate over the last 7 days
          "rate_30d": float | None,   # success rate over the last 30 days
          "runs_7d": int,             # record count in the 7d window
          "runs_30d": int,            # record count in the 30d window
          "trend": "worsening" | "improving" | "stable",
          "declining": bool,          # worsening AND rate_7d below the floor
          "corrections_rate": float | None,  # fraction corrected (30d window)
        }
    """
    now_ms = _now_ms(now)
    if key is not None:
        records = [r for r in records if r.get("key") == key]

    recs_7d = _within_days(records, now_ms, 7)
    recs_30d = _within_days(records, now_ms, 30)

    rate_7d = _success_rate(recs_7d)
    rate_30d = _success_rate(recs_30d)
    trend = _failure_trend(rate_7d, rate_30d, warn_delta)
    declining = trend == "worsening" and rate_7d is not None and rate_7d < _DECLINING_FLOOR

    return {
        "key": key,
        "rate_7d": rate_7d,
        "rate_30d": rate_30d,
        "runs_7d": len(recs_7d),
        "runs_30d": len(recs_30d),
        "trend": trend,
        "declining": declining,
        # corrections measured over the broader 30d window for stability.
        "corrections_rate": _corrections_rate(recs_30d),
    }


def trend_by_key(records, now=None, warn_delta=0.15):
    """One ``success_rate_trend`` per distinct key, sorted worst-first.

    Worst-first ordering: declining keys first, then by ascending 7d rate (a
    ``None`` rate sorts as if 1.0 so keys with no recent runs sink to the
    bottom), then alphabetically by key for determinism.
    """
    keys = sorted({r.get("key") for r in records if r.get("key") is not None})
    trends = [success_rate_trend(records, key=k, now=now, warn_delta=warn_delta)
              for k in keys]

    def sort_key(t):
        rate = t["rate_7d"] if t["rate_7d"] is not None else 1.0
        return (not t["declining"], rate, t["key"] or "")

    return sorted(trends, key=sort_key)


def normalize_failure_reason(reason):
    """Normalise a failure reason so semantically-equal failures cluster.

    Ports inspection.js ``normalizeFailureReason`` (lowercase; strip ISO
    timestamps, UUIDs, file paths; collapse whitespace) and additionally strips
    bare hex blobs and numbers — so e.g. "timeout after 3211ms at /tmp/x/y.py:44"
    and "timeout after 9s at /home/a/b.py:9" both reduce to the same signature.
    Order matters: timestamps/UUIDs/paths first (they contain digits), numbers last.
    """
    if not reason or not isinstance(reason, str):
        return "unknown"

    import re

    text = reason.strip().lower()
    # ISO timestamps (already lowercased, so t/z not T/Z) -> <timestamp>
    text = re.sub(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}[.\dz]*", "<timestamp>", text)
    # UUIDs (already lowercased) -> <uuid>
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", text)
    # Absolute file paths (with optional :line[:col] suffix) -> <path>
    text = re.sub(r"/[\w./-]+(?::\d+){0,2}", "<path>", text)
    # Bare hex blobs (0x… or a long hex run) -> <hex>
    text = re.sub(r"\b0x[0-9a-f]+\b", "<hex>", text)
    text = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", text)
    # Any remaining number -> <n>, absorbing a trailing unit so that
    # "3211ms" and "9s" both collapse to the same token. The unit alternatives
    # are longest-first so "ms" wins over "s".
    text = re.sub(r"\b\d+(?:\.\d+)?(?:ms|ns|us|µs|s|m|h|d|kb|mb|gb|tb|b|%)?\b",
                  "<n>", text)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text or "unknown"


def cluster_failures(records, min_count=2):
    """Cluster failure records by normalised reason; return recurring signatures.

    Ports inspection.js ``groupFailures`` + ``detectPatterns``, but groups across
    all keys by the normalised signature alone (clanker's run records share one
    failure namespace). Only signatures occurring ``>= min_count`` times are
    returned, sorted by count descending then most-recent ``last_seen`` first.

    Each entry::

        {
          "signature": str,        # normalised reason all members share
          "count": int,            # number of failure records in the cluster
          "first_seen": str|None,  # earliest recorded_at in the cluster
          "last_seen": str|None,   # latest recorded_at in the cluster
          "sample": str|None,      # one raw failure_reason (a representative)
          "keys": [str, ...],      # distinct keys that hit this signature, sorted
        }
    """
    groups = {}
    for rec in records:
        outcome = str(rec.get("outcome") or "").lower()
        if outcome not in _FAILURE_OUTCOMES:
            continue
        sig = normalize_failure_reason(rec.get("failure_reason"))
        groups.setdefault(sig, []).append(rec)

    clusters = []
    for sig, members in groups.items():
        if len(members) < min_count:
            continue
        # Sort members oldest-first by recorded_at (unparseable sink to the end).
        ordered = sorted(
            members,
            key=lambda r: (_parse_ms(r.get("recorded_at")) is None,
                           _parse_ms(r.get("recorded_at")) or 0),
        )
        timestamps = [r.get("recorded_at") for r in ordered
                      if _parse_ms(r.get("recorded_at")) is not None]
        first_seen = timestamps[0] if timestamps else None
        last_seen = timestamps[-1] if timestamps else None
        sample = next((r.get("failure_reason") for r in ordered
                       if r.get("failure_reason")), None)
        keys = sorted({r.get("key") for r in members if r.get("key") is not None})
        clusters.append({
            "signature": sig,
            "count": len(members),
            "first_seen": first_seen,
            "last_seen": last_seen,
            "sample": sample,
            "keys": keys,
        })

    # Worst-first: count descending, ties broken by most-recent last_seen.
    # Stable sort -> apply the secondary key first, then the primary.
    clusters.sort(key=lambda c: c["last_seen"] or "", reverse=True)
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters
