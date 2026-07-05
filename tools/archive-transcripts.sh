#!/usr/bin/env bash
# archive-transcripts (harness overhaul M6, 2026-07-05) — move Claude session
# transcripts older than N days (default 60) from ~/.claude/projects (rootfs)
# to /data/claude-archive, leaving symlinks so `claude --resume`, the resume
# queue, and retro/transcript reads keep working at the original paths.
# Idempotent; skips symlinks (already archived). Run monthly via cron.
set -euo pipefail

DAYS="${1:-60}"
SRC="$HOME/.claude/projects"
DST="/data/claude-archive"
moved=0
bytes=0

while IFS= read -r -d '' f; do
    rel="${f#"$SRC"/}"
    mkdir -p "$DST/$(dirname "$rel")"
    sz=$(stat -c %s "$f")
    mv "$f" "$DST/$rel"
    ln -s "$DST/$rel" "$f"
    moved=$((moved + 1))
    bytes=$((bytes + sz))
done < <(find "$SRC" -name '*.jsonl' -type f -mtime +"$DAYS" -print0 2>/dev/null)

echo "[archive-transcripts] moved $moved transcript(s), $((bytes / 1024 / 1024)) MB -> $DST (symlinks left in place; threshold ${DAYS}d)"
