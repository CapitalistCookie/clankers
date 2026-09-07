# Harness audit — how sessions are started, governed, remembered and managed
### CT 112 (googleclidev) · 2026-09-03 · read-only

**Scope (per operator):** the harness around development and project/infrastructure management —
hooks, settings, clanker, memory, skills, governance plumbing, fleet operations. Individual projects
appear only as evidence of what the harness did or did not catch (Appendix C).

**Method:** direct reads of `~/.claude` (settings, 48 hooks, 9 skills, git state), clanker (repo,
registry, telemetry, alerts, sync), the memory corpus (475 files), crontabs/timers/boot map, process
and disk state; every hook selftest run; hook latency measured; six read-only opus subagents swept the
memory corpus (3), per-repo contracts, clanker code and the scheduled-job ledger; a docs agent
verified Claude Code 2.1.259 semantics against code.claude.com. **Nothing was modified, deleted,
committed, restarted or pushed.** Audit window 2026-09-03 00:00–01:20 UTC.

---

## 1. Verdict

The in-session layer is excellent and should not be churned: a single-read PreToolUse dispatcher with
12 gates, a Stop chain with a real evidence check, a measured context gauge, a task-registry budget
gate, and selftests that all pass today (12 ms per Bash call). The failures are all in the
**between-sessions** layer — the part that is supposed to make each new session start well informed,
keep law in one place, keep the fleet healthy, and keep knowledge alive:

1. **The harness has no liveness check for itself.** Five load-bearing things have been silently broken for weeks to months (a production backup 112 nights; the research data feed since July, exiting green; the self-improvement cron since mid-July; session briefings since 19 July; a dashboard unit crash-looping since boot behind a watchdog that logs "ok"). Nothing on the box asserts that the harness's own jobs ran.
2. **Law is fragmented and partly orphaned.** Rules live in six layers (global CLAUDE.md, repo CLAUDE.md, hooks, skills, clanker docs, memory). Memory was switched off on 8 Aug, but 62 cross-project operator rulings and 5 infra guard-rails exist only there, while 14 live documents, 4 hooks, 1 skill and the `clanker work` launcher still treat memory paths as authority.
3. **The most-emphasised rule (19, never delete) is the only one with no mechanical gate**, on a fleet that runs 49 sessions fully unattended.
4. **Alerting is a firehose with no floor**: 25 alerts (22 duplicates of one CI failure) injected into every session of every project, including ~3,600 headless batch sessions a day; cron failures go to a mail spool nobody reads.
5. **The harness's own version control lags disk by a month**, two operator-ruling hooks exist in exactly one place with no history, and `clanker sync` has been half-applied for six weeks.
6. **Secrets law is bypassed by its consumers**: 9 memory files hold live credentials (tracked in git on two remotes), crons use a hard-coded old API key instead of the rotated one in the age store, and the age key has a single copy.

Everything else — telemetry pollution, mis-ranked cost analysis, ghost tmux sessions, an 11 GB
35-day-old Claude process, a registry bug armed to poison project names — follows from those six.

---

## 2. What works (keep)

| Component | Evidence | Note |
|---|---|---|
| `pretooluse-bash-dispatch.sh` (12 gates, bash prefilters) | parity test 25/25; 12 ms trivial call, 141 ms commit path | the right architecture; only gap is the missing destructive-command gate (§3-G3) |
| `stop-dispatch.sh` + `iron-law-check.sh` | selftest PASS; 9,029 fires, 88 violations caught | keep; the once-per-session marker prevents ping-pong |
| `context-gauge.{sh,py}` | 13/13; post-compact staleness fix; subagent grounding; fallback-shrink floor | keep |
| `task-payload-gate.py` | 34/34; born from a measured 1.33 M-token blowout | keep; **version it** (it is untracked everywhere) |
| `subagent-tier-gate.py`, `subagent-delivery-gate.py` | 9/9 | keep; the docs agent confirms the harness backgrounds every subagent by default, so the delivery contract is a cheap belt-and-braces |
| `hooks/README.md` contract sheet | claims re-verified against docs (B.7) | one correction: default hook timeout is 600 s, not 60 |
| `memory-lint.sh` (as a design) | 20/20 | inert while memory is off; its secret regex missed every real credential (§4) |
| clanker `sync --check/--apply/--pin`, `doctor --fleet`, `schedules audit`, `publish` | code reviewed; `ci/fast.sh` 515 green in 58 s | sound designs, under-applied |
| researchgov (`_spec/` + fail-closed gates + auto-run hook) and the D31 law-tier pattern in fableNQkronos | init.sh green-or-refuse; ci-gated context budgets | the two best governance patterns on the box; the checker only recognises the first (§3-G11) |
| `verify-operational-preconditions` and `pre-flight-verification` skills | content quality | the probe list of the first is exactly what the missing liveness check should run |

---

## 3. Systemic gaps (the management layer)

### G1 · No liveness for the harness's own plumbing
**Symptoms found today** (all silent, all "green" from the outside):

| What | Broken since | Root cause | Evidence |
|---|---|---|---|
| eigenstate prod SQLite nightly backup | 2026-05-14 | `~/backups/eigenstate-sqlite/` does not exist; the cron's `>>` redirect fails before rsync starts; 112 cron mails, unread | `/var/mail/user`; `ls ~/backups` |
| Weekly `clanker analyze/propose/digest` | ~2026-07-12 | unescaped `%s` in the crontab line; cron cuts the command at the first `%`, `$(date +` left open → `sh: Syntax error` | `journalctl -u cron` (CMD truncated at `week-`); last `week-2026-07-05.md`; A.3 |
| Session briefings + handoffs fleet-wide | 2026-07-19 | `session-start.sh:81` imports `../lib/briefing` from `~/.claude/hooks/lib/` (absent); `2>/dev/null \|\| true` at :87 hides the `ModuleNotFoundError`; `sync --apply` pending consent since 22 Jul; dist hooks are July versions | `clanker sync --check` 27/70; B.6(a) |
| Research data feed | July | Databento `402 account_delinquent_invoice` on both hosts; `daily_data_collector` prints COMPLETE while 4/5 products are stale; two scripts hard-code the **old** key as a fallback and crons never export the rotated one | 182 × `402`; A.6 |
| `kronos-dashboard.service` | since boot (40 d) | watchdog's DIRECT-SPAWN orphan holds :8643; systemd's unit dies `Address already in use`; watchdog curls the orphan → "ok" | `systemctl --user status`; `ss -ltnp` |
| clanker DR rsync log | 2026-07-20 | `rsync -a` is silent on success → 0-byte log; success is unobservable | `reports/backup.log` |

**Why the harness missed them:** `clanker alert check` (every 15 min) covers disk/unpushed/tmux/tunnel/
cloudflared/locks only. Cron output goes to `/var/mail/user` (467 KB). Twelve jobs log to `/tmp`
(lost on reboot) or nowhere. The `verify-operational-preconditions` skill encodes the right probes but
is a per-deploy ritual invoked once in 30 days.

**Fix:** (a) `~/bin/cronwrap <name> -- <cmd>` writes `~/.local/state/cron/<name>.{ok,fail}` stamps and
posts to the clanker alert sink on non-zero; convert every crontab/cron.d line; `MAILTO=""`.
(b) `clanker doctor --live` (daily, under cronwrap): sync parity, hook selftests, briefing renders,
weekly-report age, per-job stamp age vs cadence, per-product data freshness, dashboard units bound by
their MainPID, any `claude` > 4 GB RSS, disk, DR log has a success stamp. (c) Stop hiding errors in
`session-start.sh:87` — emit "briefing unavailable: <reason>" instead of nothing.
**Verify:** each row in the table above has a concrete check in Appendix A.

### G2 · Law is fragmented; memory was switched off without migrating it
- **Six layers**: `~/.claude/CLAUDE.md` (20 rules, 38 lines), 33 repo CLAUDE.md files, 48 global hooks + ~40 repo hooks, 9 global + 63 repo skills, clanker docs, and 475 memory files. No index says which rule is enforced by what.
- **Memory off since 2026-08-08, but:** global rules 17 and 18a still say "Detail: memory `…`"; `.clanker-arcs.yaml` (11 projects) still injects absolute memory paths into every `clanker work` launch (`newsession.py:118` → `memoryns.ensure_memory_stub`, which also keeps *writing* stubs into the frozen corpus); `check-compute.sh`, `closure-claim-verifier.sh`, `subagent-tier-gate.py`, `skills/wsl2-5070-devenv` and 12 repo docs cite memory files as authority; `pattern-promoter-daily.sh` (SessionEnd) and `memory-lint.sh` (PostToolUse) run against an inert store; `clanker gc` regenerates its indexes weekly.
- **What is stranded there (the three memory sweeps):** 62 cross-project operator rulings with no live home — the largest cluster is *autonomy* (six files, repeated operator rebukes: execute autonomously, halt only at genuine forks, never stop between approved plan items, a reported live bug is standing authorisation to fix-deploy-verify); plus no-band-aid fixes, secrets stay on-prem, tmux on this box is read-only for tests, never move session transcripts, contract-first before editing, recurring behavioural failure ⇒ build the hook that session, discover credentials before asking, re-verify handoff claims, subagent NOT-FOUND is a hypothesis (40 % false-negative measured), audits must run things, verify the first commit landed on the agent branch, compute write-scope overlap before parallel dispatch, pipe-test new gates, rule 18 extends to baselines; 17 research-validity gates that belong in the researchgov standard; 5 infra guard-rails absent from `infra/` (Turing-GPU SBR/vtconsole wedge = 18 h host outage, OOM fleet-kill fix + recovery, UDM Teleport bounce = mass-SSH-drop signature, CT115 reconciler "gaps" are unmapped channels, the research-data HTTP server); `harness-overhaul-2026-07-05.md` — the source of the memory law — exists only in memory.
- **What is dangerous there:** fableNQkronos's namespace carries 8 memories that the repo has since retracted or quarantined (three name banned frames verbatim in the router lines that load first) — never re-enable memory for that namespace without a RETRACTIONS gate.
- **What is inert there:** 25 stub namespaces (MEMORY.md + INDEX_ALL.md boilerplate), 18 dead-project files, 26 project-history files, 24 eigenstateresearch memories orphaned by the polymarket `filter-repo` split (that repo has no CLAUDE.md).

**Fix:** §6.3 (rotate → promote → freeze → repoint → lint) and a generated `clanker/docs/LAW-INDEX.md`
(rule → location → enforcer) with `doctor` checks that fail on memory citations while memory is off.

### G3 · Rule 19 is prose-only on a fully unattended fleet
49 boot sessions launch `claude --dangerously-skip-permissions`; `defaultMode: auto`;
`skipDangerousModePermissionPrompt`. (`trustedWorkspaces: ["/"]` is **not a documented key** and does
nothing — trust lives in `~/.claude.json` and does not gate hooks anyway.) So hooks and CI are the
only safety layer, and the dispatcher has no gate for `rm -rf`, `git reset --hard`, `git clean -f`,
`git push --force`, `git branch -D`, `find … -delete`, `truncate`/`: >` on non-tmp paths, `DROP`,
`zfs destroy`, `pct|qm destroy`, `crontab -r`. The harness's own doctrine (researchgov README): "prose
discipline does not survive sessions; only mechanical gates do." The precedent loss (415 dirs, 580 MB)
was irrecoverable.
**Fix:** a fail-closed `destructive-command-guard` in the dispatcher with a selftest and an explicit
override token the model must obtain from the operator per invocation; scratchpad/`$TMPDIR` exempt.

### G4 · Alerting is a firehose with no floor
- 25 active alerts; 22 are cm "FULL suite RED" (10 for commit 2a72736); 2 are a resolved AWS case; all "ignored 22 d". The dedup in `lib/alerts.py:34` works, but `:317` mints `manual-<timestamp>` ids, `:318` hard-codes `info` (so `_escalate_ignored` never escalates), and `project=None` (so the banner cannot scope). Clanker's own `ci/full.sh:62,69` already does it right (stable per-repo id, cleared on green).
- `session-start.sh` prints **every** alert into **every** session, including ~3,600 `claude -p` batch sessions/day from lyric-generator (each gets a 5,989-byte hook context; 3,739 transcripts and 3,965 session-env dirs since 25 Aug; `analyze weekly` now reports "lyric-generator 3,904 sessions"). The documented fix is `--bare` (or `--settings '{"disableAllHooks":true}'`) in `claude_cli.py:45`.
- Cron failures: only `/var/mail/user`.
**Fix:** alert id = `(source, project, stage, commit)`; caller-supplied severity; auto-clear on green; project-scoped banner (project alerts + global-only); headless sessions skip hooks; cronwrap (G1).

### G5 · The cold-start contract is unenforced
The 5 July design: a session cold-starts from the repo alone via the injected `head -30` of STATUS.md +
`git log -5` + the briefing. Today: the briefing is dead (G1); the banner is 25 alerts; and nothing
checks the shape of what gets injected. Evidence the contract has rotted: `constructionmanagement/
STATUS.md` is 2,555 lines with `## NOW` at line 771 (cold start reads a 15 Aug essay); STATUS stubs
seeded 5 July for eigenstate/eigenstateresearch were never refined; 27 roots have neither CLAUDE.md nor
a state file. fableNQkronos solved this locally with ci-gated context budgets (CLAUDE ≤120,
STATE ≤200, fact routing) — the fleet should inherit that gate.
**Fix:** fleet-wide budget gate in `doctor` and pre-push (CLAUDE.md ≤150; STATUS/STATE ≤200; `## NOW`
within the first 30 lines); banner ≤ 1 KB; measure injected bytes per session start.

### G6 · Fleet management: boot map ≠ registry ≠ reality
- Boot map (`~/.tmux-startup.sh`, managed by `clanker tmux`): 49 sessions; 3 target the missing `~/projects/cottondashboard` and fall back to `$HOME` (`:68`) — three full-permission sessions boot in the home dir on every boot and every `clanker resurrect` (`tmux_manager.py:167` keeps non-registry names). Duplicates: 6 × eigenstateresearch, 5 × ISD, 4 × toxicflow/nq, 4 × `$HOME`. 59 tmux sessions live.
- Processes: 58–72 `claude`, 24–25 GB RSS, load 14/24, 11 GB swap. One process (`fableNQkronostransformerresearch`) is **11.4 GB and 35 days old** (44 % of all claude memory). RSS is not monitored; the 22 July host OOM killed the whole fleet.
- Registry (`.clanker.yaml`): `collab-stack` is nested under `aliases:`; `registry.py` never reads `aliases`, and `projects.py:113` stringifies the dict, so a session in that repo resolves to a 122-char project name — armed to poison telemetry/alert tags (0 rows yet). `spec-kit` has no archetype → zero hooks, never self-heals. ~15 active repos are auto-discovered, not declared. Two "archive candidates" flagged 5 July are still registered.
- Disk: root 86 % (17 GB free) while `/data` has 4 TB free. Nothing needs deleting: agent worktrees 7.1 GB (cm 24 worktrees with `node_modules`), CUDA venvs/models/DBs ~10 GB, `~/.local` 18 GB — movable to `/data` with symlinks; worktrees prunable after merge confirmation.
**Fix:** registry validation on load; boot map derived from registry + 30-day activity (telemetry has it); `resurrect` skips missing dirs instead of `$HOME`; RSS + disk checks in `doctor --live`; lazy launch (tmux window now, `claude` on attach).

### G7 · The harness's own state is not under control
- `~/.claude` (git, two private remotes) last commit 8 Aug: `settings.json` (+21 lines), `CLAUDE.md`, 24 `.age` secrets, `task-payload-gate.py`, `subagent-delivery-gate.py` uncommitted. The task gate is also untracked in clanker; the delivery gate is in **no repo**. 472 tracked memory files show as deleted because `projects/` became a symlink on 29 Aug; the SessionEnd autocommit (`git add -A -- projects/*/memory`) can no longer add through it and silently no-ops.
- `clanker sync`: 27/70 parity; a future `--apply` would sweep the untracked gates into a side-effect commit (`synccmd.py:113`). The "publishable clanker" and the "live ~/.claude" have diverged for six weeks.
- `~/.claude/projects → /data/claude-archive/...` symlink is unsupported (docs: use `CLAUDE_CONFIG_DIR`); it also broke git tracking and the memory-lint path regex.
- Config drift: `env.CLAUDE_CODE_EFFORT_LEVEL="max"` overrides `effortLevel: "xhigh"` (env tier wins; effective = max); `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING` is a no-op on Fable 5.1; `autoCompactEnabled:false` (full-window behaviour undocumented); `cleanupPeriodDays: 3650` covers session-env/file-history/tasks too, so nothing is ever swept; `superpowers` is installed only for the deprecated quanta-ai yet 12 files mandate its skills; a third-party marketplace is registered; 6 `settings.json.bak*` + 5 `.claude.json.*` copies; `/tmp/orch-smoke` project entries in `.claude.json`.
**Fix:** commit now; SessionEnd autocommit covers `hooks/ settings.json skills/ secrets/ CLAUDE.md`; both gates into clanker's manifest; decide `sync --apply`; `git rm --cached` the memory tree once frozen; remove the no-op keys.

### G8 · Secrets law is bypassed by its consumers
- Nine memory files hold **live plaintext credentials** (Razorpay live key-id + secret, Deepgram, Google AI, Grafana admin + InfluxDB token, research-data basic-auth, Titrin worker key, PostHog, Hikvision, Nextcloud admin ×2) — tracked in git on GitHub and GitLab; `memory-lint`'s regex (`glpat|cfut|db-|AKIA|PGPASSWORD|PEM`) matched none of them.
- Two yon scripts hard-code the old Databento key as an `os.environ.get(..., 'db-…')` fallback; crons never export the rotated key (age store, 23 Aug) — rotation cannot work while consumers embed values.
- Plaintext files: `~/.databento_api_key` (the old key), `~/.polygon_api_key`, `~/.discord_bot_token`, `~/.modal.toml`, `/data/clanker/.{ntfy-credentials,cf_tunnel_token,auth_token}`; a PostgreSQL password in `/var/mail/user`; `secrets/--help.age` junk.
- The age private key exists on CT112 only ("NEVER copied") — single point of failure for all 47 secrets; 24 `.age` files are untracked (no off-box copy either).
**Fix:** rotate the nine + the mail-spool password first; broaden the lint regex (ledger P9, "operator-owned" since July) and run it as a git pre-commit in `~/.claude`; migrate consumers to `secret get`; decide an **encrypted** off-box copy of the age key (compatible with the "on-prem only" ruling).

### G9 · The self-improvement loop is currently zero-yield
Telemetry is polluted (3,900 headless rows + 264 manual `ingest` rows from the `clanker-session`
skill); `analyze.py:220-221` ranks on error-hours and never reads `estimated_cost_usd` (the $782 batch
spend is invisible, ranked last at 0.3); the weekly cron is dead (G1); 108 proposals pending never
reviewed, 78 expired, 11 implemented; the loop was TABLED 19 July; `retro-prompt` nudged 31× in 30 days,
`session-retrospective` ran 0× in August. `pre-flight-verification` fell from 299 uses to 4/30 d.
**Fix:** either retire the cron (approval) or fix it and make the digest per project; tag headless
rows; real-dollar column; make the retro produce `docs/retro/<date>.md` and have the Stop nudge check
for the file; feed `clanker propose --from-retro` (flag exists).

### G10 · Skills: the global set needs pruning, the project set needs triggers
See §5. Three global skills are CLI wrappers, one (`clanker-session`) actively double-counts telemetry,
two are misplaced (research-specific / cotton-specific), 12 files mandate an uninstalled plugin, 19/63
project skills have no "Use when…" trigger so cannot auto-select.

### G11 · The governance checker measures form, not substance
Three governance dialects coexist — researchgov `_spec/` (3 repos), D31 laws + gates (fableNQkronos),
spec-kit constitution + checklist (4 repos) — but `doctor --fleet` recognises only the first, so the
best-governed research repo "fails". 10/49 projects meet the contract; 22 lack `ci/fast.sh`.
**Fix:** `governance: {dialect, gate_cmd}` per project in `.clanker.yaml`; doctor *runs the declared
gate*; hoist D31's context-budget gate fleet-wide (G5).

### G12 · No off-box copy of the things that matter most
Covered nightly: `/data/clanker` + registry → tank16 (silent log), trade-relay state, collab audit tags.
**Not covered from this box:** `~/projects` (79 GB; 5 repos have no remote: collab-stack,
lyric-generator, gramdyne-cad, omnigentfork, fairvaluegaps), `~/.claude` (git a month stale), the age
key. Proxmox vzdump may cover CT 112 — unverifiable from inside; record it in `infra/hosts/jangmojib.md`.
**Fix:** nightly rsync of `~/projects` (exclude `node_modules`, `.venv`, `.claude/worktrees`) + `~/.claude`
+ encrypted age key → tank16 with a success stamp; remotes for the five repos (GitLab CE on CT120 exists).

---

## 4. Memory — state and disposition

**State.** `autoMemoryEnabled: false` (docs: neither read nor written). 475 files: global namespace 294
(132 `feedback*` = 390 KB; 161 others = 2.1 MB; 181 orphans; frontmatter complete on all non-feedback
files), eigenstate 50, eigenstateresearch 37, fableNQkronos 29, cm 5, small/stub namespaces 30.

**Classification (three sweeps):**

| Class | Files | Bytes | Disposition |
|---|---|---|---|
| Feedback: LOST-GENERAL (no live home) | 62 | — | promote to global CLAUDE.md / researchgov / hooks |
| Feedback: LOST-PROJECT | 21 | — | promote to the owning repo (yon has no CLAUDE.md; polymarket has none) |
| Feedback: covered (global 7 / repo 9 / hook 11), duplicates 4 clusters, stale 18 | 49 | — | freeze; stale + duplicates listed for approval |
| DURABLE-REFERENCE (infra facts, data semantics, runbooks) | 52 | 243 KB | promote to `infra/` and owning repos; top five absent from infra |
| RESEARCH-FINDING | 49 | 935 KB | 44 already in repos; 5 unhomed (Andrea corpus 53 KB, two Andrea fixes, tribar, laptop GEX tape) |
| PROJECT-HISTORY | 26 | 583 KB | freeze (forensics) |
| DEAD-PROJECT (yon, comms, cotton era…) | 18 | 140 KB | freeze; approval list |
| HARNESS-DESIGN | 10 | 79 KB | `harness-overhaul-2026-07-05.md`, `local_ci_cd_convention.md` → `clanker/docs/` |
| SECRET-BEARING (overlay) | 9 live + 4 partial | — | **rotate, then redact** |
| Project ns: eigenstateresearch 24 orphaned by the polymarket split; fableNQkronos 8 CONTRADICTED + 9 with broken "codified in CLAUDE.md" pointers; eigenstate mostly present (Razorpay credential); 25 stub namespaces | — | — | promote 21 items (B.3), never re-enable fableNQkronos ns without a RETRACTIONS gate |

**Disposition, in order:**
1. **Rotate** the nine credentials and the mail-spool PostgreSQL password; then redact the lines to `$(secret get …)`. Values are in git history on two remotes — rotation is the only real fix.
2. **Promote**: the 15 top lost rulings → global CLAUDE.md rules 21–27 (≈12 lines; autonomy is one rule); 17 research gates → `researchgov/template/DISCIPLINE.md`; 5 infra guards → `infra/hosts/jangmojib.md`, `infra/network.md`, `infra/hosts/dublin-capture-vm.md`; 5 research findings → owning repo docs; harness-design files → `clanker/docs/`; the polymarket block (9 memories) → a new `polymarket/CLAUDE.md` + `docs/`; the 21 project-namespace items (B.3).
3. **Freeze**: `tar --zstd` by class to `/data/claude-archive/memory-2026-09-03/` with a manifest; originals stay (no deletion); the 18 stale feedback files + 4 duplicate clusters + 25 stub namespaces go on the approval list (§8 A11).
4. **Repoint**: rules 17/18a → `clanker/localci/CONVENTION.md` and `researchgov/README.md`; `.clanker-arcs.yaml` → repo docs or drop the injection and the `ensure_memory_stub` write; fix `check-compute.sh`, `closure-claim-verifier.sh`, `subagent-tier-gate.py`, `skills/wsl2-5070-devenv`, `infra/data/DATA_INVENTORY.md`, `infra/STATUS.md:9`, and the 12 repo docs (Appendix C).
5. **Repo**: `git rm -r --cached projects` in `~/.claude` (A10); autocommit covers harness files instead.
6. **Lint the ban**: `doctor` fails on `\.claude/projects/.*/memory/` citations while memory is off; gate `pattern-promoter-daily.sh`, `memory-lint.sh`, `gc` router-gen and `ensure_memory_stub` on the setting.
7. **If memory is ever re-enabled**: fix the lint path regex for the resolved `/data/...` path; repair the 181 orphans (`memory-lint.sh --doctor`); gate fableNQkronos behind a RETRACTIONS grep.

---

## 5. Skills

### 5.1 Global (`~/.claude/skills`, 9) — usage from `/data/clanker/raw/skills`

| Skill | All-time / 30 d | Verdict |
|---|---|---|
| pre-flight-verification | 299 / 4 | **Keep.** Best skill on the box; usage collapsed after July — worth a UserPromptSubmit nudge on design/plan prompts. |
| verify-operational-preconditions | 29 / 1 | **Keep and mechanise** into `doctor --live` (G1). |
| session-retrospective | 26 / 0 | Keep body; fix trigger economics (G9); step 5 still says "update feedback_*.md memory". |
| wsl2-5070-devenv | 38 / 17 | **Keep** (actively used); replace its 3 memory pointers with `infra/wsl2-5070/` sections. |
| integration-contracts | 0 tracked | Keep; strip four `superpowers:*` integrations. |
| session-plan-execution | 0 tracked | Research-specific → `eigenstateresearch/.claude/skills/` (2026-07-05 rule). |
| project-discipline | 1 | Cotton-derived; cites a repo that no longer exists; `MULTI_AXIS_AUDIT_TEMPLATE.md` "NOT YET WRITTEN" since April. Finish or archive. |
| clanker-briefing / clanker-review | 0 / 0 | Pure CLI wrappers (23/29 lines) → README; archive. |
| clanker-session | 14 / 3 | **Archive.** Instructs manual `clanker ingest`; hooks already record every session; produced 264 duplicate rows. |

`~/.agents/skills/` holds inert copies of already-archived skills; `~/.claude/agents/` is empty (no
custom subagent definitions, although archetypes imply them).

### 5.2 Project skills (63 across 11 repos)
19/63 lack a trigger phrase (all 10 vendored `speckit-*`, `toxicflow`, `kronos-pipeline-invariance`,
`gramdyne-infra-module-build`, `rust-best-practices`, `telegramstockdeployment`,
`harness-integration-guide`, `cad-loop`). `toxicflow` exists in two repos, diverged (198 vs 52 lines).
`polymarket` has 9 skills and hooks but no CLAUDE.md. Most-used all-time: cotton-module-build 154 (repo
gone), toxicflow 106, handoff-quality 104 (archived), rust-best-practices 99, optimization-audit 77.
Last 30 days total: 45 invocations.
**Fix:** a `doctor` check that every SKILL.md description contains a trigger clause; merge the two
`toxicflow` skills; `clanker adopt polymarket` to scaffold its contract.

---

## 6. Clanker (the management tool) — defects that matter for management
Ranked from the code review (B.6), all with `file:line`:
1. `lib/projects.py:113` — dict-valued alias stringified into a project name (**CRITICAL**, armed).
2. `hooks/clanker-dist/session-start.sh:81,87` + missing `~/.claude/hooks/lib/` — briefings dead, error hidden (**CRITICAL**).
3. `lib/alerts.py:317-318` — timestamp id + hard-coded `info` defeat dedup/escalation (HIGH).
4. `lib/analyze.py:220-221` — ranks on error-hours, ignores `estimated_cost_usd` (HIGH).
5. `~/.tmux-startup.sh:68` + `lib/tmux_manager.py:162-167` — ghost sessions respawn in `$HOME` (HIGH).
6. `lib/registry.py:87,96,118` — ignores `aliases`, never backfills archetype (MEDIUM).
7. Untracked gates (`hooks/harness/task-payload-gate.py`; `subagent-delivery-gate.py` nowhere) (MEDIUM).
8. `ci/publint.sh` red on `lib/cleanup.py:118`; will trip on untracked `lib/schedules.py:182` `roots=("/home/user",)` (MEDIUM).
9. Repo law 9 (no import-time env capture) violated in 16 modules; `memoryns.py:56` reads a file at import.
10. Seven wired-but-unused modules (wiki, decompose, crossproject, plugins, context, ecosystem, audit_configs) with no tests and no STATUS mention; `orch/` 2,234 LOC and `ecc/` 2,755 LOC parked.
`ci/fast.sh`: 515 passed in 58 s (green).

---

## 7. Recommendations

### 7.1 P0 — this week
1. **Unbreak the six silent failures** (G1 table): backup dir (or repoint to `/data/backups/`), weekly cron `%` (move into `tools/weekly.sh`), `sync --apply` decision (commit the untracked gates first), Databento key plumbing + non-zero exit on stale products, kronos orphan (A1), DR log stamp.
2. **Cron failure alerting** (`cronwrap` + `MAILTO=""` + stamp check in `alert check`).
3. **`clanker doctor --live`** daily (G1 list), under cronwrap.
4. **Rotate the nine credentials + the mail-spool password**; commit `~/.claude`; add both gates to the clanker manifest.
5. **Alert dedup + project-scoped banner + `--bare` for headless**; dismiss the two AWS-case alerts.
6. **Registry yaml**: `collab-stack` under `projects:`; `spec-kit` archetype; `projects.py:113` guard.
7. **Hook hazards** found by the sweep: `timeout: 8000 → 8` in `drone-cad` and `dronelinespec` (6 occurrences); add `timeout` to `modal_guard.py`; narrow ISD's `Bash|Grep|Read` matcher.

### 7.2 P1 — this month (structural)
1. **Destructive-command gate** (G3) — fail-closed, selftest, operator override token.
2. **Law consolidation** (G2): promote the lost rulings; `LAW-INDEX.md`; doctor checks for memory citations, `superpowers:*` refs, hook `timeout ≥ 1000`, hook symlink targets, SKILL.md triggers.
3. **Memory disposition** §4 (rotate → promote → freeze → repoint → lint).
4. **Governance dialect declaration** + fleet-wide context-budget gate (G5/G11).
5. **Fleet management** (G6): registry validation; boot map from registry + 30-day activity; `resurrect` skips missing dirs; RSS/disk in `doctor --live`; lazy launch.
6. **Version control of the harness** (G7): autocommit covers harness files; `CLAUDE_CONFIG_DIR` instead of the symlink; drop the no-op keys (`trustedWorkspaces`, adaptive-thinking env, duplicate effort key); document `autoCompactEnabled:false`.
7. **DR** (G12).
8. **Skills** (G10).
9. **Clanker code** (§6 items 1–9).
10. **yon crons** (Appendix C.4): after the Databento decision, disable the 9 CT117 duplicates, move the two infra-owned jobs out of the `yon-*` namespace, redirect `/tmp` logs, register `~/yon` as `infra` or migrate its live pieces into a real repo.

### 7.3 P2 — session quality
1. Session-start context diet (≤ 1 KB banner; measured).
2. STATUS contract fleet-wide (`## NOW` in the first 30 lines; journals to `docs/journal/`).
3. Retro economics (artifact-checked nudge; `propose --from-retro`).
4. Telemetry: headless tag; real-dollar column; exclude `outcome:empty & duration<60` from rankings.
5. `cleanupPeriodDays`: pick a real retention for session-env/file-history or accept unbounded growth explicitly.
6. Plugin hygiene (A9).

---

## 8. Approval-required items (never batched — rule 19)

| # | Action | Why | Reversible? |
|---|---|---|---|
| A1 | Stop orphan pids 428621/428625 (kronos dashboard) so the unit can bind | crash-loop since boot | yes |
| A2 | Restart tmux session `fableNQkronostransformerresearch` (11.4 GB claude, 35 d) | 44 % of claude RSS | yes (`--resume`) |
| A3 | Disable 9 duplicate yon crons (rename `.disabled-…`; comment crontab twins) | racing CT117 timers | yes |
| A4 | Rotate the 9 memory-file credentials + the PostgreSQL role in the mail spool; redact files; purge `/var/mail/user` | plaintext, in git on two remotes | rotation one-way; purge is cron noise |
| A5 | Remove plaintext key files in `$HOME`/`/data/clanker` after migrating consumers to `secret get` | secrets law | keep an encrypted copy first |
| A6 | Prune merged agent worktrees (cm 24, omnigentfork 2, hftlogger-rust 7, …) after `git branch --merged`; move venv/db/model blobs to `/data` with symlinks | 7 GB + ~10 GB on a 17 GB-free root | prune: branches survive if merged; moves: yes |
| A7 | Archive `Market-Master-…-wsl`, `devworkstation` to `/data/backups/projects-archive/` | flagged 5 July | yes |
| A8 | Retire the weekly analyze/propose cron line if the loop stays tabled | dead + noisy | yes |
| A9 | Uninstall project-scoped superpowers/playground (quanta-ai); drop the third-party marketplace | hygiene | yes |
| A10 | `git rm -r --cached projects` in `~/.claude` once the memory tarball exists | 472 phantom deletions | yes |
| A11 | Tar-freeze the memory corpus; list 18 stale feedback files + 4 duplicate clusters + 25 stub namespaces for later removal | §4 | tar: yes; removal: separate approval |
| A12 | Boot-map pruning: remove the 3 cottondashboard entries and collapse duplicates | G6 | yes (file is regenerable) |

---

## Appendix A — evidence
A.1 Selftests (00:10 UTC): stop-dispatch PASS; memory-lint 20/20; task-payload-gate 34/34; iron-law PASS; context-gauge 13/13; `test_pretooluse_dispatch.sh` PASS=25 FAIL=0; delivery-gate 9/9; resume-surface 24/24.
A.2 Hook tax (20-run means): dispatcher trivial 12 ms; git PostToolUse pair 17 ms; gauge wrapper 5 ms; Edit pair 13 ms; commit-path dispatcher 141 ms; session-start 153 ms.
A.3 Weekly cron: `crontab -l | sed -n '/analyze weekly/p' | cat -A` → `week-%s.md` unescaped; journal 2026-08-30 06:00:01 shows CMD truncated at `week-`; `sh -n` → `Syntax error: end of file unexpected (expecting ")")`; `clanker analyze weekly` by hand succeeds. Check after fix: new `reports/week-*.md` + `alerts/weekly-digest.json` on Sunday.
A.4 Backup: 112 mails from 2026-05-14 03:00, all `cannot create /home/user/backups/eigenstate-sqlite/sync.log: Directory nonexistent`. Check after fix: today's files in the target dir, no new mail.
A.5 Headless: newest transcript in `…/-home-user-projects-lyric-generator-runs-cache-claude-workdir/` line 4 = 5,989 bytes containing `CLANKER ALERTS (23 active)`; 3,588 transcripts dated 2026-09-01; `raw/sessions/2026-09-01.jsonl` 3,749 lyric rows; 3,900 rows / $782 over 14 d; `src/generate/backends/claude_cli.py:45` = `["claude", "-p", …]`. Check after fix: no `CLANKER ALERTS` in a new batch transcript.
A.6 Databento: sha256 prefixes — `~/.databento_api_key` `b15ff2c3…`, age `databento_api_key` `08f89778…`, age `…OLD_delinquent` `b88dd96e…`; fallback literals at `yon/scripts/backfill_gex_history.py:38` and `yon/live/daemon/backfill_dom_history.py:23`; crontab exports no `DATABENTO_API_KEY`; 182 `402` lines in `fut_l2l3_nightly.log`. Check after fix: `grep -c 402` stops growing; newest `options_snapshots/` file is today.
A.7 Fleet: 58 claude processes / 23.9 GB (72 / 24.7 GB minutes later); pid 1118521 11.46 GB, 35 d 19 h, tmux `fableNQkronostransformerresearch`; `free -g` 88 / 24 used / 11 swap; load 14.2.
A.8 Registry poison: `projects.resolve_project('/home/user/projects/collab-stack')` → the stringified dict (live run); 0 poisoned telemetry rows so far. Check after fix: returns `collab-stack`.
A.9 Sync: `clanker sync --check` → 27/70, 3 drifted (session-start/end, agent-resume-surface), 40 `lib/*.py` not-installed. Check after apply: 70/70 and a briefing block on `clanker work clanker`.
A.10 Kronos dashboard: `systemctl --user status kronos-dashboard.service` → `activating (auto-restart) … status=1/FAILURE`; `ss -ltnp | grep 8643` → pids 428621/428625 (not the unit). Check after fix: `active (running)`, MainPID owns the socket.

## Appendix B — subagent reports (condensed; full text in the session transcript)
B.1 **mem-feedback** (132 files): COVERED-GLOBAL 7 · COVERED-REPO 9 · COVERED-HOOK 11 · LOST-GENERAL 62 · LOST-PROJECT 21 · STALE 18 · DUPLICATE 4 clusters; top-25 lost rulings with destinations (autonomy cluster first); duplicates: never-delete ×2, scope ×2, pre-commit ×2, subagent tier ×3; one partial key fragment; `plan-drift-check.sh` and `yon/.claude/hooks/framework_first_guard.py` exist but are wired to nothing.
B.2 **mem-corpus** (155 files): class table; 30-row durable-reference table (top five absent from `infra/`); 9 live-credential files + 4 partial; 18 dead-project files; 5 unhomed research findings; frontmatter complete; 63 non-feedback orphans, 20 durable.
B.3 **mem-projects**: eigenstateresearch 7/4/24/0 (24 orphaned by the polymarket split); fableNQkronos 11/3/5/**8 contradicted** (D11 lifted 08-19; STATUS archived; "speed is cleanliness 17×" and climate-champion figures banned in RETRACTIONS; read-order and model-split rulings changed 08-13; two pre-archive-tag docs quarantined); eigenstate ~30/8/8/2 (Razorpay live key in `eigenstate.md`); cm 2/1/0/0; drone-cad 0/1/1/0; infra 1/0/0/0 (already copied to gramdyne-infra); 21 promotion candidates (the polymarket bots-VM identity survived into infra; its deploy/teardown procedure did not); 25 stub namespaces.
B.4 **cron-liveness** (41 entries): LIVE-CONSUMED 9 · LIVE-UNCONSUMED 1 · LIVE-NO-LOG 3 · BROKEN 7 · DISABLED 2 · ZOMBIE 10. yon: keep capture-audit + laptop-replica (infra-owned symlinks mis-filed under `yon-*`) + toxicflow shadow_deployed; BROKEN refresh_regime_orb_v1 (parquet touched daily, content frozen since 06-16) and yon-ci-nightly (fails nightly, pages infra); ZOMBIE ×9 (same-minute ISD twin on CT117, root-owned outputs, `PermissionError`). Non-yon: eigenstateresearch collector BROKEN-but-green; eigenstate backup BROKEN; clanker health/gc live, DR log empty; kronos watchdog blind; cm nightly-full + sns-case-watch no log; trade-relay backup OK; collab smoke RED `thin pool 95%`; collab weekly tag partial (`secret: not found` — cron PATH lacks `~/bin`); lyric release_watch OK; yonmusic trend_monitor BROKEN (`claude -p` failing 2 weeks); gramdyne smokes first run pending; `polymarket-bot-weather-sync` (root, hourly) BROKEN — SSH timeout to 54.228.124.62, 648 KB log. Nine jobs log to `/tmp`; five have no log.
B.5 **contracts-sweep** (61 roots): 12 stale references; 5 rule contradictions (Assisted-by trailer ×5 vs rule 16; ISD autoCompact; eigenstateresearch `git add -A`; Estudio `python -c` allowlist; eigenstate deploy protocol lacks the consent clause); 6 hook hazards (8000 s timeouts ×6; ISD Read/Grep gate; breadcrumb on every call ×5; `modal_guard` no timeout); sizes (cm STATUS 2,555; eigenstate CLAUDE 246; quanta-ai 218); 27 roots with no contract; 19 skills without triggers; duplicated law (rule 16 ×16 files, rule 17 ×11, author identity ×8, explicit-`git add` ×7); worktrees 7.1 GB. One correction applied here: `ScheduleWakeup` *is* a current tool, so fableNQkronos's `wakeup_text_guard` matcher is valid.
B.6 **clanker-review**: items (a)–(j) with `file:line` (see §6); `ci/fast.sh` 515 green; `publint` red.
B.7 **cc-guide** (code.claude.com): effort env beats settings, `max` valid, adaptive-thinking env no-op on Fable 5.1; `claude -p` fires SessionStart/SessionEnd, skip via `--bare` or `--settings '{"disableAllHooks":true}'`; hook timeout in seconds, default 600 (30 on UserPromptSubmit), fail-open; `if` valid on 5 tool events; `cleanupPeriodDays` covers projects/, tool-results, file-history, session-env, tasks, shell-snapshots, backups, plans, debug, paste-cache (auto-memory excluded); `autoMemoryEnabled` and the env kill-switch both documented, disable = neither read nor written; `trustedWorkspaces` undocumented, trust in `~/.claude.json`, hooks load regardless; full-window behaviour with auto-compact off undocumented; projects symlink unsupported (`CLAUDE_CONFIG_DIR`); harness backgrounds every subagent by default, result arrives as a completion notification.

## Appendix C — project-level drift the harness should have caught (evidence for §3)
C.1 **Docs that cite dead things**: `eigenstateresearch/CLAUDE.md:72-92` (deleted `check-tasks.sh`; hooks "in ~/.claude/settings.json" moved to the repo; `branded-pdf-guard` never fires there; memory pdf guide); `improvedstockdashboard/CLAUDE.md:25` (claims a global hook its own settings removed); `polymarkethftinfrastructure{,-f48}/CLAUDE.md:18-40`, `hftbacktester/CLAUDE.md:113-115` + 8 worktree copies (`superpowers:*`, memory as authority); `yon/scripts/toxicflow/{CLAUDE,STATUS}.md`; `dronelinespec/STATE.md:17`; `infra/hosts/{jangmojib:28,dublin-capture-vm:9,yoni-laptop:5,7,macmini:4}.md` and `infra/data/DATA_INVENTORY.md` (memory pointers).
C.2 **Contradictions with global law**: `Assisted-by: claude-code:claude-fable-5` in 5 repos' `attribution.commit` (collab-stack P2-F1, deliberate 31 Aug) vs rule 16 — decide and record; `eigenstateresearch/CLAUDE.md:22` `git add -A` vs its own `research/CLAUDE.md:22`; `Estudio/.claude/settings.local.json` `python -c` allowlist vs rule 12; `eigenstate/CLAUDE.md:146` deploy protocol without a consent clause.
C.3 **Shape**: cm STATUS 2,555 lines (`## NOW` at 771); eigenstate CLAUDE 246; quanta-ai 218; 27 roots without CLAUDE.md or state; 16 with CLAUDE.md but no state; seeded STATUS stubs (eigenstate, eigenstateresearch) unrefined since 5 July; git hygiene (eigenstateresearch 56 dirty on `claude/max-effort-*`, trading-bot 34 dirty + 4 unpushed, infra 15 unpushed, gramdyne-infra 10, polymarkethftinfrastructure-f48 14).
C.4 **Scheduled jobs outside the harness**: 12 yon jobs (9 duplicate CT117 twins, 2 broken, 3 keep — two of them infra-owned symlinks mis-filed as `yon-*`); root cron `polymarket-bot-weather-sync` timing out hourly; yonmusic `trend_monitor` failing; collab weekly tag `secret: not found`; eigenstateresearch collector green-but-stale.
C.5 **Data governance**: the Databento chain (account state → hard-coded old key → collector exits 0 → capture audit RED into an unread log).
