#!/usr/bin/env bash
# FULL suite — runs detached on post-commit. Full tests + a secret scan (gitleaks)
# if it's installed. Failures here notify; they don't block (the gate is fast.sh).
set -uo pipefail
# Runs detached from the post-commit hook, where git has exported GIT_DIR /
# GIT_INDEX_FILE / … — which would hijack the git calls the orch worktree tests
# make (relative GIT_DIR resolves to this repo's .git). Scrub for a clean env;
# rev-parse below still works because we're inside the repo's working tree.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_OBJECT_DIRECTORY GIT_COMMON_DIR GIT_NAMESPACE
cd "$(git rev-parse --show-toplevel)"
rc=0; fail_steps=""
fail() { rc=1; fail_steps="$fail_steps $1"; }
echo "[ci/full] tests…";    python3 -m pytest tests/ -q || fail tests
if command -v gitleaks >/dev/null 2>&1; then
  # git mode: scans committed content + history (what actually ships). A
  # filesystem scan would flag gitignored __pycache__/*.pyc, where the synthetic
  # test fixtures compile with real newlines the source-shaped allowlist misses.
  # Acknowledged HISTORICAL findings are fingerprint-ignored in .gitleaksignore.
  echo "[ci/full] gitleaks…"; gitleaks detect --source . --config .gitleaks.toml || fail gitleaks
else
  echo "[ci/full] gitleaks not installed — skipping secret scan"
fi
echo "[ci/full] publint…";  bash ci/publint.sh || fail publint
# Live-harness hook selftests: these scripts gate every session on this box —
# a broken edit must fail CI loudly, not wait to be noticed at 2am.
for st in "$HOME/.claude/hooks/memory-lint.sh" "$HOME/.claude/hooks/context-gauge.sh"; do
  if [ -f "$st" ]; then
    echo "[ci/full] selftest $(basename "$st")…"
    bash "$st" --selftest || fail "selftest:$(basename "$st")"
  fi
done
# Auto-publish the public mirror on a green build of main (item 7). Best-effort:
# the mirror is downstream of CI, gated by publish's own content checks — a
# publish failure is logged but must NOT flip rc (CI's gate is fast.sh, not this).
# PUBLISH_SKIP=1 opts a manual full-run out.
# ci/full.sh is itself published, so the MIRROR runs this file too. Only the
# SOURCE repo may publish: a mirror that auto-publishes re-drives the exporter
# from downstream (observed 2026-08-15 — the public repo's green run kicked off
# its own `clanker publish --push`). .publish-manifest.json exists only in the
# export, so it is the marker for "this tree IS the mirror".
if [ -f .publish-manifest.json ]; then
  echo "[ci/full] mirror repo — auto-publish suppressed"
elif [ "$rc" -eq 0 ] && [ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] \
   && command -v clanker >/dev/null 2>&1 && [ -z "${PUBLISH_SKIP:-}" ]; then
  echo "[ci/full] auto-publish…"
  clanker publish --push >> "${CLANKER_DATA:-/data/clanker}/reports/publish-auto.log" 2>&1 \
    || echo "[ci/full] auto-publish FAILED (see log)"
fi
# Surface the outcome on the clanker alert sink — a detached run has no other
# voice. The 4e6744e historical-leak episode kept every ci/full from 07-19 to
# 07-22 red with nobody noticing, and auto-publish silently gated off the whole
# time: red now drops a warning alert, green clears it.
# The alert id is PER-REPO. Both the source repo and its published mirror run
# this script against the SAME sink, so a single shared id let the mirror's
# green run delete the source's red alert — the red gate lost its voice exactly
# as in the 07-19..07-22 episode this block was written to prevent (observed
# 2026-08-15: public 03486ae green cleared clanker's publint-red alert).
ALERTS="${CLANKER_DATA:-/data/clanker}/alerts"
REPO_NAME="$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")"
if [ -d "${CLANKER_DATA:-/data/clanker}" ]; then
  if [ "$rc" -eq 0 ]; then
    rm -f "$ALERTS/ci-full-red-$REPO_NAME.json"
    # Retire the pre-2026-08-15 shared-id alert so an old one can't sit forever.
    [ "$REPO_NAME" = "clanker" ] && rm -f "$ALERTS/ci-full-red.json"
  else
    mkdir -p "$ALERTS"
    printf '{"severity":"warning","project":"%s","message":"%s ci/full RED at %s — failing:%s (auto-publish blocked). Log: .git/ci/run-%s.log"}\n' \
      "$REPO_NAME" "$REPO_NAME" "$(git rev-parse --short HEAD)" "$fail_steps" \
      "$(git rev-parse HEAD)" > "$ALERTS/ci-full-red-$REPO_NAME.json"
  fi
fi
[ "$rc" -eq 0 ] && echo "[ci/full] all green" || echo "[ci/full] FAILURES (rc=$rc — failing:$fail_steps)"
exit "$rc"
