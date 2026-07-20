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
rc=0
echo "[ci/full] tests…";    python3 -m pytest tests/ -q || rc=1
if command -v gitleaks >/dev/null 2>&1; then
  # git mode: scans committed content + history (what actually ships). A
  # filesystem scan would flag gitignored __pycache__/*.pyc, where the synthetic
  # test fixtures compile with real newlines the source-shaped allowlist misses.
  echo "[ci/full] gitleaks…"; gitleaks detect --source . --config .gitleaks.toml || rc=1
else
  echo "[ci/full] gitleaks not installed — skipping secret scan"
fi
echo "[ci/full] publint…";  bash ci/publint.sh || rc=1
# Live-harness hook selftests: these scripts gate every session on this box —
# a broken edit must fail CI loudly, not wait to be noticed at 2am.
for st in "$HOME/.claude/hooks/memory-lint.sh" "$HOME/.claude/hooks/context-gauge.sh"; do
  if [ -f "$st" ]; then
    echo "[ci/full] selftest $(basename "$st")…"
    bash "$st" --selftest || rc=1
  fi
done
# Auto-publish the public mirror on a green build of main (item 7). Best-effort:
# the mirror is downstream of CI, gated by publish's own content checks — a
# publish failure is logged but must NOT flip rc (CI's gate is fast.sh, not this).
# PUBLISH_SKIP=1 opts a manual full-run out.
if [ "$rc" -eq 0 ] && [ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] \
   && command -v clanker >/dev/null 2>&1 && [ -z "${PUBLISH_SKIP:-}" ]; then
  echo "[ci/full] auto-publish…"
  clanker publish --push >> /data/clanker/reports/publish-auto.log 2>&1 \
    || echo "[ci/full] auto-publish FAILED (see log)"
fi
[ "$rc" -eq 0 ] && echo "[ci/full] all green" || echo "[ci/full] FAILURES (rc=$rc)"
exit "$rc"
