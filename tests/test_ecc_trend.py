"""Hermetic tests for lib/ecc/trend.py — success-rate trend + failure clustering.

Fully deterministic: every run record carries a fixed ISO-8601 ``recorded_at``
and every assertion passes an explicit ``now`` (NOW), so no wall-clock / Date.now
is ever consulted. Exercises improving/worsening/stable trends, the declining
flag, corrections_rate, and that two differently-worded-but-same failures cluster
to a single signature.

Run: python3 -m pytest tests/test_ecc_trend.py -v   (or: python3 tests/test_ecc_trend.py)
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from ecc import trend  # noqa: E402

# Reference instant for every test. Days are counted backwards from here.
NOW = "2026-06-09T12:00:00Z"
_NOW_DT = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _ts(days_ago):
    """ISO-8601 timestamp at 00:00:00Z, ``days_ago`` whole days before NOW."""
    dt = (_NOW_DT - timedelta(days=days_ago)).replace(hour=0, minute=0, second=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rec(key, outcome, days_ago, feedback=None, reason=None):
    """Build a run record dated ``days_ago`` whole days before NOW (00:00:00Z)."""
    return {
        "key": key,
        "outcome": outcome,
        "user_feedback": feedback,
        "failure_reason": reason,
        "tokens": 100,
        "duration_ms": 1000,
        "recorded_at": _ts(days_ago),
    }


# ---------------------------------------------------------------------------
# success_rate_trend
# ---------------------------------------------------------------------------

def test_improving_trend():
    # 30d window: many recent successes + old failures -> 30d rate < 7d rate.
    records = (
        [_rec("k", "failure", d) for d in (10, 12, 14, 16, 18, 20)]   # old, in 30d not 7d
        + [_rec("k", "success", d) for d in (1, 2, 3, 4, 5, 6)]        # recent week, all pass
    )
    t = trend.success_rate_trend(records, key="k", now=NOW)
    assert t["rate_7d"] == 1.0, t
    assert t["rate_30d"] < t["rate_7d"], t
    assert t["trend"] == "improving", t
    assert t["declining"] is False
    assert t["runs_7d"] == 6 and t["runs_30d"] == 12, t


def test_worsening_and_declining_flag():
    # Recent week is mostly failures (rate well below the 0.5 floor); the wider
    # 30d window was healthy -> worsening AND declining.
    records = (
        [_rec("k", "success", d) for d in (10, 12, 14, 16, 18, 20)]   # old: all pass
        + [_rec("k", "failure", d) for d in (1, 2, 3, 4, 5)]          # recent: all fail
        + [_rec("k", "success", 6)]                                   # 1 recent pass
    )
    t = trend.success_rate_trend(records, key="k", now=NOW)
    assert t["rate_7d"] < 0.5, t
    assert t["rate_30d"] > t["rate_7d"], t
    assert t["trend"] == "worsening", t
    assert t["declining"] is True, t


def test_worsening_but_not_declining_when_above_floor():
    # Recent week dips to 0.6 (above the 0.5 floor) while the wider 30d window
    # stays high (~0.87) thanks to a perfect old block -> delta <= -0.15 so the
    # trend is worsening, but rate_7d >= floor so it is NOT flagged declining.
    records = (
        [_rec("k", "success", d) for d in (10, 12, 14, 16, 18, 20, 22, 24, 26, 28,
                                           11, 13, 15, 17, 19, 21, 23, 25, 27, 29)]  # 20 old, perfect
        + [_rec("k", "success", d) for d in (1, 2, 3, 4, 5, 6)]                       # 6 recent passes
        + [_rec("k", "failure", d) for d in (1, 2, 3, 4)]                             # 4 recent fails
    )
    t = trend.success_rate_trend(records, key="k", now=NOW)
    assert t["rate_7d"] == 0.6, t              # 6 / 10 in the 7d window
    assert t["rate_30d"] > 0.8, t              # old block keeps 30d high
    assert t["trend"] == "worsening", t        # delta <= -0.15
    assert t["declining"] is False, t          # rate_7d above the 0.5 floor


def test_stable_trend():
    # Recent and historical rates are equal -> delta 0 -> stable, not declining.
    records = (
        [_rec("k", "success", d) for d in (1, 2, 3)]
        + [_rec("k", "failure", 4)]
        + [_rec("k", "success", d) for d in (15, 16, 17)]
        + [_rec("k", "failure", 18)]
    )
    t = trend.success_rate_trend(records, key="k", now=NOW)
    assert t["rate_7d"] == 0.75, t
    assert t["rate_30d"] == 0.75, t
    assert t["trend"] == "stable", t
    assert t["declining"] is False


def test_empty_windows_are_stable_none():
    t = trend.success_rate_trend([], key="ghost", now=NOW)
    assert t["rate_7d"] is None and t["rate_30d"] is None
    assert t["runs_7d"] == 0 and t["runs_30d"] == 0
    assert t["trend"] == "stable"
    assert t["declining"] is False
    assert t["corrections_rate"] is None


def test_corrections_rate():
    # 4 records in the 30d window, 1 marked corrected -> 0.25.
    records = [
        _rec("k", "success", 1, feedback="accepted"),
        _rec("k", "success", 2, feedback="corrected"),
        _rec("k", "failure", 3, feedback="rejected"),
        _rec("k", "success", 4, feedback=None),
    ]
    t = trend.success_rate_trend(records, key="k", now=NOW)
    assert t["corrections_rate"] == 0.25, t


def test_window_excludes_out_of_range_records():
    # A record 8 days old is in the 30d window but NOT the 7d window;
    # a record 40 days old is in neither (and is unreachable via _rec, so build
    # it by hand to prove the cutoff).
    in7 = _rec("k", "success", 3)
    in30_only = _rec("k", "failure", 8)
    too_old = {**_rec("k", "success", 1), "recorded_at": "2026-04-01T00:00:00Z"}
    t = trend.success_rate_trend([in7, in30_only, too_old], key="k", now=NOW)
    assert t["runs_7d"] == 1, t      # only in7
    assert t["runs_30d"] == 2, t     # in7 + in30_only, too_old excluded
    assert t["rate_7d"] == 1.0 and t["rate_30d"] == 0.5, t


def test_key_filter_isolates_records():
    records = [
        _rec("a", "success", 1),
        _rec("a", "failure", 2),
        _rec("b", "success", 1),
    ]
    ta = trend.success_rate_trend(records, key="a", now=NOW)
    tb = trend.success_rate_trend(records, key="b", now=NOW)
    assert ta["runs_7d"] == 2 and ta["rate_7d"] == 0.5, ta
    assert tb["runs_7d"] == 1 and tb["rate_7d"] == 1.0, tb


def test_no_key_aggregates_all_records():
    records = [_rec("a", "success", 1), _rec("b", "failure", 2)]
    t = trend.success_rate_trend(records, now=NOW)
    assert t["key"] is None
    assert t["runs_7d"] == 2 and t["rate_7d"] == 0.5, t


def test_unparseable_now_raises():
    try:
        trend.success_rate_trend([], now="not-a-date")
        assert False, "expected ValueError"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# trend_by_key
# ---------------------------------------------------------------------------

def test_trend_by_key_sorts_worst_first():
    records = (
        # "healthy" key: all recent successes.
        [_rec("healthy", "success", d) for d in (1, 2, 3)]
        # "declining" key: was perfect, now all failing -> declining True.
        + [_rec("declining", "success", d) for d in (15, 16, 17, 18)]
        + [_rec("declining", "failure", d) for d in (1, 2, 3, 4)]
        # "midpack" key: steady 50% -> stable, not declining.
        + [_rec("midpack", "success", 1), _rec("midpack", "failure", 2)]
    )
    out = trend.trend_by_key(records, now=NOW)
    keys = [t["key"] for t in out]
    assert set(keys) == {"healthy", "declining", "midpack"}
    # Declining key must come first; healthy (rate 1.0) must come last.
    assert keys[0] == "declining", keys
    assert keys[-1] == "healthy", keys
    assert out[0]["declining"] is True


# ---------------------------------------------------------------------------
# normalize_failure_reason
# ---------------------------------------------------------------------------

def test_normalize_collapses_timestamps_paths_numbers():
    a = trend.normalize_failure_reason("timeout after 3211ms at /tmp/x/y.py:44")
    b = trend.normalize_failure_reason("timeout after 9s at /home/a/b.py:9")
    assert a == b, (a, b)
    assert "<path>" in a and "<n>" in a, a
    assert "3211" not in a and "44" not in a, a


def test_normalize_strips_iso_timestamp_and_uuid():
    s = trend.normalize_failure_reason(
        "job 550e8400-e29b-41d4-a716-446655440000 failed at 2026-06-09T12:00:00.123Z"
    )
    assert "<uuid>" in s and "<timestamp>" in s, s
    assert "550e8400" not in s and "2026-06-09" not in s, s


def test_normalize_handles_none_and_blank():
    assert trend.normalize_failure_reason(None) == "unknown"
    assert trend.normalize_failure_reason("") == "unknown"
    assert trend.normalize_failure_reason("   ") == "unknown"
    assert trend.normalize_failure_reason(12345) == "unknown"


# ---------------------------------------------------------------------------
# cluster_failures
# ---------------------------------------------------------------------------

def test_two_worded_failures_cluster_to_one_signature():
    records = [
        _rec("alpha", "failure", 1, reason="timeout after 3211ms at /tmp/x/y.py:44"),
        _rec("beta", "failure", 3, reason="timeout after 9s at /home/a/b.py:9"),
        # A non-failure with the same text must NOT be counted.
        _rec("gamma", "success", 2, reason="timeout after 5s at /var/z.py:1"),
    ]
    clusters = trend.cluster_failures(records, min_count=2)
    assert len(clusters) == 1, clusters
    c = clusters[0]
    assert c["count"] == 2, c
    assert sorted(c["keys"]) == ["alpha", "beta"], c
    assert c["sample"] in (
        "timeout after 3211ms at /tmp/x/y.py:44",
        "timeout after 9s at /home/a/b.py:9",
    ), c
    # first_seen/last_seen span the two failure dates (oldest..newest).
    assert c["first_seen"] == "2026-06-06T00:00:00Z", c   # 3 days ago
    assert c["last_seen"] == "2026-06-08T00:00:00Z", c    # 1 day ago


def test_cluster_respects_min_count():
    records = [
        _rec("k", "failure", 1, reason="disk full at /data/clk-99"),
        _rec("k", "failure", 2, reason="connection refused on port 8443"),
    ]
    # Two distinct signatures, each count 1 -> nothing meets min_count=2.
    assert trend.cluster_failures(records, min_count=2) == []
    # min_count=1 surfaces both.
    assert len(trend.cluster_failures(records, min_count=1)) == 2


def test_clusters_sorted_by_count_desc():
    records = (
        [_rec("k", "failure", 1, reason=f"timeout after {i}s") for i in range(3)]
        + [_rec("k", "error", 2, reason=f"parse error at line {i}") for i in range(2)]
    )
    clusters = trend.cluster_failures(records, min_count=2)
    assert len(clusters) == 2, clusters
    assert clusters[0]["count"] == 3, clusters
    assert clusters[1]["count"] == 2, clusters
    assert "timeout" in clusters[0]["signature"]


def test_cluster_treats_error_and_failed_as_failures():
    # inspection.js groups failure/failed/error alike.
    records = [
        {**_rec("k", "error", 1), "failure_reason": "boom at /a/b:1"},
        {**_rec("k", "failed", 2), "failure_reason": "boom at /c/d:2"},
    ]
    clusters = trend.cluster_failures(records, min_count=2)
    assert len(clusters) == 1 and clusters[0]["count"] == 2, clusters


def test_cluster_groups_missing_reasons_as_unknown():
    records = [
        _rec("k", "failure", 1, reason=None),
        _rec("k", "failure", 2, reason=None),
    ]
    clusters = trend.cluster_failures(records, min_count=2)
    assert len(clusters) == 1, clusters
    assert clusters[0]["signature"] == "unknown", clusters
    assert clusters[0]["sample"] is None, clusters


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
