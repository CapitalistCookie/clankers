"""Context window intelligence — track file read frequency, suggest pre-loading."""

import os
from collections import Counter, defaultdict
from datetime import datetime


def analyze_file_frequency(project=None, last_days=30, top_n=20):
    """Track which files get read most often per project."""
    from analyze import load_sessions

    sessions = load_sessions(last_days=last_days)
    if project:
        sessions = [s for s in sessions if s.get("project") == project]

    if not sessions:
        print(f"No sessions found{' for ' + project if project else ''}.")
        return

    file_counter = Counter()
    session_counter = Counter()  # in how many sessions was each file touched

    for s in sessions:
        files = set(s.get("files_touched", []))
        for f in files:
            file_counter[f] += 1
            session_counter[f] += 1

    print(f"=== File Frequency{' (' + project + ')' if project else ''} ===")
    print(f"Sessions analyzed: {len(sessions)}\n")
    print(f"{'File':<60} {'Sessions':>8} {'Pct':>6}")
    print("-" * 78)

    for filepath, count in session_counter.most_common(top_n):
        pct = count / len(sessions) * 100
        # Shorten path for display
        short = filepath
        if len(short) > 58:
            short = "..." + short[-55:]
        print(f"{short:<60} {count:>8} {pct:>5.0f}%")

    # Suggest files for SessionStart pre-loading
    high_freq = [(f, c) for f, c in session_counter.most_common(10) if c / len(sessions) > 0.5]
    if high_freq:
        print(f"\n=== Suggested for SessionStart Pre-Loading ===")
        print(f"(Files read in >50% of sessions)\n")
        for filepath, count in high_freq:
            pct = count / len(sessions) * 100
            print(f"  {filepath} ({pct:.0f}%)")


def analyze_session_patterns(project=None, last_days=30):
    """Analyze what files are typically read FIRST in a session."""
    from analyze import load_sessions

    sessions = load_sessions(last_days=last_days)
    if project:
        sessions = [s for s in sessions if s.get("project") == project]

    # We can't determine read ORDER from our metrics (files_touched is a set)
    # But we can identify files that appear in EVERY session — strong candidates for pre-loading
    if not sessions:
        return

    all_files = [set(s.get("files_touched", [])) for s in sessions]
    if not all_files:
        return

    # Files in ALL sessions
    common = set.intersection(*all_files) if all_files else set()
    if common:
        print(f"\n=== Files in EVERY {project or 'all'} session ===\n")
        for f in sorted(common):
            print(f"  {f}")
    else:
        print(f"\nNo files common to all {len(sessions)} sessions.")
