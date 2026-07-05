---
name: clanker-onboard
description: Automatically onboard a new project — clone repo, detect archetype, register with clanker, set up tmux session, and start working. Use when the user says "clone this repo", "start working on X", "add this project", or provides a GitHub URL for a new project.
---

# Project Onboarding

When the user wants to work on a new project, follow these steps automatically:

## Step 1: Clone

```bash
cd ~/projects
git clone <repo-url> <project-name>
```

If no project name is given, extract it from the URL (last path segment, minus `.git`).

## Step 2: Read the project

Before scaffolding, READ the project:
- Read the README.md (or README.rst, etc.)
- Read package.json, pyproject.toml, Cargo.toml, or Makefile (whichever exists)
- Skim the directory structure
- Understand what the project IS and HOW it works

## Step 3: Register and scaffold

```bash
clanker init <project-name>
```

This auto-detects archetype, creates CLAUDE.md, clanker config, per-project hooks, and tmux session.

## Step 4: Improve the generated CLAUDE.md

The auto-generated CLAUDE.md is generic. Based on what you learned in Step 2:
- Add project-specific rules (coding style, naming conventions, important patterns)
- Add key architecture notes (what the main modules do, how they connect)
- Add any warnings or gotchas from the README
- Keep it concise — bullet points, not paragraphs

## Step 5: Compile knowledge base

```bash
clanker compile
```

## Step 6: Report

Tell the user:
- Project cloned to `~/projects/<name>/`
- Archetype: `<detected>`
- Tech stack: what was detected
- Commands: test, build, lint
- Tmux session `<name>` created
- CLAUDE.md written with project-specific context
- Ready to work

## Rules

- ALWAYS clone into `~/projects/` — never anywhere else
- ALWAYS read the README before scaffolding
- ALWAYS improve the generated CLAUDE.md with real project context
- If the repo already exists in `~/projects/`, skip clone, just register
- If already in the registry, say so and skip
- Do NOT install dependencies automatically (security risk for untrusted repos)
