# The self-improving loop (ratified 2026-07-05, harness overhaul)

The system improves itself only when the loop CLOSES — capture without ratification is a
diary, ratification without mechanical application is advice. Six stages, each mapped to a
live component; the invariant is at the bottom.

## 1. CAPTURE (automatic, already live)
- `hooks/session-end.sh` → session metrics (/data/clanker/raw/)
- `hooks/skill-tracker.sh` → per-skill usage counts (what earns its context)
- `retro-prompt` Stop hook → `session-retrospective` skill on substantial sessions
- clanker alerts (cron q15m) + repo `STATUS.md` files (BLOCKED sections = friction log)
- feedback memories (`~/.claude/.../memory/feedback-*.md`, `process-feedback-ledger.md`)

## 2. SYNTHESIZE (automatic, weekly)
- Sunday 06:00 cron: `clanker analyze weekly` + `clanker propose` → /data/clanker/reports/week-*.md
- `pattern-promoter-daily.sh` (SessionEnd) promotes recurring patterns

## 3. SURFACE (the stage that was missing — added 2026-07-05)
- Weekly digest now drops an ALERT (`/data/clanker/alerts/weekly-digest.json`) so the report
  lands in EVERY session start until reviewed — reports no longer rot unread.
- `clanker doctor` drift-summary injects into the project briefing at session start
  (briefing.py §6): a repo that falls out of contract nags until converged.
- Pending proposals already show in the briefing (§4).

## 4. RATIFY (operator-in-the-loop, cheap)
- `clanker review` in any meta session — accept / reject / defer each proposal.
- Zero-risk mechanical classes (typo-grade hook fixes, dead-path removal) may be auto-accepted
  by a session that IMMEDIATELY implements + tests them; anything touching gate SEMANTICS or
  operator conventions requires explicit operator acceptance.

## 5. APPLY (mechanical, versioned, reversible)
- An accepted proposal becomes a COMMIT in clanker or ~/.claude (both are git repos with dual
  private remotes) WITH its regression test (clanker ci/fast = 353 tests; hooks get dummy-input
  runs; memory-lint has PASS+FAIL cases).
- Prose rules that recur become gates: **any lesson that appears twice (retro, feedback memory,
  STATUS BLOCKED) MUST graduate to a hook/CI gate/doctor check** — this is the recurrence→gate
  law. The 101-lesson `process-feedback-ledger.md` is the standing backlog to mine.

## 6. VERIFY + MEASURE (did it work?)
- ci/fast green before push (353 tests); hook-tax timing script (scratchpad harness, ~171ms/Bash
  baseline 2026-07-05) re-run after hook changes; skill-usage stats decide what stays global;
  doctor pass-rate across the registry = the system-health number.

## Archetype matrix (referenced by lib/adopt.py `_archetype_checks`)

The six loop stages are UNIVERSAL; what differs per project class is **what counts as a
lesson, who ratifies, and which gates the lessons graduate into**:

| | research (strategy-research programs) | infra (infra, comms, macmini) | development (production/frontend/tool repos) |
|---|---|---|---|
| capture | prereg ledger + deflation entries; guard trips; certification anchors | incident notes in STATUS BLOCKED; capture-freshness/backup audits | test failures, review findings, retro-prompt retros, user corrections |
| failure class guarded against | **false positives** (leakage, optimization theater, false attribution) AND false negatives (wrong-sign gate silently killing edge) | **drift** (prod≠git, stale backups, dead crons, config rot) | **regressions** (red suites, broken deploys, contract drift) |
| mechanical gates lessons graduate into | researchgov `<name>_spec/` fail-closed gates; research-rule-guards hooks; tape-source law | doctor checks (backup story tracked), freshness crons + alerts, drift checks | ci/fast.sh pre-push (green <60s) + ci/full.sh detached; per-repo hooks |
| ratification | prereg BEFORE test (rule 18a — no test without an entry); operator certifies; reproduce-before-certify | operator for anything touching prod (rule 1); zero-risk fixes auto-apply | operator for gate semantics; typo-grade fixes auto-apply with tests |
| doctor rows (adopt.py) | `_spec/` dir + prereg.yml present | backup story named in STATUS.md | ci/full.sh present (deployables) |

One law is shared verbatim by all three: **recurrence→gate** — any lesson that appears
twice must graduate from prose into a hook / CI gate / doctor check.

## Invariant
Every enforcement is mechanical (hook / CI / doctor), self-tested, versioned, and pushed.
A rule that lives only in prose is a draft. A gate that was never seen RED is not a gate.
Rollback path: `git -C ~/.claude log` / `git -C ~/projects/clanker log`.
