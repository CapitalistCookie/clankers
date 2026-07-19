# Reader mode — the mobile session view (2026-07-19)

## Why a different renderer

A terminal application lays out its own UI at the width it was told
(`TIOCGWINSZ`). One tmux window cannot be two widths at once, so a 107-column
Claude Code frame reaching a ~45-column phone can only be wrapped (tables
shred), clipped (panning), or shrunk (unreadable). No client-side renderer can
fix that — only the app redrawing could, and the desktop owns the width.

Reader mode sidesteps the physics: the transcript JSONL is the ground truth of
the conversation, and its *source* (markdown, tables, code) reflows natively as
HTML. The terminal remains one tap away for TUI moments (permission dialogs are
also pushed via ntfy with the question inline, so those rarely require it).

## Architecture

```
tmux session ──► lib/reader.py resolve_transcript()
                   1) wyc last_snapshot.json  (exact tmux_session → session id)
                   2) fallback: pane cwd → git root → namespace slug → newest
                      *.jsonl (agent-*.jsonl excluded)
              ──► parse_tail(path, offset)   incremental, 512KB-bounded,
                                             partial-record + rotation safe
GET /api/reader/<session>?offset=N  ──►  {units, offset, reset}
lib/web/reader.js  ──►  feed of units: user bubbles, assistant markdown
                        (marked + DOMPurify, vendored), tool chips, error marks
input: the existing /ws/view socket + compose bar (atomic send-keys)
```

## Invariants (bulletproof/future-proof)

1. **wyc enriches, never gates** — resolution works transcript-direct when the
   watcher is down; the snapshot only upgrades accuracy (sibling sessions in
   one repo namespace).
2. **Bounded everything** — 512KB tail window server-side, ~200 DOM nodes
   client-side, 3s poll paused while the tab is hidden.
3. **Fail-open parsing** — malformed lines skipped; a transcript rewrite
   (rotation) resets cleanly via the `reset` flag.
4. **Sanitized rendering** — assistant markdown goes through DOMPurify;
   user text is text-node only. Transcript content is treated as untrusted.
5. **Input stays on the proven path** — reader adds NO new write path; the
   compose bar and quick keys ride the same /ws/view protocol as view mode.
6. **Renderer choice is per-device, persisted** — Reader is the phone default;
   Terminal (capture view) one tap away; desktop keeps the PTY terminal.
