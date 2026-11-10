# Session accounts

Status (2026-10-29): built, tested and walked on the box. Not deployed. Branch `session-accounts` (clanker) and
`accounts-d002` (infra).

## 1. Goal

1. `clanker new`, `clanker work`, `clanker tmux add`, `clanker orch spawn` and the web dialog start Claude on an
   account slot that the user selects.
2. The CLI, the statusline and the web terminal show the slot of each session.
3. `clanker open <name> <slot>`, `clanker work <slot> <project>` and the web terminal's ⇄ control move a running session to a
   different slot. The conversation continues.

## 2. Owner decisions (2026-10-29)

| ID | Decision |
|----|----------|
| D1 | A switch copies the conversation into the target slot under the copy rule (6). |
| D2 | Use the box account layer (`claude-acct`) fully: slots, figures and leases. Make it robust and future-proof. |
| D3 | The user always selects the slot of a new session. Nothing picks one. There is no `auto`. |
| D4 | Policy never blocks a user session. Caps, fences, fair share and slot notes produce warnings. "Clanker sessions are user sessions; they supersede other things." |
| D5 | Slot availability is live data from `claude-acct`. A slot that cannot run is not offered. |
| D6 | `work` on a running session without a slot word attaches only. |
| D7 | The web terminal gets a switch control. |

## 3. Facts the design rests on (measured 2026-10-29)

- A slot is a `CLAUDE_CONFIG_DIR` (`/data/accounts/aN`, `claude-acct list --json`). a1 is `~/.claude`.
- Separate in each slot: `projects/`, `sessions/`, `file-history/`, `tasks/`, `.claude.json`, credentials.
- The home slot must launch with `CLAUDE_CONFIG_DIR` UNSET. With it set to `/data/accounts/a1`, Claude uses
  `~/.claude/.claude.json`, which is a different file from `~/.claude.json`.
- `claude --resume <id>` under another slot continues the same session ID when the files are copied first. The
  target slot appends to its copy (spike: a1 → a3 → a1, `-p` and interactive, context kept).
- The trust dialog appears once for each slot and new directory. Claude Code 2.1.312 highlights `No, exit` first.
- `C-c` then `/exit` ends an idle Claude in about 1 s. The pane returns to its shell. The SessionEnd hook runs.
  `C-c` clears an unsent draft.
- The statusline 5H and 7D meters come from the session's own `rate_limits`. They show `?` until the first API call.
- While the dashboard terminal is open, Claude's registry record names the dashboard's grouped `web-*` session, not
  the pane's own session (`web-9e96f80f:@360.%391`). Only the pane ID is reliable.
- npm relinks `~/.npm-global/bin/claude` from time to time. A launch in that gap fails with
  `env: 'claude': No such file or directory`.
- `claude-acct list --json --offline` takes 0.13 s. The online form (login state) takes 0.64 s.

## 4. Parts

| Part | Where |
|------|-------|
| Slots, leases, launch env, slot of a process | `lib/accounts.py` |
| Registry of every slot, record of a pane | `lib/claude_registry.py` |
| Launch, verify, retry, never type into Claude | `lib/newsession.py` (`spawn`, `start_in_pane`, `wait_registered`, `pane_record`) |
| Switch, copy rule, rollback | `lib/session_switch.py` |
| CLI words, picker, `ls`, help, completion | `bin/clanker`; fleet rows and forgiving names: `lib/fleet.py` |
| Dashboard fields, `/api/accounts`, `/api/session/switch`, reconciler | `lib/serve.py` |
| Chips, header, ⇄ picker, new-session select | `lib/web/live.js`, `lib/web/app.css` |
| Slot in WHERE, F5 for each slot, usage harvest | `scripts/statusline.py`, `scripts/statusline-usage-refresh.py` |
| User lease (`lease --user`), D-002 | `~/projects/infra/accounts` |

## 4a. Command line (owner, 2026-10-29: "--account is unnecessary, simplify")

The account is a plain word. Any word shaped like a slot ID (`a1`, `a3`, `a12`) is an account word, in any
position. A slot-shaped typo (`a9`) is an error, never a session name. Two account words are an error.

| Command | Does |
|---------|------|
| `clanker` / `clanker ls [slot] [-a] [--json] [-w [s]]` | every account's 5h/7d usage, then every Claude session, most urgent first |
| `clanker open <project\|session> [slot]` | go to it (section 4c); with a slot and Claude running, move it there first; no name on a terminal: a numbered picker |
| `clanker new [slot] [project] [name]` | another Claude session in a project, opened at once (`-d`: do not open); an existing name is opened |
| `clanker close <session> [-y] [-f] [-n]` | end the session (section 4c) |
| `clanker close --idle 30d [--shells] [-n] [-y]` | every session idle that long, listed, then one question |
| `clanker project [ls\|create\|adopt\|remove\|path]` | the projects and how many Claude sessions run in each |
| `clanker orch spawn [slot] "<task>"` | orchestrated session on that slot (else the orch config `account`) |
| `clanker help [all\|<command>]` | grouped help; `all` lists every verb, rarely used ones included |
| `clanker completion` | bash tab completion (`eval "$(clanker completion)"` in `~/.bashrc`) |

- Names are forgiven: exact, case, spacing (`construction management`), unique prefix. Otherwise "did you mean"
  (a question on a terminal, a list and exit 1 in a script).
- An unknown verb gets a hint: `clanker openclanker` → `clanker open clanker`.
- Only a person at a terminal is attached or switched. A script or a Claude tool call gets the command to run.
- `ls` marks `working?` when a session had no status change for 12 h (stuck or forgotten).
- Old verbs stay and work, hidden from the help: `work` (the same command as `open`, with `--no-attach`,
  `--no-boot`, `--print-path`), `attach`, `scratch`, `list`, `tmux list|add|remove|accept-trust`. The boot script
  calls `tmux accept-trust`; `~/.bashrc` and the repo CLAUDE.md files name `work`.

## 4b. Usage of every account

- Each live session's statusline publishes its account's `rate_limits` to `~/.cache/clanker/limits/<slot>.json`
  (claude-acct figure-mirror format) when its own figures change after an API response. An idle session never
  re-stamps old figures; a session already running when this ships only records its first observation.
- `claude-acct` reads that file as the mirror `clanker-statusline`; the newest figure wins. `clanker ls`, the
  dashboard and every job picker on the box then see live figures without a probe.
- The dashboard shows a row for each account above the sessions: 5h and 7d meters, reset countdowns, session
  count, and the age and source of the figure.

## 4d. Command-line conventions (owner, 2026-10-29: "do 1-12")

The CLI follows the Command Line Interface Guidelines (clig.dev) and what git, gh and docker do.

| Convention | In clanker |
|------------|------------|
| Results on stdout, messages on stderr | `new`, `open` and `close` print messages to stderr. Without a terminal, stdout gets the session name (`s=$(clanker new a3 clanker -d)`), as `docker run -d` prints the container ID |
| `-q` and `--json` | `-q` (also before the verb) keeps only errors and the result. `--json` on `new`, `open`, `close`, `ls`, `project ls` |
| Names in scripts | A terminal forgives case, spacing, prefixes and typos. A script names a session or project exactly (case aside): a prefix that is unique today can match two names later |
| Ask before destroying | `close` and `remove` ask on a terminal; a script needs `-y`. `close -n` / `--dry-run` shows what would happen |
| Ctrl-C | From the moment Claude is told to exit until it runs again (switch) or the session is gone (close), Ctrl-C waits and the terminal says so. SIGTERM and SIGHUP wait too, then take effect. Main thread only |
| Progress | `switch` and `close` print each step on a terminal |
| Speed | `ls`: 1,074 ms before, 336–421 ms after (measured on the box). The checked login state is a file cache shared with the dashboard (5 min); the project list is a file cache that a registry edit or a new repo under a project root makes stale at once |
| Colour | On a terminal only; `NO_COLOR=1` or `TERM=dumb` turns it off. Colours come from the gruvbox palette (`CLANKER_PALETTE`, default `~/.config/gruvbox-material/palette.json`), else the terminal's 16 colours |
| `--version` / `-V` | `clanker 1.1.0` from `.claude-plugin/plugin.json` |
| Examples | `-h` of `new`, `open`, `close`, `ls`, `project`, and the help page |
| Nouns for the rare things | `project ls\|create\|adopt\|remove\|path`; the top-level `create`, `adopt`, `remove`, `registry` still work, hidden |
| `-` for "back" | `open -` goes to the session you came from (the tmux client's session, else the one opened before) |
| Completion | `clanker completion bash\|zsh\|fish` |

- `remove <project>` asks first, lists what it deletes, and ends the project's session through `close` after the
  scheduled-work gate and before anything is removed. A working Claude keeps the project as it is (`-f` overrides).
- `close --idle <duration>`: idle is "no status change" for a Claude session and "no output" for a shell. It skips
  attached sessions, a working, starting or unknown Claude, and sessions that run other programs.

## 4c. Sessions live in projects (owner, 2026-10-29)

Owner: "force everything into a project folder to keep it clean … no session should be projectless". Work that
crosses projects is a folder of its own. Code: `lib/session_flow.py`.

- Every Claude session runs in a registered project: the project root, or a git worktree of the project. The
  project's CLAUDE.md, skills, hooks and conversation history load. The global rules (`~/.claude/CLAUDE.md`) load
  in every project.
- Work that crosses projects is a project of its own (for example `infra`). A session in one project can read and
  edit files in other projects.
- A plain shell (`new --shell`) can run anywhere.
- Every session is kept across reboots (the boot map, `~/.tmux-startup.sh`) until `close`. After a reboot, a kept
  session is a plain shell, and `open` starts Claude in it again.
- Measured on the box before this change: 10 of 21 Claude sessions ran at `~`. All their conversations were in one
  transcript folder (`~/.claude/projects/-home-user`), so `--resume` listed them together.

| Command | Does |
|---------|------|
| `open <name>`, a session running Claude | attach |
| `open <name>`, a session that is a plain shell in a project | start Claude in it on the account named or picked, then attach |
| `open <name>`, a plain shell outside every project | attach to the shell; with a slot word: exit 1 |
| `open <project>`, no session of that name | start the project's session in the project root |
| `new [slot]` inside a project | a session in the top of the git checkout you are in (a worktree stays a worktree), else the project root |
| `new [slot] <project> [name]` | a session in the project root |
| `new` outside every project | starts nothing; a terminal offers the close match, then `clanker create` (default No); a script gets exit 1 and the command to run |
| `close <session>` | `/exit` to each Claude, so the conversation stays on disk; release the session's leases; kill the session and any dashboard terminal on it; remove the boot entry; print the command that continues the conversation |

- A session's name is the name given, else the project's name, else `<project>-2`, `<project>-3`.
- `close` refuses, unless `--force`, when a Claude in it is working, when its prompt box holds unsent text, and when
  other programs run in it. On a terminal it asks (default No). A script needs `-y`. `close` and the account switch
  take the same lock.
- The dashboard's New Session form requires a project (`GET /api/projects`). The name defaults to the project's.
- `clanker ls` shows the PROJECT of each session. A session outside every project shows `—` and its directory.
  Nothing moves these sessions; they end when their owner closes them.

## 5. Launch

1. The account word is required. On a terminal clanker shows the usable slots and asks. In a script it exits 2 and
   lists them. Single-slot mode (no accounts command) has one slot and asks nothing.
2. A slot that cannot run (no dir, not logged in, retired) is refused with its reason. Exit 1.
3. Clanker takes a user lease with `--pid` set to its own process.
4. The launch line pins the slot: `env -u CLAUDE_CONFIG_DIR` for the home slot,
   `env CLAUDE_CONFIG_DIR=/data/accounts/aN` for any other. It also sets `CLANKER_SESSION=<name>`.
5. Clanker types the line only when the pane's foreground process is a shell.
6. Clanker waits for the registry record of the pane (by pane ID, any slot) and answers the trust dialog.
7. If the pane returns to its shell without Claude, the launch fails at once with the pane's last lines. A "not
   found" error (the npm relink) gets one retry after 3 s.
8. The process slot (from `/proc/<pid>/environ`) must be the selected slot. Then the lease moves to the Claude pid.
9. A new tmux server never inherits a parent Claude session's `CLAUDE_CONFIG_DIR` or identity variables.

`CLANKER_LAUNCH_TIMEOUT` (default 60 s) limits step 6.

## 6. Switch and copy rule

Checks before any change: the pane runs Claude; its record is idle; it is not an `exec` launch; the target slot can
run; the copy rule passes for every file. A second switch of the same session is refused (lock).

Then: lease on the target, `C-c`, `/exit`, wait for the exit, copy, relaunch with `--resume <id>` on the target,
verify the same session ID on the target slot, move the lease, release the old lease, log the switch to
`$CLANKER_DATA/account-switch.jsonl`.

Copy rule, for each file of the conversation (`projects/<dir>/<id>.jsonl`, `projects/<dir>/<id>/**`,
`file-history/<id>/**`, `tasks/<id>/**`):

- target missing: copy;
- target equal: skip;
- target is a byte prefix of the source: replace;
- target unchanged since the session left that slot (departure manifest, `$CLANKER_DATA/switch/<id>.json`): replace;
- any other case: diverged. The switch stops before Claude is stopped.

Files are written atomically (temp file, fsync, rename, mode 0600). No source file is changed or deleted.

After Claude stops, a failure relaunches the session on its source slot and verifies it (rollback). The rollback
never types into a pane where Claude runs: it reports that Claude instead. A relaunch that registers late is
accepted when it is on the target slot with the same session ID. A failed rollback raises a `clanker alert` and
prints the manual launch line.

## 7. Leases

- Holder `clanker:<tmux session>`, kind `user`, need 0, ttl 24 h, bound to the Claude pid.
- `claude-acct lease --user` refuses only a slot that cannot run. Every policy reason is a warning on the lease.
- User leases count for every job decision (capacity, `max_leases`, fair share). Automation gives way to the owner.
- The dashboard reconciler runs every hour. It renews each clanker lease and leases each Claude process with
  `CLANKER_SESSION` that holds none. It never leases a session that another launcher started.
- When the accounts command fails, a home-slot launch continues without a lease (warning). Other slots stop.

## 8. Display

- The slot of a session is read from its process and its registry dir, never from a state file.
- `/api/status`: `account` (slot, plan, 5h, 7d), `lease` (id, kind, warning count), `managed` (`CLANKER_SESSION`).
- Session cards: slot chip; red `? account` for an unknown dir; dim `no lease` for a clanker session without a lease.
- Terminal header: slot, plan, 5h, 7d and the ⇄ control.
- Statusline WHERE: `user@host·aN`. The slot map comes from `~/.cache/clanker/accounts.json` (refreshed in the
  background every 300 s).
- F5: one cache for each config dir (`cfg-<sha256[:10]>`), filled by the refresher with that dir's own credentials.
  An a3 session never shows a1's figure.
- `clanker tmux list`: `[account aN]` after each Claude session (pane ID mapping).

## 9. Tests and walk

- `tests/test_session_accounts.py` (25 tests) drives the real `bin/clanker` with fakes in `tests/fakes/`: a tmux that
  runs the launch lines it receives, a claude that registers from its own pid and environment, and a claude-acct.
- `ci/fast.sh` runs the suite with 8 workers (737 tests, about 34 s). Serial, it took 80 s, over the 60 s budget.
- Walk on the box (real Claude on a1 and a3, worktree code, a dev dashboard on loopback):

| Step | Result |
|------|--------|
| `new` without `--account` (script) | exit 2, usable and not-usable slots listed |
| `new --account a2` | exit 1, "not logged in (owner: claude-acct login a2)" |
| `new --account a3` | 4 s; process env `CLAUDE_CONFIG_DIR=/data/accounts/a3`; record in a3; user lease on the Claude pid |
| `tmux list` | `blqc-build` and `cm-lead12` tagged a3, others a1 |
| switch a3 → a1 (CLI) | 6 s; same session ID; `CLAUDE_CONFIG_DIR` unset; lease moved; context kept ("walnut") |
| switch while busy | refused, exit 1; the running task finished |
| web: chips, header, ⇄ switch a1 → a3, new session on a3 | as specified; no account preselected; "Choose an account" when none |
| exit both sessions | both user leases ended with their pids |

Walk of section 4c (2026-10-29, real Claude on a3, worktree CLI, dev dashboard on loopback):

| Step | Result |
|------|--------|
| `new a3 walkx` in `/tmp` (script) | exit 1, "no project 'walkx'", the `clanker create` command |
| `new a3 walknew -d` in `~/projects/clanker` | Claude on a3 in the project root; boot entry written |
| `ls` | `walknew a3 waiting now clanker`; 10 older sessions marked `—`, at `~` |
| `close walknew` (script), then on a terminal with `n` | exit 2 with the summary; then "kept 'walknew'" |
| `close walknew -y` | 1.5 s; no process, registry record, lease or boot entry is left |
| `new --shell walksh` in clanker, `open walksh a3` | Claude started in the kept shell ("was a plain shell") |
| `new --shell walkloose` at `~`, `open walkloose a3` | exit 1, "outside every project" |
| `new a3 clankr walktypo` on a terminal, `y` | "did you mean clanker?", session in clanker |
| web New Session, phone and desktop | 58 projects, none preselected; "Choose a project" until one is chosen; created on a3 in clanker; no page overflow |

## 10. Walk findings fixed before release

1. A launch during the npm relink failed after the full timeout. Now: fail fast, one retry.
2. With a web terminal open, the record names the `web-*` bridge session. Matching by session name failed, and the
   rollback typed a launch line into the running Claude, which sent it to the model as a message. Now: match by pane
   ID; never type unless the pane runs a shell; the rollback reports a running Claude.
3. `/api/session/switch` returned a bare 500 on an exception. Now: a JSON answer that tells the user to look at the
   session.
4. `clanker open` from a Claude tool call inside tmux switched the owner's own tmux client. Now: only an interactive
   terminal is attached or switched.
5. A session that never got a message has no transcript, and the switch refused it. Now: it starts fresh on the
   target.
6. The switch reported Claude's dim prompt-box suggestion as cleared text. Now: dim text (SGR 2) is not a draft.
7. The dashboard showed a4 as usable: the fast account list had never checked its login (`live?`). Now: the
   dashboard and `clanker ls` use the same checked login state (`accounts.slots_display`).
8. `close` offered to resume a conversation that did not exist (Claude never got a message). Now: the hint shows
   only when the transcript exists.
9. `new` with a typo of a project name (`clankr`) offered to create `clankr` first. Now: it offers the close match
   first.
10. `lib/tmux_manager.py` fixed the boot-map path at import time. An in-process caller under a test HOME could
    write the real `~/.tmux-startup.sh`. Now: the path is read at call time (repo law 9).
11. `main` runs a Claude Code from a binary npm has since replaced. It left only `sessions/<pid>.key`, no record,
    and `clanker ls` hid it among the plain shells. Now: a pane that runs Claude without a record shows as Claude in
    the state `unknown`, with the slot read from its process.
12. The F5 refresher failed every 5 min from 2026-10-11 (4340 failures): the home login's usage answer holds only
    `session` and `weekly_all`, with no Fable entry. The statusline kept `F5 61% ↻ reset` from 2026-10-10. Now: a
    well-formed `limits[]` without a Fable entry is cached as "no Fable weekly limit", and F5 is not shown. A Fable
    entry that cannot be read still fails and keeps the cache.
13. A Ctrl-C between Claude stopping and Claude starting again skipped the switch's rollback (it caught `Exception`,
    not `KeyboardInterrupt`) and would have left a bare shell. Now: section 4d. Walked: a real switch a3 → a1 with
    Ctrl-C at 2.5 s ended on a1, exit 0.
14. `clanker remove <project>` killed the project's session without asking and without `/exit`. Now: section 4d.
15. At deploy, the dashboard (a systemd service) ran with a PATH without `~/bin`, found no `claude-acct` and fell
    back to single-slot mode: 18 of 24 Claude panes had a record, the a3 sessions had none. The dev walk had set
    `CLANKER_ACCOUNTS_CMD` and hid it. Now: `~/bin` and `~/.local/bin` are searched after PATH.
16. Orchestrated sessions pinned the slot but took no lease, and could run outside every project. Now: `orch
    spawn` refuses a directory outside every project (`--project` resolves through the registry), marks the
    Claude with `CLANKER_SESSION`, and takes a user lease on its pid at once (the reconciler covers a miss).
    The hidden `tmux add <name> <slot>` keeps the project rule too.
17. A switch cleared unsent text in the prompt box and only reported it. Now: it refuses; `open <name> <slot>
    -f` or the web dialog's second question clears it on purpose.
18. The ntfy token was in a world-readable systemd drop-in (mode 644) and in `systemctl show`. Now:
    `CLANKER_NTFY_TOKEN_CMD` names a command that prints it (the box uses the age-encrypted secret store).

## 11. Known limits

- `wyc/threads.py` (watchyourclankers) puts the production `~/projects/clanker/lib` first on `sys.path` at run time
  (`contract.CLANKER_PROJECTS_PY` is hard-coded). A dashboard that runs from any other checkout then imports
  production modules for every later lazy import. Production is not affected. To fix in that repo.
- After a switch, SendMessage does not reach sessions on other slots.
