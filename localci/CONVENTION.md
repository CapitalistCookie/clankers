# Local CI/CD convention (box-wide, CT112) — localci

Generalized from yon's local pipeline (2026-06-10). Two thin **dispatcher** git
hooks live here and are propagated to every repo; each repo supplies its own CI
scripts. No GitHub Actions, no network — everything runs on this box.

```
~/.git-templates/hooks/pre-push      # FAST gate: blocks `git push` on failure
~/.git-templates/hooks/post-commit   # FULL spawn: detached, never blocks commits
~/bin/localci-install                # installer for existing repos (idempotent)
```

## How a repo's CI config is resolved (at hook time, in this order)

1. **`.localci` at the repo root** — authoritative if present (no fallback):
   ```
   FAST=live/ci/ci_fast.sh      # repo-relative path, run by pre-push
   FULL=live/ci/ci_full.sh      # repo-relative path, spawned by post-commit
   ```
   Either line may be omitted (e.g. FAST-only repos).
2. **`ci/fast.sh` + `ci/full.sh`** — the generic convention (executable files).
3. **`live/ci/ci_fast.sh` + `live/ci/ci_full.sh`** — the yon layout.
4. **Nothing found → the hooks exit 0 silently.** Safe to install everywhere.

## What the dispatchers do

- **pre-push**: runs `pre-push.local` first if present (a pre-existing gate that
  the installer chained aside — its failure aborts the push, and it receives the
  real pre-push stdin/ref-lines). Then runs FAST from the repo root; non-zero
  blocks the push. Bypass the FAST gate once with `SKIP_CI=1 git push`
  (SKIP_CI never bypasses `.local` — pre-existing gates keep their own bypass).
- **post-commit**: runs `post-commit.local` first if present (cannot abort).
  Then spawns FULL **detached** (`setsid nohup`) so the commit returns
  instantly. Single-flight + collapse-to-latest via flock on
  `.git/ci/dispatch.lock` + `.git/ci/dispatch.pending`: commits made during an
  active run queue; only the newest sha re-runs. Log: `.git/ci/run-<sha>.log`.
  Skip once with `SKIP_CI=1 git commit`.
  NOTE: the dispatcher lock is `dispatch.lock`, NOT `.git/ci/lock`, so a FULL
  script with its own internal flock (yon's `ci_full.sh`) keeps working.
- Telegram alerts, state lines, history — all belong to the repo's own FULL
  script (see yon's `live/ci/ci_full.sh` for the reference implementation).
  The dispatcher stays generic.

## How a NEW project opts in

1. `git init` / `git clone` — the hooks arrive automatically via
   `git config --global init.templateDir ~/.git-templates` (already set).
2. Drop an executable `ci/fast.sh` (target <60s, hard-cap thinking: 120s) that
   exits non-zero on failure. Optionally `ci/full.sh` and/or a `.localci`.
3. Done. Pushes now gate on FAST; commits spawn FULL detached.

For an EXISTING repo (cloned before the templateDir was set):
`localci-install <repo-path>` or `localci-install --all`.

## Installer safety rules (localci-install)

- Pre-existing non-dispatcher hooks are moved to `<name>.local` and chained
  (verified with `bash -n` only — never executed by the installer).
- A repo whose existing pre-push contains `git commit` gets NOTHING installed
  (a committing pre-push is branch-corrupting). `polymarkethftinfrastructure`
  is additionally hard-denylisted (its pre-push-time tests commit sandbox
  fixtures — memory: polymarket_repo_prepush_gotchas).
- Repos with `core.hooksPath` outside `.git/hooks` (e.g. a tracked `.githooks/`
  dir: `polymarket`, `polymarket-protocol`) are skipped for manual review —
  installing would dirty the working tree.
- Linked worktrees are skipped (hooks are shared with the main checkout).
- Idempotent: dispatcher hooks carry `managed-by: localci dispatcher` and are
  refreshed in place. To update every repo after editing a template:
  `localci-install --all`.

## Per-repo state (never committed)

`.git/ci/` — `run-<sha>.log` (detached FULL output), `dispatch.lock`,
`dispatch.pending`, plus whatever the repo's own scripts keep there
(yon: `last_fast`, `last_full`, `history.log`, `lock`, `pending`).

## Caveats

- Suites run against the **working tree** at hook time, not the pushed sha.
- `.localci` and `ci/fast.sh` are working-tree files: commit them to the repo
  so they survive clones (hooks themselves never need committing).
- pre-push only fires when there are refs to push ("Everything up-to-date"
  pushes don't run it).
- This README is copied into new repos' `.git/` by `git init` (harmless —
  self-documenting).
