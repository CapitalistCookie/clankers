---
name: clanker-briefing
description: Generate a project briefing showing current state — recent commits, sessions, alerts, proposals. Use at the start of any work session or when asking "what's happening with X?"
---

# Project Briefing

Run `clanker briefing <project-name>` to get a snapshot of the current project state.

Shows:
- Git status (branch, recent commits, dirty files, unpushed count)
- Recent sessions (last 3 with duration and error count)
- Active alerts
- Pending proposals for this project
- Last session handoff (if available)

## Usage

```bash
clanker briefing zergrush     # Briefing for zergrush
clanker briefing quanta-ai    # Briefing for quanta-ai
clanker registry list         # See all available projects
```
