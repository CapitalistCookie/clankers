# HANDOFF — clanker harness-reliability round (audit follow-up, 2026-07-22)

**What this is:** internal reliability + observability engineering on our own developer
harness (clanker). Plain software work — queue durability, crash-recovery, telemetry,
CLI ergonomics, performance. **This is NOT a security audit and touches no
credentials, secrets, exploits, or offensive tooling.** The security-flavored items are
carved out below — do not pick them up.

## Framing (read first)

You are continuing a maintenance round on the operator's personal meta-harness. Every
task here is benign internal tooling on the operator's own machine. Nothing in scope
involves attacking systems, evading detection, or handling secret material. If a task
ever *looks* security-shaped, it's out of scope by definition (see "Out of scope").

## Already DONE — do NOT redo (shipped in `ff4c3ad`, on origin/main)

- **P1** `resume-doc-truncate` — `docs/AGENT_AUTO_RESUME.md` no longer teaches the global
  `: > queue` truncate; it now points at `agent-resume-surface.sh --clear`.
- **P4** `briefing-no-upstream-guard` — `lib/briefing.py` no-upstream crash fixed;
  `tests/test_briefing.py` added (2 cases, green).
- **P5a** — global `MEMORY.md` line 52 trimmed 251→246 chars; `memory-doctor: all files pass`.

The ledger still lists these three as `pending` (there's no resolve verb yet — that's P8).
Mark them resolved as you review; don't re-implement them.

## Work queue — priority order (all benign reliability work)

Proposals are in the ledger: `clanker review` (source `harness-audit-2026-07-22`). Full
"ready-to-apply shape" for each is in `docs/audit-2026-07-22-harness-governance.md` §8
(read §4 for the defect write-ups). **Skip §9 of that report entirely** (see below).

1. **P2 `resume-clear-archive`** — *highest value; directly prevents recurrence of the
   loss that started this whole round.* `--clear` currently deletes queue entries; make it
   append the dropped entries (`status=cleared`, `resolved_at`, `resolved_by`) to
   `agent_resume_queue.resolved.jsonl` under the existing flock. Also stop scoped clears
   from deleting unscoped entries (R2). ~15 lines in `hooks/agent-resume-surface.sh` + 2
   `--selftest` cases. Patch sketch in audit §5 (R1).
2. **P3 `work-spawns-boot-entry`** — crash-resilience gap the 07-22 OOM exposed: sessions
   from `clanker work`/`scratch` write no boot-map entry, so they don't survive a reboot.
   After a successful spawn in `cmd_work` (`bin/clanker`), upsert the boot entry for
   registered projects (`tmux_manager.startup_entries`/`write_startup`); `--no-boot` opt-out.
   ~8 lines; test pattern exists in `tests/test_tmux_manager.py`.
3. **P10 `clanker-resurrect-verb`** — the one-command post-OOM/reboot recovery: regenerate
   the startup map from the registry and relaunch missing tmux sessions. Composes
   `startup_entries()` + registry + `newsession.spawn()` — all pieces already exist.
4. **P6 `session-end-failure-reason`** — telemetry: `hooks/session-end.sh` records no
   end/failure reason. Grep the transcript tail for the already-cataloged limit/API-error
   signatures (reuse `LIMIT_SIGNS` from `subagent-resume-detect.py`) and write a
   `failure_reason` field + a last-assistant-line into the handoff. Unblocks
   signature-driven proposals. ~30 lines.
5. **P7 `session-heartbeat-rows`** — long-lived sessions emit zero telemetry until they
   die (07-20/21 had no rows while ~49 sessions ran). SessionStart writes an `open` stub
   row; SessionEnd upgrades it. Cap `duration_s` at write, keep `wall_clock_s` raw.
6. **P5 (b–d) `memory-debt-burndown`** — data hygiene: (b) gc raises an ALERT when
   `memory_doctor` FAILs instead of burying it in cron stdout (`lib/cleanup.py`, ~5 lines
   calling `alerts._create_alert`); (c) gc sweeps registered project namespaces, not just
   global; (d) weekly digest lists top-N orphans for triage. (P5a already done.)
7. **P8 `propose-add-verb`** — `clanker propose --add --project X --desc … --impact …`
   manual path + non-bare `except` on ledger parse (`lib/propose.py:19-23`).
8. **P11 `session-start-perf-consolidate`** — session-start.sh spawns ~6 serial python
   interpreters; consolidate into one. Behavior-preserving; measure with the 2026-07-05
   hook-tax harness first.
9. **P12 `alerts-project-field-escalation`** — alerts carry a `project` field + an
   `ignored_days` escalation (a warning ignored N days bumps ntfy priority).

**Paper-cuts** (fold into any passing commit, no ledger entry): README pipe example (L1),
`plugin.json` version bump + `cmd_version` fallback (L2), `datetime.utcnow()` /
`tarfile.extractall(filter=)` deprecations (L5), prune old `settings.json.bak*` (L6).

## Out of scope — do NOT touch (removes all guardrail-adjacent surface)

- **P9 `memory-lint-secret-re-broaden`** — operator-owned; leave it in the ledger untouched.
- **Audit report §9 "Security posture"** — a statement of fact, operator-accepted. Do not
  act on it, extend it, or re-audit it.
- Anything involving credentials, secret material, the `secret`/age store, auth internals,
  penetration/attack framing, or detection evasion. None of that is part of this round.

## Engineering discipline (repo laws — non-negotiable)

- **Stage explicit paths, never `git add -A`** (CLAUDE.md Law 8) — other sessions share this tree.
- **Hook edits are deploys** (Law 3): run the hook's `--selftest` in the same change.
- **Repo ↔ live parity** (Law 4): edits to a hook with a `clanker-dist/` copy must round
  through `clanker sync`. Check `clanker sync --check` (parity table).
- **Commit bodies for substantive changes carry `Integration evidence (cross-component):`**
  (Law 7) with observed tokens (ci output, selftest counts) and one of the literal phrases
  `cross-component` / `integration test` / `end-to-end test` / `regression-safety`.
- **Gate before commit:** `bash ci/fast.sh` must print `[ci/fast] all green` (currently
  440 passed, 1 skipped, ~30s). Land fixes WITH tests.
- **No `Co-Authored-By` trailers.** End commit messages with the `Claude-Session:` line.
- Trunk-based: commit to `main` (that's how this repo operates). Push when green.

## Deploy consent

Implement, test, commit, and push freely. The one thing that needs an operator nod is
**`clanker sync --apply`** (the live deploy of hook changes into `~/.claude`) — batch it
when a coherent set is ready and confirm before applying, per the operator's
"no production deploy without consent" rule. Repo commits/pushes are fine autonomously.

## State anchors

- HEAD: `ff4c3ad` on origin/main; tree clean at handoff time; 0 unpushed.
- Gate: `bash ci/fast.sh` → `[ci/fast] all green`, 440 passed / 1 skipped.
- Ledger: `clanker review` — 12 audit proposals (3 done above, P9 out of scope, 8 to do).
- Multi-session: other clanker sessions may be live in this tree. Re-check
  `git status --porcelain` right before staging; stage only your own paths.
