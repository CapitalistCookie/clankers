"""Cross-project intelligence — detect patterns that transfer between projects."""

import os
from collections import defaultdict, Counter
from datetime import datetime


def analyze_cross_project(last_days=30):
    """Find patterns that could transfer between projects."""
    from analyze import load_sessions
    from registry import Registry

    reg = Registry()
    sessions = load_sessions(last_days=last_days)

    if not sessions:
        print("No session data.")
        return

    # Group by project
    by_project = defaultdict(list)
    for s in sessions:
        by_project[s.get("project", "global")].append(s)

    # 1. Tool usage patterns — find tools heavily used in one project but not others
    print("=== Tool Usage Patterns ===\n")
    project_tool_rates = {}
    for proj, sessions_list in by_project.items():
        if proj == "global" or len(sessions_list) < 3:
            continue
        total = sum(sum(s.get("tool_uses", {}).values()) for s in sessions_list)
        tool_counts = Counter()
        for s in sessions_list:
            for tool, count in s.get("tool_uses", {}).items():
                tool_counts[tool] += count
        if total > 0:
            project_tool_rates[proj] = {t: c / total for t, c in tool_counts.items()}

    # Find tools with high variance across projects
    all_tools = set()
    for rates in project_tool_rates.values():
        all_tools.update(rates.keys())

    for tool in sorted(all_tools):
        rates = [(p, r.get(tool, 0)) for p, r in project_tool_rates.items()]
        if len(rates) < 2:
            continue
        rates.sort(key=lambda x: -x[1])
        max_rate = rates[0][1]
        min_rate = rates[-1][1]
        if max_rate > 0.1 and min_rate < 0.02 and max_rate / max(min_rate, 0.001) > 5:
            print(f"  {tool}: {rates[0][0]} uses it {max_rate:.0%}, {rates[-1][0]} barely uses it ({min_rate:.0%})")

    # 2. Error rate comparison — projects with similar archetypes should have similar error rates
    print("\n=== Error Rate by Archetype ===\n")
    by_archetype = defaultdict(list)
    for proj, sessions_list in by_project.items():
        arch = reg.get_archetype(proj)
        if arch != "unknown":
            err_rate = sum(s.get("errors", 0) for s in sessions_list) / len(sessions_list)
            by_archetype[arch].append((proj, err_rate, len(sessions_list)))

    for arch, projects in by_archetype.items():
        if len(projects) < 2:
            continue
        projects.sort(key=lambda x: -x[1])
        print(f"  {arch}:")
        for proj, rate, count in projects:
            print(f"    {proj}: {rate:.1f} errors/session ({count} sessions)")
        # Flag if any project is >2x the archetype average
        avg = sum(r for _, r, _ in projects) / len(projects)
        for proj, rate, count in projects:
            if rate > avg * 2 and count >= 3:
                print(f"    ** {proj} has {rate/avg:.1f}x the archetype average — investigate **")

    # 3. File overlap — projects touching the same files might have dependencies
    print("\n=== Shared File Patterns ===\n")
    file_to_projects = defaultdict(set)
    for proj, sessions_list in by_project.items():
        for s in sessions_list:
            for f in s.get("files_touched", []):
                file_to_projects[f].add(proj)

    shared = {f: projs for f, projs in file_to_projects.items() if len(projs) > 1}
    if shared:
        # Group by project pair
        pair_files = defaultdict(list)
        for f, projs in shared.items():
            projs_sorted = tuple(sorted(projs))
            pair_files[projs_sorted].append(f)

        for pair, files in sorted(pair_files.items(), key=lambda x: -len(x[1]))[:5]:
            print(f"  {' + '.join(pair)}: {len(files)} shared files")
            for f in files[:3]:
                print(f"    - {f}")
    else:
        print("  No shared files detected (expected for isolated projects)")


def suggest_transfers(last_days=30):
    """Suggest improvements that worked in one project for another."""
    from analyze import load_sessions
    from registry import Registry

    reg = Registry()
    sessions = load_sessions(last_days=last_days)

    # Find projects with improving error rates (might have good hooks/practices)
    by_project = defaultdict(list)
    for s in sessions:
        by_project[s.get("project", "global")].append(s)

    print("=== Potential Transfers ===\n")
    suggestions = []

    for proj, sessions_list in by_project.items():
        if len(sessions_list) < 5:
            continue
        arch = reg.get_archetype(proj)
        err_rate = sum(s.get("errors", 0) for s in sessions_list) / len(sessions_list)

        # Find other projects with same archetype but higher error rates
        for other_proj, other_sessions in by_project.items():
            if other_proj == proj or len(other_sessions) < 3:
                continue
            other_arch = reg.get_archetype(other_proj)
            if other_arch != arch:
                continue
            other_err_rate = sum(s.get("errors", 0) for s in other_sessions) / len(other_sessions)

            if other_err_rate > err_rate * 2 and err_rate > 0:
                suggestions.append(
                    f"{proj} ({err_rate:.1f} err/sess) vs {other_proj} ({other_err_rate:.1f} err/sess) — "
                    f"both {arch}. Check what {proj} does differently."
                )

    if suggestions:
        for s in suggestions:
            print(f"  - {s}")
    else:
        print("  No actionable transfers found (need more data or divergent error rates)")
