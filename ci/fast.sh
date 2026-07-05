#!/usr/bin/env bash
# FAST gate — runs on pre-push (must stay green + under ~60s). The hermetic test
# suites cover the subsystems with no external dependencies.
set -euo pipefail
# Some tests shell out to git (the orch worktree suite runs `git worktree add`
# in its own temp repos). When CI runs from a git hook, git has exported
# GIT_DIR / GIT_INDEX_FILE / … into the environment — a relative GIT_DIR then
# resolves to THIS repo's .git and hijacks those child git calls. Scrub them so
# every test starts in a clean git environment.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_OBJECT_DIRECTORY GIT_COMMON_DIR GIT_NAMESPACE
cd "$(git rev-parse --show-toplevel)"
echo "[ci/fast] hermetic test suites…"
python3 -m pytest tests/ -q
echo "[ci/fast] all green"
