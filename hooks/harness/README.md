# Global Claude Code hooks

This directory holds the global hooks for Claude Code on this machine. Every session runs them. The file `~/.claude/settings.json` wires them.

The last rewire was on 2026-09-24. The record of the retired and moved hooks is in `/data/claude-archive/googleclidev/hooks-retired-20260924/MANIFEST.txt`.

## Layout

A global hook enforces a general rule for all projects. A rule for one project is a project hook, and it goes into that project.

| Scope | Scripts | Wiring |
|---|---|---|
| Global | `~/.claude/hooks/` | `~/.claude/settings.json` |
| Global, managed by clanker | `~/.claude/hooks/clanker-dist/` | `~/.claude/settings.json` |
| Project | `<repo>/.claude/hooks/` | `<repo>/.claude/settings.json` |

Claude Code loads the hooks of a project only for sessions that start in that project. In the command of a project hook, use the path `$CLAUDE_PROJECT_DIR/.claude/hooks/<name>`.

The unit of `timeout` is seconds. A value of 5000 gives the hook 83 minutes, not 5 seconds.

## Global hooks

| Event | Matcher | Filter (`if`) | Script | Timeout (s) | Purpose |
|---|---|---|---|---|---|
| SessionStart | `startup`, `resume`, `clear`, `compact` | none | `clanker-dist/session-start.sh` | 10 | Gives the cold-start brief for the project. |
| PreToolUse | `Bash` | none | `pretooluse-bash-dispatch.sh` | 60 | Runs the four generic Bash gates (see the next table). |
| PreToolUse | `Agent` | none | `clanker-dist/subagent-tier-gate.py` | 5 | Blocks an Agent call that has no explicit `model`. |
| PreToolUse | `Agent` | none | `subagent-delivery-gate.py` | 10 | Blocks an Agent call whose prompt has no contract to deliver the report to `main`. |
| PreToolUse | `TaskCreate\|TaskUpdate` | none | `task-payload-gate.py` | 30 | Limits the size of the task registry, because each reminder sends the full registry again. |
| PostToolUse | `Bash` | `Bash(git *)` | `post-build-review-reminder.sh` | 5 | After `git commit` or `git push`, asks for comments and concerns. |
| PostToolUse | `Bash` | `Bash(git commit *)` | `closure-claim-verifier.sh` | 10 | Warns when a commit claims a closure but shows no integration-test evidence. |
| PostToolUse | `Skill` | none | `clanker-dist/skill-tracker.sh` | 5 | Records which skills run (telemetry only). |
| PostToolUse | `Edit\|Write` | none | `governance-gates-autorun.sh` | 150 | Runs the gates of a research spec directory after an edit to one of its registry files. |
| PostToolUse | `*` | none | `context-gauge.sh` | 10 | Gives the measured percentage of free context. |
| Stop | none | none | `iron-law-check.sh` (`asyncRewake`) | 30 | Blocks a success claim that has no evidence token in the recent tool output. |
| SessionEnd | none | none | `clanker-dist/session-end.sh` | 20 | Records the session metrics for clanker. |

## Bash dispatcher

The dispatcher reads the payload one time. It starts a gate only when the prefilter of that gate matches the command. Each prefilter is a superset of the trigger in its gate.

| Order | Gate | Budget (s) | Prefilter | Result |
|---|---|---|---|---|
| 1 | `check-compute.sh` | 5 | a `python` or `python3` word | Advice about GPU or local compute, and the RAM law. |
| 2 | `gpu-vm-guard.py` | 15 | `gpu-train`, or the `GPU_HOST` address | Deny for `nohup` on the GPU host, `scp` to `/data`, and `docker stop frigate`. Advice for other commands. |
| 3 | `pre-commit-verification.sh` | 5 | `git`, then `commit` | Advice before a research commit, only in repos that have the registry archetype `research`. |
| 4 | `ssh-tunnel-port-guard.py` | 10 | `ssh` with `-L` or `-D` | Deny for a local forward to a port that is in use. |

The budgets add up to 35 seconds, and the timeout of the dispatcher is 60 seconds. A gate that exits with code 2 blocks the command. The dispatcher also blocks a payload that is not valid JSON.

The dispatcher reads `GPU_HOST` and `GPU_USER` from `~/.claude/harness.env`. Do not read `~/.claude/research.env` in a global hook. That file also holds API keys for third-party services.

## Project hooks that came from this directory

These hooks moved out of this directory on 2026-09-24.

| Repo | Script | Event and filter | Timeout (s) | Purpose |
|---|---|---|---|---|
| eigenstate | `.claude/hooks/check-git-target.sh` | PreToolUse `Bash`, `if: Bash(*git*push*)` | 5 | Blocks a push when unpushed commits change `research/` or `docs/papers/`. |
| eigenstate | `.claude/hooks/deploy-gate.sh` | PreToolUse `Bash`, prefilter in the script | 60 | Blocks `--delete-data`. Runs the deploy pre-flight before `deploy-vm.sh` or `spacetime publish`. |
| eigenstate | `.claude/hooks/backfill-safety.sh` | PreToolUse `Bash`, prefilter in the script | 5 | Gives advice before a backfill script runs without `--limit`. |
| polymarket | `clanker_hooks/pwb-pre-commit-bot-change-needs-test.sh` | PreToolUse `Bash`, `if: Bash(*git*commit*)` | 10 | Blocks a commit that changes bot code without a test. |
| polymarket | `.claude/hooks/pwb-risk-surface-review-required.sh` | PreToolUse `Bash`, `if: Bash(*git*commit*)` | 10 | Blocks a commit to the risk surface that has no review marker. |
| polymarket | `clanker_hooks/pwb-pre-deploy-suite-green.sh` | PreToolUse `Bash`, `if: Bash(*polymarket_weather_bot*)` | 120 | Blocks a restart or a kill of the bot when the test suite fails. |
| constructionmanagement, gramdyne-infra | `.claude/hooks/post-commit-oi-scan.sh` | PostToolUse `Bash`, `if: Bash(git *)` | 5 | Lists the open issues that a commit names and that are still open. |
| yon | `.claude/hooks/toxicflow-compute-routing.sh` | PreToolUse `Bash`, `if: Bash(*python*)` | 5 | Advice to send heavy toxicflow compute to the 5070 laptop. |

## Clanker sync

`clanker sync` manages the files that `~/projects/clanker/hooks/harness/MANIFEST.md` lists. They include the hooks in `clanker-dist/`. The clanker repo got the 2026-09-24 rewire in commit 68428ac.

- To change a managed file, edit the copy in the clanker repo. Then run `clanker sync --apply`.
- `clanker sync --check` compares each installed file with the repo copy.
- The file `.clanker-sync-state.json` in this directory holds the hash of each managed file at the last apply.

CAUTION: `clanker sync --apply` does not overwrite an installed file that was changed outside sync. An installed file can match neither the repo copy nor its hash in `.clanker-sync-state.json`. Then the apply installs nothing and shows the three hashes. Copy the change into the repo, or use `--force` to overwrite the installed file. `clanker sync --pin` does an apply first, so the same rule applies.

## Hook contract (Claude Code 2.1.x)

| Mechanism | Does the model see it? | Notes |
|---|---|---|
| Exit code 2 and text on stderr | Yes, as a blocking error | Claude Code ignores stdout on exit code 2. |
| `hookSpecificOutput.additionalContext` | Yes | Only for PreToolUse, UserPromptSubmit, PostToolUse, PostToolBatch, Stop, SubagentStop, and SessionStart. Not for PreCompact. |
| `hookSpecificOutput.permissionDecision` (PreToolUse) | Yes | Use `deny`, `allow`, or `ask`. The old top-level `decision: block` does not work for PreToolUse. |
| `systemMessage` | No | Only the user sees it. |
| Plain stdout with exit code 0 | Only for UserPromptSubmit and SessionStart | For other events, the text is only in the transcript. |

The fields of a hook entry:

- `timeout`: the time limit in seconds.
- `statusMessage`: the text of the spinner.
- `async`: the hook runs in the background.
- `asyncRewake`: the hook runs in the background. It wakes the model only on exit code 2, so the model does not see the output of an exit-0 hook.
- `if`: a permission rule, for example `Bash(git *)`. Claude Code applies it only for tool events. On Stop, SessionStart, or PreCompact, a hook that has `if` never runs.
- Claude Code ignores `rewakeMessage` and `rewakeSummary`. Do not use them.

For a Bash command, the `if` rule matches each subcommand of a compound command. It removes a `VAR=value` prefix before it matches. A `*` can be at any position. A headless test on 2026-09-24 showed this behavior.

About the payload:

- A Stop payload contains `last_assistant_message`. It has no field for the active `/goal`.
- A subagent payload contains a top-level `agent_id`. Its `transcript_path` is the file of the parent session.
- Read the fields of the payload with `jq`. A text match on the raw payload can find the same key inside `tool_input` or `tool_response`. On 2026-09-24, this error caused the context gauge to repeat its first reading after each Agent call.
- Claude Code reloads the hook configuration when a settings file changes. The next tool call uses the new configuration. A headless test on 2026-09-24 showed this behavior.

## Change a hook

1. If the rule applies to one project only, write a project hook.
2. If the rule applies to all projects, write a global hook in this directory.
3. For a new generic Bash gate, add the gate to the dispatcher. Give the gate a prefilter that is a superset of its trigger.
4. Add a RED fixture and a GREEN fixture for the gate to `tests/test_pretooluse_dispatch.sh`.
5. Write the new settings to a temporary file in the same directory.
6. Run `jq .` on the temporary file. Then move the temporary file into place.
7. Change the settings before you move or remove a script.
8. Make sure that no settings file refers to a script before you move that script.

## Tests

1. After a change to the dispatcher or to one of its gates, run `bash ~/.claude/hooks/tests/test_pretooluse_dispatch.sh`. Each row must show PASS.
2. After a change to the context gauge, run `bash ~/.claude/hooks/context-gauge.sh --selftest`.
3. After a change to the iron-law hook, run `bash ~/.claude/hooks/iron-law-check.sh --selftest`.
4. Before you use a new `if` rule, test it in a headless session. Use `claude -p --setting-sources project` in a scratch project that has only that hook.
