# Subagent auto-resume

Turns a usage/rate-limit kill of a subagent from a **silent loss of parallel work**
into an automatic, staged, context-aware retry. Registered globally (all projects)
in `~/.claude/settings.json`.

## Architecture

Two hooks + one queue:

| Piece | Fires | Job |
|---|---|---|
| `hooks/subagent-resume-detect.py` | `SubagentStop` (synchronous subagents) | detect a limit signature in the agent transcript → append the task (original prompt + worktree branch + reset-time hint) to the queue |
| `hooks/agent-resume-surface.sh` | `SessionStart` | surface the pending queue so the parent re-dispatches the killed agents — **context-aware**, staged in one batch |
| `~/.claude/agent_resume_queue.jsonl` | — | the durable, **shared** queue (one JSON object per line) |

Limit signatures matched: `"error":"rate_limit"`, `apiErrorStatus:429`, `"hit your
session limit"`, `usage limit`, `Overloaded`, … (see `LIMIT_SIGNS`).

## ⚠️ Caveat: `run_in_background` agents

**`SubagentStop` fires ONLY for SYNCHRONOUS subagents.** A `run_in_background: true`
agent completes via a **task-notification**, NOT `SubagentStop` — so the detect hook
never runs for it, and a background limit-kill is invisible to the synchronous path.

For background agents the **PARENT (main loop) must do the detection**: the limit
signature is right there in the notification's `<result>`. The parent either:

- **re-dispatches immediately** if capacity is back (context-aware — see below), or
- **persists it for the next session** via the parent-callable path:

  ```bash
  python3 /home/user/projects/clanker/hooks/subagent-resume-detect.py \
    --queue-background --result-file <notif-output-file> \
    --prompt-file <file-with-orig-prompt> --cwd <agent-cwd> --branch <branch>
  ```

  (queued entries carry `source: background-parent`; the next `SessionStart` surface
  prompts the staged re-dispatch).

## Re-dispatch is CONTEXT-AWARE, never a restart

Every re-dispatch message + the surface hook demand the same discipline: **read the
current repo/state first** (`git log`, what's already on `main` and the entry's
`branch`), then tell the new agent **exactly what its partial work already
committed** so it *completes from there* — never restarts from scratch. Pair this
with **per-step commits** in your subagents (one commit per task / User Story) so a
limit-kill never loses more than the single in-flight step.

After you've re-dispatched a queued entry, clear it: `: > ~/.claude/agent_resume_queue.jsonl`
(clear only entries you've handled — the queue is shared).

## Shared-environment note

Multiple Claude sessions can share this box — and sometimes a repo / working tree /
the global resume queue. Don't reset or force-push a shared branch, or clobber
another session's uncommitted files, without coordinating. The queue at
`~/.claude/agent_resume_queue.jsonl` is shared across sessions.

## Paste-ready broadcast for other sessions

```
Heads-up: the global subagent auto-resume hook in this environment was fixed (the
run_in_background agents never fired SubagentStop, so their limit-kills were silently
lost). If you launch subagents: commit subagent work PER-STEP; for background agents,
detect a usage-limit signature in the task-notification <result> yourself and either
re-dispatch context-aware (read current state, complete the partial work on its
branch, don't restart) or persist it with
`subagent-resume-detect.py --queue-background --result-file <f> --prompt-file <f>
--cwd <d> --branch <b>`. Full doc: clanker docs/AGENT_AUTO_RESUME.md. Shared box —
don't reset/force-push a shared branch or clobber another session's files without
coordinating; the resume queue is shared.
```
