# Hook contract cheat-sheet (Claude Code 2.1.x — docs-verified 2026-07-05)

> distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)

Four hook bugs in one day traced to the same root: authors re-deriving this contract
from memory. Check THIS table before writing/editing any hook. Full citations: the
cc-docs-guide report, session c488883a (raw docs mirrored in that session's scratchpad).

## Channels (what actually reaches whom)
| Mechanism | Reaches the MODEL? | Notes |
|---|---|---|
| exit 2 + **stderr** | ✅ (as blocking error) | stdout is IGNORED on exit 2 — never print the message there |
| `{"decision":"block","reason":…}` (Stop) | ✅ hard-continue | reason required |
| `hookSpecificOutput.additionalContext` | ✅ | supported events ONLY: PreToolUse, UserPromptSubmit, PostToolUse, PostToolBatch, Stop, SubagentStop, SessionStart. **NOT PreCompact** (strict validator hard-fails) |
| `hookSpecificOutput.permissionDecision` deny/allow/ask (PreToolUse) | ✅ | the modern form; legacy top-level `decision:block` is DEAD for PreToolUse |
| `systemMessage` | ❌ user-only warning | the model never sees it — do not use it to instruct the model |
| plain stdout (exit 0) | context ONLY for UserPromptSubmit/SessionStart | elsewhere: transcript-visible, not instruction |

## Config fields
- REAL: `timeout` (**seconds** — 5000 = a 83-minute hang budget, not 5s), `statusMessage`
  (spinner text), `async`, `asyncRewake` (background; wakes on exit 2; stderr shown),
  `if` (**tool events only** — PreToolUse/PostToolUse/PostToolUseFailure/PermissionRequest/
  PermissionDenied; on Stop/SessionStart/PreCompact a hook with `if` NEVER runs).
- FAKE (silently ignored — do not cargo-cult): `rewakeMessage`, `rewakeSummary`.

## Stop payload stdin
Carries `last_assistant_message` NATIVELY (plus stop_hook_active, background_tasks,
session_crons). The 2026-07-05 "dead trio" existed because hooks read `.assistant_message`
— one wrong field name, never fired for months. There is NO `goal` field: hooks cannot
read the active `/goal`.

## /goal
Built-in session-scoped prompt Stop hook (v2.1.139+): Haiku evaluator blocks stopping
until the condition holds; survives --resume. Generic "are you done?" Stop hooks are
redundant with it (check-tasks.sh deleted accordingly). Custom Stop hooks remain for
DETERMINISTIC checks (iron-law evidence tokens).

## Compaction
PreCompact can only BLOCK (exit 2 / decision:block) — it cannot inject anything.
Project-root CLAUDE.md is natively re-read + re-injected after compaction. Scripted
post-compaction injection = SessionStart hook with matcher `compact` (wired).

## Sessions / memory (for tooling)
- Transcript storage: `~/.claude/projects/<slug(cwd)>/<id>.jsonl` + `<id>/` sidecar dir
  (subagents, tool-results) — MOVE BOTH when migrating; `--resume <id>` scans the current
  dir's slug only. Supported relocation: `/cd`.
- AUTO MEMORY keys on the **git repo ROOT** (subdirs/worktrees share one memory), NOT the
  raw cwd. MEMORY.md: first 200 lines / 25KB auto-loaded; topic files on demand.

## Discipline (unchanged)
Blockers exit 2 with an actionable message on stderr; advisories fail-OPEN on internal
errors; every gate ships a RED/GREEN selftest and must be SEEN red before it counts
(`tests/test_pretooluse_dispatch.sh` for the Bash dispatcher's 12 gates).
