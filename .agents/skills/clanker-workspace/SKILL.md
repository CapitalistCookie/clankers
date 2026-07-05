---
name: clanker-workspace
description: Workspace management rules for this development VM. ALWAYS follow these rules when working with projects, repos, or files. Activate automatically at the start of every session.
---

# Workspace Rules

This VM uses clanker for project management. Follow these rules at ALL times.

## Project Location

- ALL git repos MUST live in `~/projects/`
- NEVER clone repos to `~/`, `/tmp/`, or anywhere else
- If a user references a repo not in `~/projects/`, clone it there first

## New Projects

When you encounter a new repo URL or the user asks to work on something new:

1. `cd ~/projects && git clone <url> <name>`
2. `clanker init <name>`
3. Tell the user it's been onboarded

## Session Tracking

If the `clanker` command is available:
- At session START: `clanker ingest --event session_start --project $(basename $PWD) --agent claude-code`
- At session END: `clanker ingest --event session_end --project $(basename $PWD) --agent claude-code --summary "<what was done>"`

## Available Commands

- `clanker registry list` — see all projects
- `clanker briefing <project>` — current project status
- `clanker analyze weekly` — performance analysis
- `clanker alert check` — health checks
- `clanker doctor` — self-diagnostics

## Before Starting Work

Run `clanker briefing <project>` to see what's been happening.
