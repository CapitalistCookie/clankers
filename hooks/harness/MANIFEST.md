# hooks/harness: the distributed global hooks (MANIFEST)

This directory is the distribution source for the generic global hooks of Claude Code.
`clanker sync --apply` copies each top-level file in this directory to `~/.claude/hooks/<name>`.
It does not copy this manifest or the `tests/` directory.
Edit the files here. Do not edit the installed copies.

On 2026-09-24, the operator rewired the installed hooks. After that rewire, each file in this
directory became byte-identical to its installed copy. `clanker sync --check` compares the two
copies by SHA-256. `clanker sync --apply` does not overwrite an installed copy that someone edited
by hand. It stops and tells you to use `--force`.

> The literals in this manifest are masked (`/home/<user>`, `192.168.x.x`), so that this file
> stays clean under the publint grep.

## Rules for code in this directory

- A code file must not contain an operator home path, a namespace slug, a LAN address, or an
  operator ID. `ci/publint.sh` enforces this rule.
- A gate reads operator values from the environment. The Bash dispatcher sources
  `~/.claude/harness.env` and exports the values to its gates.
- A global hook must not read `~/.claude/research.env`. That file also holds API keys for
  third-party services.

## Distributed files

| File | Installed wiring | Purpose |
|---|---|---|
| `README.md` | none (document) | The layout of the global hooks and the hook contract. |
| `pretooluse-bash-dispatch.sh` | PreToolUse `Bash`, timeout 60 s | Runs the four generic Bash gates. See the gate table. |
| `gpu-vm-guard.py` | dispatcher gate 2 | Deny or advice for commands to the GPU host. Inert when `GPU_HOST` is empty, except for `gpu-train`. |
| `pre-commit-verification.sh` | dispatcher gate 3 | Advice before a commit, only in a repo that the clanker registry lists with the archetype `research`. |
| `ssh-tunnel-port-guard.py` | dispatcher gate 4 | Deny for a local forward to a port that is in use. |
| `context-gauge.sh` | PostToolUse `*`, timeout 10 s | The fast wrapper of the context gauge. It needs `context-gauge.py` in the same directory. |
| `iron-law-check.sh` | Stop, `asyncRewake`, timeout 30 s | Blocks a success claim that has no evidence token. It has a `--selftest`. |
| `last-assistant-msg.py` | none (dependency) | `iron-law-check.sh` uses it to read the last assistant message. |
| `closure-claim-verifier.sh` | PostToolUse `Bash`, `if: Bash(git commit *)` | Warns when a commit claims a closure but shows no integration-test evidence. |
| `post-build-review-reminder.sh` | PostToolUse `Bash`, `if: Bash(git *)` | Asks for comments and concerns after `git commit` or `git push`. |
| `governance-gates-autorun.sh` | PostToolUse `Edit\|Write`, timeout 150 s | Runs the gates of a research spec directory after an edit to one of its registry files. |
| `task-payload-gate.py` | PreToolUse `TaskCreate\|TaskUpdate`, timeout 30 s | Limits the size of the task registry. |

## Other files that sync manages

| Source in this repo | Installed as | Label in `sync --check` |
|---|---|---|
| `hooks/context-gauge.py` | `~/.claude/hooks/context-gauge.py` | `gauge` |
| `hooks/session-start.sh`, `hooks/session-end.sh`, `hooks/skill-tracker.sh`, `hooks/subagent-tier-gate.py` | `~/.claude/hooks/clanker-dist/<name>` | `repo-run` |
| `lib/*.py` | `~/.claude/hooks/lib/<name>` | `lib` |

`lib/synccmd.py` holds the lists that sync uses. The `lib` set is there because
`hooks/session-end.sh` imports `projects` and `handoff` from `$HOOK_DIR/../lib`.

## Bash dispatcher gates

| Order | Gate | Budget (s) | In this directory |
|---|---|---|---|
| 1 | `check-compute.sh` | 5 | No. It holds operator-specific content. |
| 2 | `gpu-vm-guard.py` | 15 | Yes |
| 3 | `pre-commit-verification.sh` | 5 | Yes |
| 4 | `ssh-tunnel-port-guard.py` | 10 | Yes |

CAUTION: Install the dispatcher together with its two Python gates. The dispatcher starts a
Python gate with `python3 <path>`. If the file is missing, `python3` exits with code 2, and the
dispatcher blocks the command. If a Bash gate is missing, `bash` exits with code 127, and the
dispatcher skips that gate.

## Tests

`tests/test_context_gauge.sh` is the selftest of the context gauge. Sync does not copy it. The
installed copy is `~/.claude/hooks/tests/test_context_gauge.sh`. The two copies differ in one line
only: this copy finds the hooks directory from its own path.

To test the repo copies of the gauge:

1. Copy `context-gauge.sh`, `../context-gauge.py`, and `tests/test_context_gauge.sh` into one
   scratch directory. Keep `tests/` as a subdirectory.
2. Run `bash <scratch>/context-gauge.sh --selftest`.
3. Make sure that the output is `context-gauge selftest: 16/16 PASS`.

## Installed hooks that clanker does not manage

These files are in `~/.claude/hooks/`, but not in this directory.

| File | Wired in `~/.claude/settings.json` | Reason |
|---|---|---|
| `check-compute.sh` | Yes, as dispatcher gate 1 | It names an operator path and private hosts. A generic copy needs a new design. |
| `subagent-delivery-gate.py` | Yes, PreToolUse `Agent` | It came on 2026-08-01, after the first vendoring. It is not in this directory. |
| `branded-pdf-guard.sh`, `data-flow-map-check.sh`, `post-deploy-screenshot.sh`, `reducer-design-check.sh`, `research-optimization-check.sh`, `research-rule-guards.sh`, `research-rule9-ast.py` | No | Project-specific. |
| `pwb-post-resolution-real-sim.sh`, `pwb-sessionend-compile.sh` | No | Symlinks into the polymarket research repo. |
| `tests/test_pretooluse_dispatch.sh` | not a hook | The parity test of the dispatcher. It contains operator paths and addresses. |

## Files removed on 2026-09-24

The rewire of 2026-09-24 moved or retired these files. `git rm` removed them from this directory,
so git history keeps each file. To read a removed file:

1. Find the commit: `git log --diff-filter=D --oneline -- hooks/harness/<name>`.
2. Show the file: `git show <commit>^:hooks/harness/<name>`.

| File | Result of the rewire |
|---|---|
| `check-git-target.sh`, `deploy-gate.sh`, `backfill-safety.sh` | Moved to the project hooks of the eigenstate repo. |
| `memory-lint.sh` | Retired. Auto-memory is off since 2026-08-08. |
| `pattern-promoter-daily.sh` | Retired. No model read its output. |
| `retro-prompt.sh`, `scope-calibration.sh` | Retired. The Stop entry uses `asyncRewake`, so the model never saw their exit-0 advice. |
| `stop-dispatch.sh`, `STOP-DISPATCH-WIRING.json` | Retired. The Stop entry runs `iron-law-check.sh` directly. |

The same change removed four repo-run hooks from `hooks/`. The rewire had already removed their
wiring. The operator's archive keeps the installed copies.

| File | Reason |
|---|---|
| `agent-resume-surface.sh` | Its queue file is empty since 2026-07-22. |
| `subagent-resume-detect.py` | It read `transcript_path`, which is the transcript of the parent session. Thus it exited on each event and did nothing. |
| `prompt-check.sh` | It read `.user_prompt`, but the field in the payload is `prompt`. |
| `status-stale-nudge.sh` | Its `systemMessage` never gets to the model. |
