"""Reader mode data source — parse a session transcript's tail into message units.

The mobile flagship (2026-07-19): reading a Claude session on a phone via
terminal frames is physics-limited (an app lays out its own UI at the width it
was told; a 107-col table cannot reflow to 45 cols). The transcript JSONL is
the ground truth, so the reader renders THAT as real HTML. This module is the
server side: tmux session -> transcript path -> incremental tail parse.

Design constraints (bulletproof/future-proof):
- No wyc dependency: resolution walks tmux pane cwd -> Claude Code's namespace
  slug dir -> newest *.jsonl. watchyourclankers enriches, never gates.
- Incremental: callers pass (path, offset); we read only appended bytes.
  Rotation/rewrite safety: if the file shrank or changed identity, restart
  from the tail window.
- Fail-open parsing: unknown/malformed lines are skipped, never fatal.
- Bounded: at most TAIL_BYTES are ever read in one call.
"""

import json
import os
import re
import shutil
import subprocess

TAIL_BYTES = 512 * 1024
MAX_UNITS = 60

# Resolve git once at import so each subprocess call skips a PATH lookup (S8).
GIT = shutil.which("git") or "/usr/bin/git"


def _slug(path):
    return re.sub(r"[/.]", "-", path)


def pane_cwd(tmux_session):
    try:
        out = subprocess.check_output(
            ["tmux", "display-message", "-p", "-t", tmux_session,
             "#{pane_current_path}"],
            stderr=subprocess.DEVNULL, text=True, timeout=3).strip()
        return out or None
    except Exception:
        return None


WYC_SNAPSHOT = "/data/clanker/watchyourclankers/last_snapshot.json"


def _wyc_transcript(tmux_session):
    """Exact mapping via the wyc watcher snapshot (tmux_session -> session id).

    Multiple sessions can share one repo namespace, so newest-mtime alone can
    pick a SIBLING session's transcript — wyc knows precisely which session
    lives in which tmux session. Enrichment only: any failure returns None."""
    try:
        with open(WYC_SNAPSHOT) as f:
            snap = json.load(f)
        best = None
        for s in snap.get("sessions", []):
            if s.get("tmux_session") != tmux_session:
                continue
            if best is None or (s.get("updated_at") or 0) > (best.get("updated_at") or 0):
                best = s
        if not best:
            return None
        sid, cwd = best.get("id"), best.get("cwd")
        if not sid or not cwd:
            return None
        for base in (cwd,):
            p = os.path.join(os.path.expanduser("~/.claude/projects"),
                             _slug(base), f"{sid}.jsonl")
            if os.path.exists(p):
                return p
    except Exception:
        pass
    return None


def resolve_transcript(tmux_session):
    """tmux session -> transcript jsonl. wyc-exact first, mtime-heuristic fallback."""
    p = _wyc_transcript(tmux_session)
    if p:
        return p
    cwd = pane_cwd(tmux_session)
    if not cwd:
        return None
    # Sessions launch at the git root (cd-prefix law) but a cd during work
    # moves pane_current_path — prefer the git root's namespace when inside a repo.
    try:
        root = subprocess.check_output(
            [GIT, "-C", cwd, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, text=True, timeout=3).strip() or cwd
    except Exception:
        root = cwd
    for base in (root, cwd):
        ns = os.path.join(os.path.expanduser("~/.claude/projects"), _slug(base))
        if not os.path.isdir(ns):
            continue
        best, best_m = None, 0
        try:
            for fn in os.listdir(ns):
                # agent-*.jsonl are SUBAGENT transcripts — never the session's own
                if not fn.endswith(".jsonl") or fn.startswith("agent-"):
                    continue
                p = os.path.join(ns, fn)
                m = os.path.getmtime(p)
                if m > best_m:
                    best, best_m = p, m
        except OSError:
            continue
        if best:
            return best
    return None


def _tool_detail(block):
    inp = block.get("input") or {}
    for key in ("command", "file_path", "description", "prompt", "pattern",
                "query", "url", "skill"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().splitlines()[0][:100]
    return ""


def _parse_line(raw):
    """One transcript line -> unit dict or None."""
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    t = d.get("type")
    msg = d.get("message") or {}
    ts = d.get("timestamp") or ""
    if t == "assistant":
        content = msg.get("content") or []
        texts, tools = [], []
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    texts.append(b["text"])
                elif b.get("type") == "tool_use":
                    tools.append({"name": b.get("name", "?"),
                                  "detail": _tool_detail(b)})
        if not texts and not tools:
            return None
        return {"role": "assistant", "ts": ts,
                "text": "\n\n".join(texts), "tools": tools}
    if t == "user":
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # tool_result carriers are not operator prompts — surface errors only
            errs = [b for b in content if isinstance(b, dict)
                    and b.get("type") == "tool_result" and b.get("is_error")]
            if errs:
                return {"role": "result", "ts": ts,
                        "text": "", "errors": len(errs), "tools": []}
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(x for x in texts if x)
        else:
            text = ""
        text = (text or "").strip()
        if not text or text.startswith("<") and "system-reminder" in text[:60]:
            return None
        # command invocations render as XML-ish wrappers — show the command line
        m = re.search(r"<command-name>([^<]+)</command-name>", text)
        if m:
            text = m.group(1)
        return {"role": "user", "ts": ts, "text": text[:4000], "tools": []}
    return None


def parse_tail(path, offset=None):
    """Read appended content from `path` starting at byte `offset`.

    Returns {units, offset, start, size, reset} — reset=True when the caller's
    offset was unusable (rotation/rewrite/first call) and the tail window was
    used. `start` = byte offset of the first COMPLETE line in the window (the
    cursor for backward pagination via parse_window)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"units": [], "offset": 0, "start": 0, "size": 0, "reset": True}
    reset = False
    if offset is None or offset > size:
        reset = True
        offset = max(0, size - TAIL_BYTES)
    with open(path, "rb") as f:
        f.seek(offset)
        blob = f.read(TAIL_BYTES if reset else min(size - offset, TAIL_BYTES))
    new_offset = offset + len(blob)
    text = blob.decode("utf-8", errors="replace")
    lines = text.split("\n")
    start = offset
    if reset and offset > 0 and lines:
        # first line is a partial record; the real window starts after it
        start = offset + len(lines[0].encode("utf-8", errors="replace")) + 1
        lines = lines[1:]
    if lines and not text.endswith("\n"):
        # last record still being written — don't consume it
        new_offset -= len(lines[-1].encode("utf-8", errors="replace"))
        lines = lines[:-1]
    units = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        u = _parse_line(line)
        if u:
            units.append(u)
    if len(units) > MAX_UNITS:
        units = units[-MAX_UNITS:]
    return {"units": units, "offset": new_offset, "start": start,
            "size": size, "reset": reset}


def parse_window(path, before):
    """Backward pagination: parse the window of complete records ENDING at
    byte `before` (which must be a line start — the `start` of a previously
    returned window). Returns {units, start, at_start} where `start` is this
    window's first-complete-line offset and at_start=True at byte 0.

    Full-session scrollback (operator, 2026-07-19): the tail window shows the
    recent conversation; scrolling to the top pages back window by window to
    the session's very first message."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"units": [], "start": 0, "at_start": True}
    before = max(0, min(before, size))
    if before == 0:
        return {"units": [], "start": 0, "at_start": True}
    chunk_start = max(0, before - TAIL_BYTES)
    with open(path, "rb") as f:
        f.seek(chunk_start)
        blob = f.read(before - chunk_start)
    text = blob.decode("utf-8", errors="replace")
    lines = text.split("\n")
    start = chunk_start
    if chunk_start > 0 and lines:
        start = chunk_start + len(lines[0].encode("utf-8", errors="replace")) + 1
        lines = lines[1:]
    units = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        u = _parse_line(line)
        if u:
            units.append(u)
    return {"units": units, "start": start, "at_start": chunk_start == 0}
