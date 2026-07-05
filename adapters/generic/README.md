# Generic Adapter

Wraps any command with session tracking. No agent integration needed.

## Usage

```bash
# Wrap any command
clanker wrap -- make test
clanker wrap -- cursor .
clanker wrap -- vim file.py
clanker wrap -- ./deploy.sh

# What happens:
# 1. Snapshots git state (branch, last commit)
# 2. Calls clanker ingest --event session_start
# 3. Runs your command
# 4. On exit: captures duration, exit code, git diff
# 5. Calls clanker ingest --event session_end
```

## Data captured

- Duration (wall clock time)
- Exit code
- Git diff stats (insertions, deletions, files changed)
- Outcome (commit if new commit detected, abandoned if exit != 0)
