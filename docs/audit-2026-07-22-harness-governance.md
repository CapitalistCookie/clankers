# Clanker harness & governance audit — 2026-07-22

**Scope:** the whole system "since opus created it" — repo, live install (`~/.claude`),
runtime data (`/data/clanker`), hooks, memory law, session tracking, crash resilience,
security. Owner-chartered (request originally queued 2026-07-17; **lost to the very
queue-truncation bug examined in §5**; re-dispatched 2026-07-22).
**Method:** read-only audit + receipts. Every claim below carries a `file:line` or a
pasted command output. No harness code was modified; improvements are proposals
(§8, filed to the ledger). Timestamps are pasted, not typed: audit ran
`Wed Jul 22 05:30:08 UTC 2026` ± ~20 min (per-receipt times noted where they matter).

**Audit-window caveat:** a concurrent session was actively working in this repo during
the audit (it pushed the 07-19 round ~05:26Z, committed `c2bc698` ~05:30Z, and
deployed it via `sync --apply` — receipt: `~/.claude` git log `4780703 sync: apply
from clanker@c2bc698`). Findings are pinned to **HEAD = c2bc698**, tree clean at the
05:30-05:34Z receipt window. Post-audit at 05:43:01Z (pasted `date -u`): a further
commit `7e0cece` ("STATUS: OOM-recovery round recorded — resume-queue v2.1 …")
landed and the tree re-dirtied with that session's in-flight work
(`M hooks/harness/pretooluse-bash-dispatch.sh`, untracked
`tests/test_agent_resume_surface.py`). Unpushed count at that moment: 2. Because the
tree held another session's uncommitted work, this audit did NOT commit this report —
it is left untracked for the operator (or the settled session) to commit.

---

## 1. What clanker is

Clanker is the operator's meta-harness for a ~49-session Claude Code fleet: a project
registry with archetypes (`~/projects/.clanker.yaml`), tmux work-session lifecycle
(`work`/`scratch`/`open` + a systemd boot map), per-project contracts (`doctor
--fleet`), session telemetry (SessionEnd → `/data/clanker/raw/sessions/*.jsonl`),
governance hooks (iron-law, memory-lint, Stop/PreToolUse dispatchers) synced from this
repo into `~/.claude` (`clanker sync`, sha256 parity), the memory law (pointer-index
MEMORY.md + sharded routers, lint-enforced), health crons + alerts, a proposals ledger
(`propose`/`review`), a sanitized public export (`publish` with publint+gitleaks
gates), and a mobile web dashboard/terminal (`serve` + Cloudflare tunnel). Born
2026-04-07 as an Opus-authored blueprint (`docs/DESIGN.md`, "v2 — incorporates 46
issues from 11 sweep rounds") that went v0.1.0 → v1.0.0 in two days; 151 commits over
four eras: April foundation (~32), June expansion (~57: ECC, orch, webui), the July-5
harness overhaul (~20: memory hardening, fleet v3, secrets store), and the July-19
flagship round (28: `sync`/`publish`/reader/SPA + test isolation). Single committer
identity throughout; ~13.1k LOC across `bin/` + `lib/` + `hooks/`; suite of 437 tests.

## 2. Snapshot at audit time

| item | state | receipt |
|---|---|---|
| HEAD | `c2bc698` (resume-queue scoped `--clear` + writer flock; `/healthz`) | `git log -1`, 05:30:08Z |
| tree | clean (was dirty 05:26Z with the in-flight fix; committed during audit) | `git status --porcelain` empty |
| remote `origin` | `0a3afcd` = HEAD~1 → **1 unpushed commit** (see §7) | `git ls-remote origin main` |
| suite | **437 passed, 1 skipped, 25.26s** — `[ci/fast] all green` | `bash ci/fast.sh`, 05:32Z |
| `clanker doctor` | all checks passed (incl. hook sync parity 29/29) | run 05:31Z |
| `clanker sync --check` | 29/29 in parity (dist == c2bc698 content) | run 05:33Z |
| gitleaks | `151 commits scanned … no leaks found` | `gitleaks detect`, 05:33Z |
| resume queue | empty (0 lines); fix live in `~/.claude/hooks/clanker-dist/` | `wc -l`, grep `--clear` |
| active alerts | 0 (unpushed-commits auto-dismissed at the 05:30 cron pass) | health log, §7 |
| pending proposals | 16 before this audit's filings | `clanker version`, 05:33Z |

## 3. What's clean (credit where due, with receipts)

- **Test discipline is real.** 437 tests in 25s, isolated from live stores by
  `tests/conftest.py` (CLANKER_DATA→tmpdir; CLAUDE.md law 1), covering the CLI, hooks'
  side-effecting modules, orch, ECC, publish, reader, webauth. The concurrent fix
  landed WITH tests (`tests/test_resume_queue.py`, +30 lines) — the repo's own laws
  are being followed under pressure.
- **Secrets story is sound.** gitleaks: 0 leaks across all 151 commits. Plaintext
  credentials appear in no repo file; the sanctioned store is age-encrypted
  (`~/bin/secret` → `~/.claude/secrets/*.age`; private key outside the repo).
  `~/.claude/harness.env` carries hostnames/config (GPU_HOST, GPU_USER,
  CHECK_GIT_TARGET_REPOS, CLANKER_DEPLOY_PREFLIGHT), not credentials. memory-lint's
  SECRET_RE blocks secret-shaped memory writes (narrow set — see P9).
- **The publish pipeline is genuinely gated.** `ci/publint.sh` (operator-path lint,
  fragment-assembled so it never matches itself) + gitleaks are fail-closed in
  `clanker publish`; STATUS records them catching 3 real leaks-in-waiting including
  the identity list inside publint itself.
- **`clanker sync` closed the repo↔live drift hole.** The 2026-07-17 audit's #1
  structural finding (hooks running unpinned from the working tree) is fixed:
  settings.json pinned to `clanker-dist/`, sha256 parity table, git snapshots of
  `~/.claude` either side of every apply, per-file selftests on install
  (`lib/synccmd.py:1-20,132-167`). Observed working live: the fix deployed during
  this audit left parity 29/29 and a snapshot commit trail.
- **Alert lifecycle verified end-to-end during the audit.** 05:15 health pass:
  `"unpushed": {"status": "warning", "details": {"clanker": 29}}`; operator pushed
  ~05:26; 05:30 pass: `"unpushed": {"status": "ok", "details": {}}` and the alert
  file auto-dismissed (`/data/clanker/raw/health/2026-07-22.jsonl`). `first_seen`
  survives cron rewrites (`lib/alerts.py:22-35`) so a 3-day-ignored alert is visible
  as such.
- **Hook engineering quality is high where it matters.** memory-lint: 20/20 selftest,
  fail-closed with exact-fix messages, bash-guard against lint-bypassing shell writes,
  namespace-scoped to avoid repo false positives (`hooks/harness/memory-lint.sh`).
  The Stop chain is consolidated into one dispatcher with per-gate budgets
  (`hooks/harness/stop-dispatch.sh`; wiring doc `STOP-DISPATCH-WIRING.json`).
  `hooks/harness/MANIFEST.md` documents the vendoring file-by-file with
  de-personalization diffs, selftest receipts, and flagged judgment calls — unusually
  rigorous provenance.
- **Ledger hygiene exists.** Proposals auto-expire after 30 days pending
  (`lib/propose.py:39-66`) — "a surface nobody reads is not a surface."
- **OOM telemetry capture worked** (see §6): all 11 dying sessions got SessionEnd
  rows at the teardown moment, sessions up to 19 days old included.

## 4. Defects found

Severity: **H** = will cause a repeat loss/wrong behavior; **M** = degrades a designed
capability; **L** = paper cut / hygiene.

**H1. The queue-truncation instruction survives in the doc.**
`docs/AGENT_AUTO_RESUME.md:50` still says: *"After you've re-dispatched a queued
entry, clear it: `: > ~/.claude/agent_resume_queue.jsonl`"*. The hook banner was fixed
by `c2bc698`, but any agent following the DOC (it is linked from README.md:108)
re-acquires the global-truncate behavior and re-creates the exact loss mode of §5.
One-line fix (P1). The doc's queue table (line 15) and shared-environment note
(line 58) also deserve the scoped-clear language.

**M1. "work-spawns-boot-entry" was recorded as adopted but never implemented.**
Commit `6b37105` ("retro-2: … work-spawns-boot-entry (recurrence→gate …)") changed
only STATUS.md (+2 lines). Today: `newsession.spawn()` (`lib/newsession.py:108-145`)
writes no boot entry; `tmux_manager.add_session` — the only boot-map writer — is
called solely from the legacy `init` verb and the manual `tmux add` verb
(`bin/clanker:267-268, 277-283`). Consequence: sessions created via `clanker work`/
`scratch` do not survive a reboot or tmux-server death. The live fleet mostly
predates this (startup map ≈52 entries vs 49 live sessions), but every NEW
work-session silently lacks resurrection. This is the recurrence→gate law
(docs/SELF_IMPROVING_LOOP.md:57-58) violated by its own recording mechanism: a
retro item that never graduated from prose. (P3; also P10.)

**M2. Briefings lose their entire Git section on non-tracking repos.**
`lib/briefing.py:33-36` shells `git rev-list --count @{upstream}..HEAD`; with no
upstream this yields empty stdout, `int(unpushed)` at line 39 raises `ValueError`,
and the bare `except:` at line 46 discards branch, recent commits, AND dirty state
from the SessionStart briefing — precisely the repos (fresh/local-only) where a
cold-start session most needs git context. 3-line fix (P4).

**M3. Memory-law enforcement is write-time-only; the standing corpus fails it and
nothing surfaces the failure.**
Live receipts from `memory-lint.sh --doctor` on the global namespace
(`~/.claude/projects/-home-user/memory`, 291 `.md` files), run 05:34Z:
- `MEMORY.md` itself **FAILS the lint**: line 52 is 251 chars (limit 250) — the
  router violates its own law by one character, meaning some write path bypassed the
  hook (or predates the current check).
- **182 orphans** — files unreachable from MEMORY.md and every `*-POINTERS.md` shard
  (INDEX_ALL.md §ORPHANS). The sharded-router migration (2026-07-19) shrank the
  index 16,351B→5,529B; a large share of the pointer lines evidently went away
  without their targets being re-homed.
- **5 topic files >64KB** (session-log-sized): `construction-mgmt-history.md`,
  `construction-mgmt-system.md`, `toxicflow_project.md`, `andrea-gex-map-recipe.md`,
  `es_short_satisfaction_algo_fix.md`.
The weekly gc DOES run this doctor — but only on the global namespace, and its
`memory_doctor: FAIL` lands in gc's result dict → cron stdout that nobody reads
(`lib/cleanup.py:90-105`). Per-project namespaces are never swept. So the system has
been *measuring* this debt weekly and *telling no one*. (P5.)

**M4. Session telemetry has structural blind spots.**
- No `end_reason`/`failure_reason` field in the session record
  (`hooks/session-end.sh:256-275`) — already recognized in STATUS.md DECISIONS as
  the missing input that leaves `propose` generating threshold-noise proposals
  instead of concrete failure signatures. (P6.)
- Long-lived sessions contribute **zero** telemetry until they die: 07-20/07-21 have
  no session files at all while ≥49 sessions ran; the 07-22 OOM finally flushed 11
  rows (§6). Cost/error analytics silently exclude the fleet's steady state. (P7.)
- `duration_s` is stored uncapped (rows up to 1,647,021s ≈ 19 days); every consumer
  must independently remember to cap (`bin/clanker:48`, `lib/briefing.py:58`,
  `lib/propose.py:92` all do `min(x, 28800)`) — one future consumer will forget.
  Cap at write, keep a `wall_clock_s` raw field. (P7.)

**L1. README composability examples are partly fiction.**
`README.md:73` pipes `clanker sessions --json | clanker analyze --by project`, but
`cmd_analyze` (`bin/clanker:55-57`) and `lib/analyze.py` never read stdin — the piped
data is ignored and analyze re-loads from disk. Only `sessions` accepts piped input
(`bin/clanker:37-40`). Fix the example or add stdin support.

**L2. Version identity is frozen.**
`.claude-plugin/plugin.json` says 1.0.0 (set 2026-04-08; ~100 commits since);
`cmd_version`'s fallback prints "v0.2.0" (`bin/clanker:94`). Harmless until someone
uses the version to reason about installs; `snapshot`/`harness_version_snapshot`
partially compensates.

**L3. session-start.sh fragility.**
The `/clear` metrics path picks "the previous transcript" as
`ls -t … | head -2 | tail -1` (`hooks/session-start.sh:27`) — wrong under concurrent
sessions in one project dir. Project/registry values are interpolated directly into
inline `python3 -c` strings (lines 43-52) — a quote in either breaks the block
(fail-open, so it degrades silently). Same pattern in `hooks/session-end.sh:282-287`.

**L4. The plugin manifest is a silently weaker install.**
`hooks/hooks.json` (the `.claude-plugin` path) carries no Stop dispatcher, no
PreToolUse bash-dispatch, no memory-lint PostToolUse — i.e., none of the governance
chain. README.md:19-20 does call it "secondary, partial", but nothing in the manifest
itself says so; an operator installing via plugin gets session tracking without any
of the gates and no warning.

**L5. Forward-compat warnings already visible in the suite.**
`lib/handoff.py:39` `datetime.utcnow()` (deprecated); `lib/publishcmd.py:82`
`tarfile.extractall` without `filter=` (Python 3.14 behavior change). Both printed in
the 05:32Z `ci/fast.sh` run.

**L6. `~/.claude` repo hygiene.**
10 uncommitted files at 05:33Z plus five `settings.json.bak*` siblings; sync's
timestamped backups accumulate forever (`lib/synccmd.py:197-198` creates, nothing
prunes). The rollback story (§SELF_IMPROVING_LOOP invariant) depends on this repo
staying coherent.

**L7. Proposal ledger robustness.**
`lib/propose.py:19-23`: corrupt ledger lines are swallowed by a bare `except` —
silent data loss in the improvement pipeline. And there is no manual-proposal verb:
this audit had to append schema-faithful rows to the ledger directly (P8).

## 5. The queue-truncation bug (the one that ate this request)

**Anatomy.** Two hooks + one shared file: `SubagentStop` →
`hooks/subagent-resume-detect.py` appends limit-killed subagent tasks to
`~/.claude/agent_resume_queue.jsonl`; `SessionStart` → `hooks/agent-resume-surface.sh`
displays pending entries. The display was project-scoped (cwd-filtered), but the
banner's clear instruction was **global**: `: > ~/.claude/agent_resume_queue.jsonl`.
Any session that processed *its* entries truncated *everyone's*. With one queue file
shared by ~49 sessions, the first project to resume after any disruption erased every
other project's pending re-dispatches.

**Consequences, concretely:** this audit request (queued 2026-07-17) was destroyed by
another project's clear and resurfaced only because the owner re-dispatched it
manually five days later. The 2026-07-22 host-OOM mass restart (~49 sessions resuming
at once — comment in `hooks/agent-resume-surface.sh:14-22`) proved the race at scale.

**The fix that landed during this audit (`c2bc698`, reviewed here):**
- `--clear` mode removes only THIS project's pending entries: flock on `$Q.lock`,
  filter, atomic `os.replace` rewrite; non-pending, unparseable, and other-project
  lines survive verbatim.
- One shared `mine()` predicate for count/list/clear — display and clear can no
  longer drift on scope. Path match is component-wise (`/proj/ab` is NOT under
  `/proj/a` — the old `startswith` prefix bug is dead).
- The writer (`subagent-resume-detect.py:_queue_entry`) now takes the SAME lock
  around its dedupe-read + append, closing the read-filter-replace vs
  read-dedupe-append interleave.
- Banner now instructs `bash <hook> --clear` and explicitly forbids truncation.
- `--selftest`: **14/14 PASS** (re-run during this audit), covering scope, prefix
  collision, unscoped entries, garbage lines, non-pending preservation.
- Deployed: dist copy carries the fix (5 `--clear` occurrences; `~/.claude` snapshot
  commit `4780703 sync: apply from clanker@c2bc698`).

Assessment: the fix is correct, tested, and live. It is the right shape (per-entry
removal under a shared lock, exactly what this audit would have proposed).

**Residual gaps (proposed, not blocking):**
- **R1 — cleared ≠ archived.** `--clear` deletes entries. The June convention
  (`~/.claude/agent_resume_queue.resolved.jsonl`, last used 2026-06-13) appended
  resolved entries with `resolved_by`/`resolved_reason`. Had clears archived, the
  07-17 loss would have been diagnosable in seconds instead of five days. Patch
  sketch (inside the existing `mode == "clear"` branch, under the same flock):

  ```python
  # in _scan clear, alongside dropped += 1:
  e["status"] = "cleared"
  e["resolved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
  e["resolved_by"] = here
  archived.append(e)
  # after os.replace(tmp, q):
  with open(q.replace(".jsonl", ".resolved.jsonl"), "a") as f:
      for e in archived: f.write(json.dumps(e) + "\n")
  ```
- **R2 — unscoped entries are everyone's to delete.** `mine()` returns True when the
  entry has no cwd (or the session no project) — so project A's `--clear` deletes an
  unscoped entry project B was also surfacing. Safer: delete unscoped entries only
  when `--clear` itself runs unscoped; scoped clears leave them.
- **R3 — background agents still rely on parent discipline.** `run_in_background`
  agents never fire SubagentStop (`docs/AGENT_AUTO_RESUME.md:20-39`); queueing them
  depends on the parent remembering `--queue-background`. Unchanged by the fix;
  inherent to the hook surface — worth restating in the banner text.
- **R4 — display path takes no lock** — a clear mid-display can show a stale count.
  Cosmetic; the clear path is the integrity-bearing one and is locked.

## 6. Crash resilience: how the 07-22 host-OOM actually went

Receipts: `/data/clanker/raw/sessions/2026-07-22.jsonl` (11 rows, timestamps
04:48:52-55Z, i.e., the teardown moment); startup map `~/.tmux-startup.sh` (~52
entries) vs 49 live tmux sessions; `/etc/systemd/system/tmux-sessions.service`
(oneshot, `RemainAfterExit`).

What worked:
- **SessionEnd fired on the mass teardown** — all 11 dying sessions wrote metric
  rows (durations up to 19 days ⇒ these were the long-lived fleet sessions), flock-
  serialized to the day file. Telemetry captured the crash rather than losing it.
- **Recovery by design, not by luck:** the startup script recreated the fleet
  (fresh `claude` per project — state re-enters via the cold-start contract:
  STATUS.md + git log + briefing + memory injection at SessionStart,
  `hooks/session-start.sh:129-141`). Handoff files (git state per project) were
  regenerated at death and served into the next briefing (`lib/briefing.py:93-102`).
- The queue race this exposed is fixed (§5).

What the OOM exposed (beyond §5):
- **M1**: any session not in the boot map didn't come back on its own.
- **M4**: two full days of fleet activity (07-20/21) have zero telemetry because
  nothing ended.
- The systemd unit is boot-scoped (`oneshot` + `RemainAfterExit`): a tmux-server
  death without reboot needs a manual `systemctl restart tmux-sessions` /
  script re-run — there is no supervised "fleet down → resurrect" path (P10). Also
  note `tmux-sessions.service` names the OLD behavior; the unit itself is not in
  this repo (infra-owned), only the script writer is (`lib/tmux_manager.py:19-48`).
- Handoffs carry git state only — no "what was I doing" line. A cheap enrichment
  exists: `hooks/harness/last-assistant-msg.py` already extracts the final assistant
  message; one line of it in the handoff would make post-crash briefings materially
  better. (Folded into P6's session-end work.)

## 7. The "29 unpushed commits" standing alert — resolved during audit

The alert was **accurate, then resolved minutes before this audit could read it**:
- The 29 = exactly the 07-19 flagship round (28 commits, `f09320a`..`0a3afcd`) + the
  07-20 commit — unpushed since 07-19 (alert `first_seen: 2026-07-19T07:15:01Z`).
- 05:15:02Z health pass still recorded `{clanker: 29}`; the concurrent session
  pushed ~05:26Z (`git ls-remote origin main` → `0a3afcd`, verified 05:27Z); the
  05:30:01Z pass dismissed the alert. Lifecycle worked exactly as designed.
- **Current state: 1 unpushed commit (`c2bc698`)** — plus this report's commit if
  committed. Local is strictly ahead of `origin/main` (no divergence), remotes are
  the operator's own (`origin` github + `gitlab` mirror), gitleaks is clean over all
  history, and publish-gating protects the public sister repo separately — pushing
  looks safe. **Not pushed** per this audit's constraints; the 15-min cron will
  (correctly) re-raise the alert until the operator pushes.
- Governance note: the alert sat active for 3 days. `first_seen` made that visible,
  but only inside the alert file/briefing. If "warning, ignored N days" should
  escalate (ntfy ping severity bump), that is a one-line rule in the health cron
  (folded into P12's alert-metadata proposal).

## 8. Prioritized improvements (filed to the proposals ledger)

Filed via the ledger mechanism (`/data/clanker/proposals/ledger.jsonl`, source
`harness-audit-2026-07-22`, review with `clanker review`). P1-P12 below; ledger ids
`prop-2026-07-22-clanker-<slug>`.

| # | prio | proposal | ready-to-apply shape |
|---|---|---|---|
| P1 | **now** | Fix `docs/AGENT_AUTO_RESUME.md:50` (+15/58 wording): replace the global truncate with `bash ~/.claude/hooks/clanker-dist/agent-resume-surface.sh --clear` | one-line doc patch; kills H1 |
| P2 | high | Archive-on-clear for the resume queue (R1 sketch in §5) + leave unscoped entries to unscoped clears (R2) | ~15 lines in `agent-resume-surface.sh` + 2 selftest cases |
| P3 | high | Implement work-spawns-boot-entry (M1): after successful spawn in `cmd_work`, upsert the boot entry for registered projects (`tmux_manager.startup_entries`/`write_startup`); `--no-boot` opt-out | ~8 lines in `bin/clanker` cmd_work; test exists pattern in `test_tmux_manager.py` |
| P4 | high | `briefing.py` no-upstream guard: wrap the `rev-list @{upstream}` call in its own try (or `unpushed = unpushed or "0"`), so branch/commits/dirty survive (M2) | 3-line patch |
| P5 | high | Memory-debt burn-down + surfacing (M3): (a) fix global MEMORY.md line 52; (b) gc raises an ALERT when memory_doctor FAILS instead of burying it; (c) gc sweeps registered project namespaces, not just global; (d) weekly digest lists top-N orphans for triage | (a) trivial; (b) 5 lines in `cleanup.py` calling `alerts._create_alert`; (c) loop over `~/.claude/projects/*/memory`; (d) report section |
| P6 | high | `failure_reason` capture in session-end (M4): grep transcript tail for the already-cataloged limit/API-error signatures (reuse `LIMIT_SIGNS` from `subagent-resume-detect.py`) + last-assistant-msg line into the handoff | ~30 lines in session-end.sh PYEOF block; unblocks signature-driven proposals (STATUS DECISIONS revival path) |
| P7 | med | Heartbeat telemetry for long-lived sessions (M4): SessionStart writes an `open` stub row; SessionEnd upgrades it; cap `duration_s` at write, keep `wall_clock_s` raw | schema is append-friendly (last-write-wins by session_id in consumers) |
| P8 | med | `clanker propose --add --project X --desc … --impact …` manual path (L7) + non-bare except on ledger parse | ~20 lines bin/clanker + propose.py |
| P9 | med | Broaden memory-lint `SECRET_RE` (fragment-assembled, self-match-safe): `ghp_`/`github_pat_`, `sk-ant-`, `AIza`, `xox[abp]-`, `AGE-SECRET-KEY-` | 6 fragment lines + selftest fixtures |
| P10 | med | `clanker resurrect`: regenerate the startup map from the registry + relaunch missing sessions; the one-command post-OOM/reboot recovery (also callable by a tmux-server watchdog) | compose of `startup_entries()` + `registry` + `spawn()`; all pieces exist |
| P11 | low | SessionStart perf: session-start.sh spawns ~6 serial python interpreters (archetype yaml, briefing, memory self-heal, JSON escape) — consolidate into one `python3` entry; measure with the 2026-07-05 hook-tax harness first | refactor, behavior-preserving |
| P12 | low | Alerts carry `project` + `ignored_days` escalation: briefing already wants per-project scoping (`lib/briefing.py:63-65` comment); 3-day-ignored warnings bump ntfy priority (§7) | field addition + one rule in alert check |

Paper-cuts batch (no ledger entry; fold into any passing session): README pipe
example (L1), plugin.json version bump + cmd_version fallback (L2), `utcnow()` and
`extractall(filter=)` (L5), prune old `settings.json.bak*` (L6), `/clear` transcript
heuristic comment-or-fix (L3), a "partial install" warning line inside
`hooks/hooks.json`'s description field (L4).

## 9. Security posture (statement of fact, operator-accepted)

No plaintext credentials in the repo (gitleaks, 151 commits) or in scanned memory
files (SECRET_RE doctor pass; 05:34Z). The following are deliberate operator
choices, listed so the audit is honest about the trust envelope rather than to
relitigate them: the fleet runs `claude --dangerously-skip-permissions` with
`CLAUDE_CODE_DISABLE_SANDBOX=1` (`lib/newsession.py:19`, `~/.tmux-startup.sh`);
`~/.claude/settings.json` sets `trustedWorkspaces: ["/"]`,
`skipDangerousModePermissionPrompt`, `defaultMode: auto`; the dashboard is
internet-reachable via Cloudflare tunnel, gated by username+password+TOTP with
lockout (`lib/serve.py:197-212`, `lib/webauth.py`), loopback bind by default, and —
as of `c2bc698` — one unauthenticated exact-match `/healthz` (reviewed: returns
`{ok, build}` only; middleware exemption is exact-match and negatively tested in
`tests/test_serve_state.py::test_healthz_bypasses_auth_but_nothing_else_does`).

## 10. Receipts appendix (key commands)

```
git -C ~/projects/clanker log --oneline -1          # c2bc698 (05:30:08Z)
git -C ~/projects/clanker status --porcelain        # (empty)
git -C ~/projects/clanker rev-list --count origin/main..main   # 1 (post-c2bc698)
git -C ~/projects/clanker ls-remote origin main     # 0a3afcd…
bash ci/fast.sh                                     # 437 passed, 1 skipped, 25.26s; all green
gitleaks detect --source . --no-banner --redact     # 151 commits scanned … no leaks found
clanker doctor                                      # All checks passed (parity 29/29)
bash hooks/agent-resume-surface.sh --selftest       # selftest: 14/14 PASS
bash hooks/harness/memory-lint.sh --selftest        # memory-lint selftest: 20/20 PASS
bash hooks/harness/memory-lint.sh --doctor ~/.claude/projects/-home-user/memory
                                                    # MEMORY.md VIOLATION (line 52, 251 chars);
                                                    # orphans=182; 5 topic files >64KB
tail -2 /data/clanker/raw/health/2026-07-22.jsonl   # 05:15 warning {clanker:29} → 05:30 ok {}
```
