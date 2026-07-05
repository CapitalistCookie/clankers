"""Prompt decomposition intelligence — detect tasks likely to be long/complex."""

import os
import re
from collections import defaultdict


def analyze_prompt(prompt_text, project=None):
    """Analyze a user prompt and suggest decomposition if it matches patterns
    associated with long/complex sessions.

    Returns suggestions or None if the prompt looks manageable.
    """
    from analyze import load_sessions, normalize_project

    # Load historical data
    sessions = load_sessions(last_days=90)

    # Build a profile of what makes sessions long
    long_sessions = [s for s in sessions if s.get("duration_s", 0) > 14400]  # >4h
    short_sessions = [s for s in sessions if 300 < s.get("duration_s", 0) < 3600]  # 5min-1h

    if not long_sessions:
        return None

    # Extract keywords from long sessions' files
    long_files = defaultdict(int)
    for s in long_sessions:
        for f in s.get("files_touched", []):
            basename = os.path.basename(f)
            long_files[basename] += 1

    # Check if prompt mentions any high-risk files
    prompt_lower = prompt_text.lower()
    risk_signals = []

    # File-based risk
    for filename, count in sorted(long_files.items(), key=lambda x: -x[1])[:20]:
        if filename.lower() in prompt_lower or filename.replace('.', ' ').lower() in prompt_lower:
            risk_signals.append(f"File '{filename}' appears in {count} long sessions (>4h)")

    # Pattern-based risk
    risk_patterns = [
        (r'\b(refactor|rewrite|migrate|overhaul|redesign)\b', "Refactoring tasks average longer sessions"),
        (r'\b(all|every|entire|complete|full)\b.*\b(service|system|module|component)\b', "Full-system changes tend to be complex"),
        (r'\b(deploy|production|prod)\b', "Deployment tasks have high variance in duration"),
        (r'\bfrom scratch\b', "'From scratch' tasks are typically underestimated"),
        (r'\b(debug|fix|investigate|diagnose)\b.*\b(bug|issue|error|failure)\b', "Debugging can be unpredictable"),
    ]
    for pattern, reason in risk_patterns:
        if re.search(pattern, prompt_text, re.IGNORECASE):
            risk_signals.append(reason)

    # Project-based risk
    if project:
        proj_sessions = [s for s in sessions if s.get("project") == project]
        if proj_sessions:
            avg_duration = sum(min(s.get("duration_s", 0), 28800) for s in proj_sessions) / len(proj_sessions)
            avg_errors = sum(s.get("errors", 0) for s in proj_sessions) / len(proj_sessions)
            if avg_duration > 10800:  # >3h average
                risk_signals.append(f"{project} sessions average {avg_duration/3600:.1f}h")
            if avg_errors > 15:
                risk_signals.append(f"{project} averages {avg_errors:.0f} errors/session")

    if not risk_signals:
        return None

    suggestions = {
        "risk_level": "high" if len(risk_signals) >= 3 else "medium",
        "signals": risk_signals,
        "recommendation": (
            "Consider breaking this into smaller tasks. "
            "Historical data suggests this type of work tends to run long. "
            "Define clear milestones with commit points."
        ),
    }
    return suggestions


def check_prompt(prompt_text, project=None):
    """Print decomposition analysis for a prompt."""
    result = analyze_prompt(prompt_text, project)
    if result is None:
        print("Prompt looks manageable — no decomposition needed.")
        return

    print(f"=== Decomposition Suggestion ({result['risk_level'].upper()} risk) ===\n")
    for signal in result["signals"]:
        print(f"  - {signal}")
    print(f"\n{result['recommendation']}")
