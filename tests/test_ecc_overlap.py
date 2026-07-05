"""Hermetic tests for the ECC file-overlap detector (lib/ecc/overlap.py).

Synthesizes clanker session records in-memory — no filesystem, no session store,
no production data. Exercises: two active sessions sharing a file -> overlap; a
terminal (committed) session sharing a file -> excluded when active_only; noisy
ignored paths (*.lock etc.) dropped; the active heuristic; and overlap_summary.

Run: python3 -m pytest tests/test_ecc_overlap.py -v   (or: python3 tests/test_ecc_overlap.py)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from ecc import overlap  # noqa: E402


def _sess(session_id, files, outcome="unknown", project="workspace"):
    """Build a minimal clanker session record."""
    return {
        "session_id": session_id,
        "project": project,
        "files_touched": files,
        "outcome": outcome,
        "timestamp": "2026-06-10T00:00:00Z",
    }


# ─── is_active_default heuristic ────────────────────────────────────────────────

def test_is_active_default_unknown_and_missing_are_active():
    assert overlap.is_active_default(_sess("s", [], outcome="unknown")) is True
    assert overlap.is_active_default(_sess("s", [], outcome=None)) is True
    assert overlap.is_active_default({"session_id": "s"}) is True  # no outcome key


def test_is_active_default_terminal_outcomes_are_inactive():
    for terminal in ("commit", "push", "deploy", "abandoned", "empty"):
        assert overlap.is_active_default(_sess("s", [], outcome=terminal)) is False, terminal
    # case / whitespace tolerant
    assert overlap.is_active_default(_sess("s", [], outcome="  COMMIT ")) is False


# ─── file_overlaps: the core scenario ───────────────────────────────────────────

def test_two_active_sessions_sharing_a_file_overlap():
    sessions = [
        _sess("session-1", ["src/lib.py", "src/only1.py"]),
        _sess("session-2", ["src/lib.py", "src/only2.py"]),
    ]
    result = overlap.file_overlaps(sessions)
    assert len(result) == 1
    row = result[0]
    assert row["file"] == "src/lib.py"
    assert row["count"] == 2
    assert set(row["sessions"]) == {"session-1", "session-2"}
    assert row["projects"] == ["workspace"]


def test_unshared_files_are_not_reported():
    sessions = [
        _sess("session-1", ["a.py"]),
        _sess("session-2", ["b.py"]),
    ]
    assert overlap.file_overlaps(sessions) == []


def test_terminal_session_excluded_when_active_only():
    # session-3 committed src/lib.py; only session-1 is active -> no live overlap.
    sessions = [
        _sess("session-1", ["src/lib.py"], outcome="unknown"),
        _sess("session-3", ["src/lib.py"], outcome="commit"),
    ]
    assert overlap.file_overlaps(sessions, active_only=True) == []

    # With active_only=False the committed session counts again -> overlap.
    relaxed = overlap.file_overlaps(sessions, active_only=False)
    assert len(relaxed) == 1
    assert relaxed[0]["file"] == "src/lib.py"
    assert relaxed[0]["count"] == 2


def test_mirrors_rust_three_session_case():
    # Direct analog of the Rust list_file_overlaps test: session-1 (active) +
    # session-2 (active) share the path; session-3 (terminal) is excluded.
    sessions = [
        _sess("session-1", ["src/lib.rs"], outcome="unknown"),
        _sess("session-2", ["src/lib.rs"], outcome="unknown"),
        _sess("session-3", ["src/lib.rs"], outcome="commit"),
    ]
    result = overlap.file_overlaps(sessions, active_only=True)
    assert len(result) == 1
    assert result[0]["file"] == "src/lib.rs"
    assert set(result[0]["sessions"]) == {"session-1", "session-2"}
    assert result[0]["count"] == 2


# ─── ignore filtering ───────────────────────────────────────────────────────────

def test_ignored_noisy_paths_excluded():
    sessions = [
        _sess("session-1", ["Cargo.lock", "build.log", "node_modules/x/i.js",
                             "pkg/__pycache__/m.pyc", ".git/index", "real.py"]),
        _sess("session-2", ["Cargo.lock", "build.log", "node_modules/x/i.js",
                             "pkg/__pycache__/m.pyc", ".git/index", "real.py"]),
    ]
    result = overlap.file_overlaps(sessions)
    # Only the non-noisy shared file survives.
    assert [r["file"] for r in result] == ["real.py"]


def test_empty_ignore_disables_filtering():
    sessions = [
        _sess("session-1", ["x.lock", "real.py"]),
        _sess("session-2", ["x.lock", "real.py"]),
    ]
    files = {r["file"] for r in overlap.file_overlaps(sessions, ignore=[])}
    assert files == {"x.lock", "real.py"}


def test_custom_ignore_pattern():
    sessions = [
        _sess("session-1", ["a.tmp", "real.py"]),
        _sess("session-2", ["a.tmp", "real.py"]),
    ]
    files = {r["file"] for r in overlap.file_overlaps(sessions, ignore="*.tmp")}
    assert files == {"real.py"}


# ─── ordering, dedup, projects, predicate override ──────────────────────────────

def test_sorted_by_count_desc_then_file():
    # shared.py touched by 3 sessions, common.py by 2 -> shared.py first.
    sessions = [
        _sess("s1", ["shared.py", "common.py"]),
        _sess("s2", ["shared.py", "common.py"]),
        _sess("s3", ["shared.py"]),
    ]
    result = overlap.file_overlaps(sessions)
    assert [r["file"] for r in result] == ["shared.py", "common.py"]
    assert [r["count"] for r in result] == [3, 2]


def test_repeated_path_within_session_counts_once():
    sessions = [
        _sess("s1", ["dup.py", "dup.py"]),  # one session, listed twice
        _sess("s2", ["dup.py"]),
    ]
    result = overlap.file_overlaps(sessions)
    assert len(result) == 1
    assert result[0]["count"] == 2  # not 3
    assert sorted(result[0]["sessions"]) == ["s1", "s2"]


def test_projects_are_distinct_and_ordered():
    sessions = [
        _sess("s1", ["shared.py"], project="alpha"),
        _sess("s2", ["shared.py"], project="beta"),
        _sess("s3", ["shared.py"], project="alpha"),
    ]
    result = overlap.file_overlaps(sessions)
    assert result[0]["projects"] == ["alpha", "beta"]


def test_custom_is_active_predicate_override():
    # Treat only project=="hot" as active; the shared file is touched by one hot
    # and one cold session -> no overlap under this predicate.
    sessions = [
        _sess("s1", ["shared.py"], project="hot"),
        _sess("s2", ["shared.py"], project="cold"),
    ]
    pred = lambda s: s.get("project") == "hot"  # noqa: E731
    assert overlap.file_overlaps(sessions, is_active=pred) == []
    # Add a second hot session -> overlap reappears.
    sessions.append(_sess("s3", ["shared.py"], project="hot"))
    result = overlap.file_overlaps(sessions, is_active=pred)
    assert len(result) == 1
    assert set(result[0]["sessions"]) == {"s1", "s3"}


def test_handles_missing_files_touched_and_empty_input():
    assert overlap.file_overlaps([]) == []
    assert overlap.file_overlaps([{"session_id": "s1"}, {"session_id": "s2"}]) == []


# ─── overlap_summary ────────────────────────────────────────────────────────────

def test_overlap_summary_rollup():
    sessions = [
        _sess("s1", ["shared.py", "common.py"]),
        _sess("s2", ["shared.py", "common.py"]),
        _sess("s3", ["shared.py"]),
    ]
    summary = overlap.overlap_summary(sessions)
    assert summary["n_overlapping_files"] == 2          # shared.py + common.py
    assert summary["n_sessions_involved"] == 3          # s1, s2, s3
    assert [o["file"] for o in summary["top"]] == ["shared.py", "common.py"]


def test_overlap_summary_top_n_limit():
    sessions = [
        _sess("s1", ["a", "b", "c"]),
        _sess("s2", ["a", "b", "c"]),
    ]
    summary = overlap.overlap_summary(sessions, top_n=2)
    assert summary["n_overlapping_files"] == 3
    assert len(summary["top"]) == 2


def test_overlap_summary_forwards_active_only():
    sessions = [
        _sess("s1", ["shared.py"], outcome="unknown"),
        _sess("s2", ["shared.py"], outcome="commit"),
    ]
    assert overlap.overlap_summary(sessions, active_only=True)["n_overlapping_files"] == 0
    assert overlap.overlap_summary(sessions, active_only=False)["n_overlapping_files"] == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
