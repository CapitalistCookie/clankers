# Clanker — Platform Independence Design

**Design Document — 2026-04-07 04:34 UTC**

---

## 1. Problem

Clanker v0.5.0 is coupled to Claude Code in 6 places: hooks, session data format, settings.json, skills format, plugin manifest, and environment variables. This prevents it from working with Cursor, Cline, Windsurf, Gemini CLI, or any other AI agent.

## 2. Key Insight

The Agent Skills specification (agentskills.io) is an open standard adopted by 40+ agents. Skills are portable markdown files (`SKILL.md`) discovered from standardized paths. **Skills are portable. Hooks are not.**

But skills can contain instructions that tell the agent to call CLI commands — effectively using the LLM's instruction-following capability AS the hook system.

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Any AI Agent                              │
│  (Claude Code, Cursor, Cline, Gemini CLI, Windsurf, etc.)   │
│                                                              │
│  Reads skill → follows instructions → calls clanker CLI      │
│                                                              │
│  For agents WITH native hooks (Claude Code):                 │
│    hooks also fire deterministically as safety net            │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
              clanker ingest                    ← UNIVERSAL ENTRY POINT
              (accepts events via CLI args)
                      │
                      ▼
        /data/clanker/raw/sessions/            ← UNIVERSAL DATA STORE
                      │
                      ▼
        clanker analyze / propose / etc.       ← ALREADY PLATFORM-INDEPENDENT
```

### Three collection mechanisms

| Mechanism | When | Reliability | Richness |
|-----------|------|-------------|----------|
| **Native hooks** | Agent has hook API (Claude Code, Cline) | Deterministic — always fires | Full — parses transcript |
| **Skill instructions** | Agent reads SKILL.md (40+ agents) | Probabilistic — LLM follows instructions | Medium — LLM reports from memory |
| **CLI wrapper** | Manual or CI/CD | Deterministic | Basic — timing + git diff |

All three feed into `clanker ingest`. The core never knows which mechanism produced the data.

## 4. Repository Structure

```
clanker/
  .agents/                       # Cross-agent skill directory (Agent Skills spec)
    skills/
      clanker-session/SKILL.md   # Universal session tracking skill
      clanker-review/SKILL.md    # Proposal review skill
      clanker-briefing/SKILL.md  # Project briefing skill

  adapters/                      # Agent-specific hooks (NOT portable)
    claude-code/
      hooks.json                 # Claude Code hook declarations
      session-start.sh           # Native SessionStart hook
      session-end.sh             # Native SessionEnd hook (transcript parsing)
      skill-tracker.sh           # PostToolUse[Skill] tracker
      prompt-check.sh            # UserPromptSubmit decomposition
      install.sh                 # Registers hooks in settings.json

  lib/                           # Core (platform-independent)
    ingest.py                    # Universal event ingestion
    events.py                    # Event schema + validation
    analyze.py                   # Analysis pipeline
    propose.py                   # Proposal system
    ... (all existing modules)

  bin/                           # CLI (platform-independent)
    clanker                      # Main entrypoint

  skills/                        # Legacy (Claude Code specific, symlinked to .agents/)
  hooks/                         # Legacy (moved to adapters/claude-code/)
```

## 5. Universal Event Schema

### Required fields (every event must have these)

```jsonl
{
  "event": "session_end",          // session_start | session_end | heartbeat
  "timestamp": "2026-04-07T...",   // ISO 8601
  "agent": "claude-code",          // agent identifier
  "project": "zergrush",           // project name
  "cwd": "/home/user/projects/zergrush"
}
```

### Optional fields (adapters provide what they can)

```jsonl
{
  "session_id": "abc-123",         // auto-generated if not provided
  "duration_s": 3847,              // 0 if unknown
  "outcome": "commit",             // commit|push|deploy|abandoned|unknown
  "agent_version": "2.1.92",      // agent-specific version string

  // Rich metrics (native hooks only)
  "tool_uses": {"Bash": 45},
  "errors": 7,
  "error_tools": {"Bash": 5},
  "files_touched": ["file.py"],
  "files_touched_count": 15,
  "user_corrections": 2,
  "subagent_count": 4,
  "flags": ["architecture-discussion"],

  // LLM-reported metrics (skill-based agents)
  "summary": "Implemented login system, fixed 3 bugs, deployed to staging",
  "files_changed": ["auth.py", "login.tsx"],  // from git diff or LLM report

  // Wrapper metrics
  "exit_code": 0,
  "git_diff_stats": {"insertions": 45, "deletions": 12, "files": 3}
}
```

### Validation rules

- Missing optional fields → treated as 0/empty/unknown (never error)
- Unknown fields → preserved (forward compatibility)
- `session_id` not provided → generated from `hash(timestamp + project + agent)`
- `duration_s` not provided → calculated from session_start/session_end pair if both exist

## 6. `clanker ingest` Command

The universal entry point for all event data.

```bash
# From native hook (stdin JSON — Claude Code format)
echo '{"session_id":"...","transcript_path":"..."}' | clanker ingest --adapter claude-code

# From skill instruction (CLI args — any agent)
clanker ingest --event session_start --project zergrush --agent cursor
clanker ingest --event session_end --project zergrush --agent cursor \
  --duration 3600 --outcome commit \
  --summary "Built login page, fixed auth bug" \
  --files-changed "auth.py,login.tsx"

# From wrapper
clanker ingest --event session_end --project zergrush --agent generic \
  --duration 120 --exit-code 0 --git-diff-stats '{"insertions":10}'

# From CI/CD
clanker ingest --event session_end --project quanta-ai --agent ci \
  --outcome deploy --summary "Deployed v42 to production"

# Pipe JSONL (batch import)
cat events.jsonl | clanker ingest --batch
```

### Return value

For `--event session_start`, prints the generated session ID to stdout:

```bash
SESSION_ID=$(clanker ingest --event session_start --project zergrush --agent cursor)
# ... work happens ...
clanker ingest --event session_end --session-id "$SESSION_ID" --project zergrush --agent cursor
```

## 7. Universal Session Tracking Skill

```markdown
# .agents/skills/clanker-session/SKILL.md
---
name: clanker-session
description: Track development sessions for analysis and improvement.
  Call clanker CLI at session start and end to log metrics. Use when
  starting or finishing work in any project.
---

# Session Tracking

## At the START of a work session

Run this command to register the session:

\`\`\`bash
clanker ingest --event session_start --project $(basename $PWD) --agent <your-agent-name>
\`\`\`

Note the session ID printed to stdout — you may need it later.

## At the END of a work session (or when completing a major task)

Run this command with a summary of what was accomplished:

\`\`\`bash
clanker ingest --event session_end \
  --project $(basename $PWD) \
  --agent <your-agent-name> \
  --duration <seconds-worked> \
  --outcome <commit|push|deploy|abandoned|unknown> \
  --summary "<brief description of what was done>" \
  --files-changed "<comma-separated list of files modified>"
\`\`\`

## Important

- Call session_start ONCE at the beginning, session_end ONCE at the end
- Do NOT call on every tool use or error — just start and end
- If you don't know the exact duration, estimate it
- If the outcome is unclear, use "unknown"
```

## 8. Crash Recovery

### The problem

Skill-based "hooks" only fire when the LLM is generating. Crashes, force-quits, and disconnects mean session_end never runs.

### The solution

1. `session_start` creates a record with `status: "active"` in `/data/clanker/raw/sessions/`
2. If `session_end` arrives, the record is completed normally
3. If no `session_end` arrives within 30 minutes of the last heartbeat (or session_start), the health check cron marks it as `status: "crashed"` with whatever data was captured
4. For agents with native hooks, the crash window is smaller (hooks fire on process exit)

### Heartbeat (optional)

For long sessions, the skill can instruct the agent to periodically call:

```bash
clanker ingest --event heartbeat --project <project> --agent <agent>
```

This extends the "alive" window so the health check doesn't prematurely mark the session as crashed.

## 9. Adapter: Claude Code (native hooks)

Moved from `hooks/` to `adapters/claude-code/`. Same functionality as current hooks, but:

- `session-end.sh` calls `clanker ingest --adapter claude-code` with the transcript
- `session-start.sh` calls `clanker ingest --event session_start` and handles alerts/briefings
- `install.sh` registers hooks in `~/.claude/settings.json`

The native adapter provides full-fidelity metrics that skill-based tracking can't match.

## 10. Adapter: Generic (wrapper)

```bash
# Wrap any command
clanker wrap -- cursor .
clanker wrap -- make test
clanker wrap -- ./deploy.sh

# What happens:
# 1. Snapshots git state (branch, dirty files, last commit)
# 2. Calls clanker ingest --event session_start
# 3. Runs the command
# 4. On exit: captures duration, exit code, git diff
# 5. Calls clanker ingest --event session_end with captured data
```

## 11. Migration Path

### Phase 1: Add `clanker ingest` and event schema (non-breaking)

- Add `lib/ingest.py` and `lib/events.py`
- Add `clanker ingest` CLI command
- Existing hooks continue to write directly to JSONL (unchanged)
- Both paths produce compatible data

### Phase 2: Move hooks to adapters (refactoring)

- Move `hooks/` → `adapters/claude-code/`
- Update hooks to call `clanker ingest` instead of writing JSONL directly
- Add `adapters/claude-code/install.sh` for settings.json registration
- Add `adapters/generic/wrap.sh` for the wrapper

### Phase 3: Add universal skills (new capability)

- Create `.agents/skills/clanker-session/SKILL.md`
- Move existing skills to `.agents/skills/` format
- Test with at least one non-Claude-Code agent

### Phase 4: Remove Claude Code assumptions from core (cleanup)

- Remove `~/.claude/` references from `advanced.py`, `alerts.py`, `onboard.py`, `bin/clanker`
- Make `doctor` check adapter configuration instead of hardcoded paths
- Make `export`/`import` agent-agnostic

## 12. What Stays Agent-Specific

| Component | Why |
|-----------|-----|
| `adapters/claude-code/session-end.sh` | Parses Claude Code's specific .jsonl transcript format |
| `adapters/claude-code/session-start.sh` | Uses CLAUDE_ENV_FILE for archetype propagation |
| `adapters/claude-code/install.sh` | Edits ~/.claude/settings.json |
| `adapters/claude-code/hooks.json` | Claude Code hook declaration format |

Everything else — the CLI, analysis, proposals, wiki, alerts, registry, plugins — is already universal.

## 13. What Changes for Users

### Claude Code users (current)

Nothing changes in behavior. The adapter handles everything. `clanker doctor` verifies the Claude Code adapter is installed.

### Other agent users (new)

1. Install clanker CLI: `ln -sf ~/projects/clanker/bin/clanker ~/bin/clanker`
2. Install the session tracking skill: copy `.agents/skills/clanker-session/` to their agent's skills directory
3. Run `clanker init <project>` for each project
4. The agent reads the skill and calls `clanker ingest` during sessions
5. `clanker analyze`, `clanker review`, etc. work exactly the same

### CI/CD integration (new)

```bash
# In CI pipeline
clanker ingest --event session_end --project myapp --agent ci \
  --outcome deploy --summary "CI build #${BUILD_NUMBER}: ${BUILD_STATUS}"
```

## 14. Success Criteria

- [ ] `clanker ingest` accepts events from CLI args, stdin JSON, and JSONL pipe
- [ ] Session data from any source feeds into the same analysis pipeline
- [ ] Claude Code adapter preserves full-fidelity metrics (no regression)
- [ ] Universal skill works in at least one non-Claude-Code agent
- [ ] `clanker doctor` checks adapter configuration, not hardcoded Claude paths
- [ ] Core lib modules have zero `~/.claude/` references
- [ ] All existing tests continue to pass
