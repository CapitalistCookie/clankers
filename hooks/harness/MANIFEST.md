# hooks/harness — vendored generic harness hooks (MANIFEST)

Distribution source for the GENERIC Claude Code harness hooks. A separate
`clanker sync` tool reads this manifest and installs these files into
`~/.claude/hooks/` on any operator's box. The installed copies are managed —
edit here, never there (every vendored file carries a `# distribution source:`
header saying so).

These files were copied VERBATIM from a reference install's `~/.claude/hooks/`
and then de-personalized to the minimum the public-readiness lint (`ci/publint.sh`)
requires: literal operator home paths → `$HOME` or derived; hardcoded LAN IPs /
hostnames → env vars with a fail-open skip when unset (the generic copy is INERT,
not pointed at someone's LAN); personal handles removed. Behavior — logic, exit
codes, fail-open/fail-closed discipline — is preserved. Project *names*
(eigenstate, spacetime, polymarket, cotton, …) are NOT publint-forbidden and are
left verbatim where removing them would change behavior; such cases are flagged
below.

> Literals in this manifest are masked (`/home/<user>`, `-home-<user>`,
> `192.168.x.x`, `<org>`) so the file itself stays clean under the publint grep.

**Scope note:** the reference install's repo-run hooks (session-start/end,
prompt-check, skill-tracker, status-stale-nudge, agent-resume-surface,
subagent-resume-detect, subagent-tier-gate, context-gauge.py) live in
`hooks/` and were out of scope for this vendoring — they are listed as
`repo-run` below for completeness.

---

## Counts

- **Vendored:** 19 files (18 hooks/docs + 1 test) → `hooks/harness/`
- **Excluded (operator/project-specific):** 18 files
- **Repo-run (already in `hooks/`, not re-vendored):** 1 file
- Build artifact ignored: `__pycache__/`

## Verification (this vendoring)

- publint grep — the four token classes `ci/publint.sh` forbids (operator home
  path, namespace slug, LAN `/16`, operator ids) over `hooks/harness/` → **zero
  hits** (verified against both the task grep and the assembled `ci/publint.sh`
  pattern). `ci/publint.sh` itself scans only `*.py *.sh *.js`; this manifest and
  `README.md` are docs (exempt there) and were masked to stay clean under the
  broader `grep -r` too.
- `bash -n` clean on all 15 shell files; `py_compile` clean on all 3 python files.
- Selftests (run on the vendored copies):
  - `memory-lint.sh --selftest` → `memory-lint selftest: 20/20 PASS` (rc 0)
  - `iron-law-check.sh --selftest` → `iron-law selftest: RED->2, GREEN->0, NATIVE-RED->2, CITATION->0, ONCE-PER-SESSION 2then0 — PASS` (rc 0)
  - `context-gauge.sh --selftest` → `context-gauge selftest: 13/13 PASS` (rc 0) — run with `context-gauge.py` placed as a sibling (see Dependencies).
- Parity spot-checks: gpu-vm-guard INERT with empty `GPU_HOST` (does not fire on every command) yet the `gpu-train` advisory path is preserved; check-git-target INERT unconfigured but still DENYs a configured repo with unpushed research files; deploy-gate delete-data hard-block still fires, preflight inert when `CLANKER_DEPLOY_PREFLIGHT` unset; dispatcher parses and is silent on a no-match command.

---

## VENDORED — full inventory (`~/.claude/hooks/<file>` → `hooks/harness/<file>`)

Every vendored file also received, right after the shebang:
`# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)`
(README.md received the same note as a blockquote under its title.) Those header
lines are omitted from the per-file diffs below. Diffs mask the operator literals.

| file | de-personalization (old → new) | selftest |
|---|---|---|
| `README.md` | none (hook-contract cheat-sheet; publint-clean). Added distribution-source blockquote. | — |
| `pretooluse-bash-dispatch.sh` | `H="<home>/.claude/hooks"` → `H="$HOME/.claude/hooks"` · `GPU_HOST="192.168.x.x"` → `GPU_HOST="${GPU_HOST:-}"` (+comment; empty default → gate-5 host-match inert until configured) | see gate table |
| `iron-law-check.sh` | none — verbatim. (The `192.168`-shaped token on the deploy-claim line is a *detection regex*, not a LAN literal; publint-clean.) | **`--selftest` → PASS** |
| `last-assistant-msg.py` | none — verbatim. (Shared dependency of iron-law + scope-calibration.) | — |
| `retro-prompt.sh` | none — verbatim. | — |
| `scope-calibration.sh` | none — verbatim. | — |
| `governance-gates-autorun.sh` | none — verbatim. (Generic `<name>_spec/` research-governance autorun.) | — |
| `memory-lint.sh` | `--doctor` default `…/-home-<user>/memory` → `…/$(printf '%s' "$HOME" \| tr '/' '-')/memory` (derives the namespace slug from `$HOME`) · selftest bash-guard fixtures `<home>/.claude/projects/<ns>/memory/…` → `$HOME/.claude/projects/<ns>/memory/…` (placeholder home; the guard is regex-based so the exercise is identical) +clarifying comment | **`--selftest` → 20/20 PASS** |
| `context-gauge.sh` | `PY="<home>/.claude/hooks/context-gauge.py"` → `PY="$(dirname "$0")/context-gauge.py"` (points at installed sibling) | **`--selftest` → 13/13 PASS** |
| `tests/test_context_gauge.sh` | `H=<home>/.claude/hooks` → `H="$(cd "$(dirname "$0")/.." && pwd)"` (relocatable; parent of `tests/`) | (is the harness for the above) |
| `closure-claim-verifier.sh` | comment `~/.claude/projects/-home-<user>/memory/…` → `~/.claude/projects/<namespace>/memory/…` | — |
| `pattern-promoter-daily.sh` | none — verbatim. (Depends on external `~/.claude/scripts/pattern-promoter.sh` — see Dependencies.) | — |
| `post-build-review-reminder.sh` | none — verbatim. | — |
| `check-git-target.sh` | `RESEARCH_REPO` default `<org>/eigenstateresearch` → `""` · hardcoded allow-check `grep -qE 'eigenstateresearch'` → env-driven `[ -n "$RESEARCH_REPO" ] && grep -qF "$RESEARCH_REPO"` · repo-list default `<home>/projects/eigenstate` → `REPOS="${CHECK_GIT_TARGET_REPOS:-}"; [ -z "$REPOS" ] && exit 0` (inert until configured) · block message `Push to $RESEARCH_REPO` → `Push to ${RESEARCH_REPO:-the research repo}` | — |
| `backfill-safety.sh` | none — verbatim. (`candles_1s` in advisory text is a generic example; publint-clean.) | — |
| `gpu-vm-guard.py` | docstring `CT 200 (192.168.x.x)` → `CT 200 (host from research.env GPU_HOST)` · `GPU_HOST` default `192.168.x.x` → `""` · activation guard `if GPU_HOST not in cmd …` → `if (not GPU_HOST or GPU_HOST not in cmd) …` (empty host must NOT match every command) · advisory `ssh root@192.168.x.x …` → f-string `ssh {GPU_USER}@{GPU_HOST} …` | — |
| `ssh-tunnel-port-guard.py` | none — verbatim. (Docstring domain already a placeholder `genericnondescriptwebsite.com`; references clanker's own `lib/serve.py`, appropriate.) | — |
| `deploy-gate.sh` | `PREFLIGHT="<home>/projects/eigenstate/scripts/deploy-preflight.sh"` → `PREFLIGHT="${CLANKER_DEPLOY_PREFLIGHT:-}"` (+comment; empty → existing `[ -f ]` guard skips the preflight block) | — |
| `pre-commit-verification.sh` | comments `<org>/eigenstateresearch` and `<org>/hftbacktester.git` — personal handle genericized to `<org>`. ⚠ retains **inert** legacy operator-repo scope-checks (polymarket/cotton/hftbacktester) verbatim — see Flags. | — |

---

## EXCLUDED — operator/project-specific (do NOT distribute)

| file | reason |
|---|---|
| `check-compute.sh` | Generic compute-routing kernel is buried under **irreducible operator content**: a trading strategy's P&L anchor values, private laptop/container hostnames, Windows `C:/Dev/…` dev paths, Frigate/MPS infra. Cannot be made generic by publint-literal substitution without deleting logic (would violate preserve-behavior). Author a fresh generic compute-router if this capability is wanted. |
| `databento-native-completeness-guard.py` | Databento OPRA/GLBX futures-options-research completeness nudge. Operator research-specific. |
| `pwb-post-resolution-real-sim.sh` | **symlink** → `~/polymarket_research/clanker_hooks/…`. Polymarket weather-bot. |
| `pwb-pre-commit-bot-change-needs-test.sh` | **symlink** → `~/polymarket_research/clanker_hooks/…`. Polymarket. (dispatcher gate 8) |
| `pwb-pre-deploy-suite-green.sh` | **symlink** → `~/polymarket_research/clanker_hooks/…`. Polymarket. (dispatcher gate 10) |
| `pwb-sessionend-compile.sh` | **symlink** → `~/polymarket_research/clanker_hooks/…`. Polymarket. |
| `pwb-risk-surface-review-required.sh` | Polymarket weather-bot risk-surface review gate. (dispatcher gate 9) |
| `cottondashboard-drift-check.sh` | cottondashboard project SessionStart drift-check. |
| `cottondashboard-sync-ship-candidates.sh` | cottondashboard project SessionStart mirror of eigenstateresearch SHIP_CANDIDATES. |
| `data-flow-map-check.sh` | eigenstate `stdb-module/src/domains/**/tables.ts` DATA_FLOW_MAP enforcement (SpacetimeDB). |
| `reducer-design-check.sh` | eigenstate `stdb-module` reducer/lifecycle design rules (SpacetimeDB). |
| `research-optimization-check.sh` | eigenstateresearch `research/…` script vectorization/parallelization nudge. |
| `research-rule-guards.sh` | Research Rules 8/9/10 (walk-forward leakage / PCA / Hopfield) on `research/` commits. |
| `research-rule9-ast.py` | AST Rule-9 checker; dependency of `research-rule-guards.sh`. |
| `post-commit-oi-scan.sh` | Project-specific OI-ledger (`OI-NNN`) auto-close scanner. |
| `post-deploy-screenshot.sh` | eigenstate deploy (`deploy-vm.sh`/`spacetime publish`) production screenshotter. |
| `branded-pdf-guard.sh` | Enforces Eigenstate PDF branding; blocks generic PDF generators. |
| `tests/test_pretooluse_dispatch.sh` | Dispatcher **parity harness** coupled to the operator's full 11-gate install (references the pwb-*/databento/check-compute gates, an operator home path, and LAN-IP fixtures). Not generic; dispatcher parity is validated in the live install, not the distributed copy. |

## REPO-RUN — already in `hooks/` (not re-vendored)

| file | note |
|---|---|
| `context-gauge.py` | Byte-identical to `hooks/context-gauge.py` (verified). The measurement/emission engine behind the `context-gauge.sh` fast-path wrapper. Synced via the repo-run set; must co-locate with `context-gauge.sh` in `~/.claude/hooks/`. |

---

## Dispatcher gate-wiring reconciliation (for the sync tool)

`pretooluse-bash-dispatch.sh` invokes 12 gates by sibling path (`$H/<gate>`), in
settings order. It **fail-opens** on a missing gate (a spawn that exits non-0/2 is
skipped), so distributing only the vendored subset is SAFE — excluded gates simply
never run. The excluded gate *names* remain in the vendored dispatcher's
comments/labels/prefilters (project names, not publint-forbidden). The sync tool
MAY prune the excluded gates' prefilter blocks for cleanliness; not required for
correctness.

| gate # | invoked file | distributed? |
|---|---|---|
| 1 | `databento-native-completeness-guard.py` | ❌ excluded |
| 2 | `check-git-target.sh` | ✅ vendored |
| 3 | `check-compute.sh` | ❌ excluded |
| 4 | `deploy-gate.sh` | ✅ vendored |
| 5 | `gpu-vm-guard.py` | ✅ vendored |
| 6 | `backfill-safety.sh` | ✅ vendored |
| 7 | `pre-commit-verification.sh` | ✅ vendored |
| 8 | `pwb-pre-commit-bot-change-needs-test.sh` | ❌ excluded (symlink) |
| 9 | `pwb-risk-surface-review-required.sh` | ❌ excluded |
| 10 | `pwb-pre-deploy-suite-green.sh` | ❌ excluded (symlink) |
| 11 | `ssh-tunnel-port-guard.py` | ✅ vendored |
| 12 | `memory-lint.sh --bash-guard` | ✅ vendored |

## Dependencies & install notes

- **`context-gauge.sh` → `context-gauge.py`** (repo-run) must co-locate in
  `~/.claude/hooks/`. The wrapper resolves it via `$(dirname "$0")/context-gauge.py`.
  `tests/test_context_gauge.sh` needs both present as siblings of the hooks dir.
  (For this vendoring's selftest, `context-gauge.py` was placed as a sibling only
  during the run and then removed — it is not part of the vendored set.)
- **`iron-law-check.sh`, `scope-calibration.sh` → `last-assistant-msg.py`**
  (vendored) — sibling in the hooks dir.
- **`pattern-promoter-daily.sh` → `~/.claude/scripts/pattern-promoter.sh`** — an
  external CLI script, NOT a hook and NOT vendored here. The hook no-ops cleanly if
  it is absent (`bash "$…/pattern-promoter.sh" … || true`); the sync tool/operator
  should provide it separately if the daily promotion ritual is wanted.
- **Config surfaces the de-personalized gates read** (all optional; unset ⇒ inert):
  `~/.claude/research.env` (`GPU_HOST`, `GPU_USER`, `GPU_SSH_KEY`), env
  `CHECK_GIT_TARGET_REPOS`, `RESEARCH_REPO`, `CLANKER_DEPLOY_PREFLIGHT`.
- The vendored files are the DISTRIBUTION SOURCE; wiring them into an operator's
  `~/.claude/settings.json` (matchers, timeouts) is the sync tool's job and is not
  part of this manifest.

## Flags (judgment calls worth a second look)

- **`pre-commit-verification.sh`** is a genuinely generic research-commit
  verification gate whose *generic* trigger is the `~/projects/.clanker.yaml`
  `archetype=research` lookup, but it still carries **legacy operator-repo
  scope-checks** (polymarket_*/cottondashboard/hftbacktester grep patterns +
  comments). These are inert for any operator without those repos and removing them
  would change behavior, so per "de-personalize only what publint forbids" they were
  kept verbatim (only the personal handle was genericized). If a cleaner generic
  copy is wanted, those scope-check blocks can be pruned — the clanker.yaml
  archetype path already covers the generic case.
- **`check-compute.sh` was EXCLUDED despite being a listed dispatcher gate** — its
  content is irreducibly operator-specific (a strategy's P&L anchors, private
  infra) and cannot be sanitized by literal substitution alone. Flagged so the drop
  is a conscious decision, not a silent omission.
