#!/usr/bin/env python3
"""
Context gauge (PostToolUse hook). Reads the REAL token usage Claude Code logs to the session
transcript and injects a MEASURED remaining-context percentage back to the model — so the model
stops *guessing* its budget (the 2026-06-14 failure: it fabricated "~1% remaining" while it had
~35% left, and stopped early against an explicit "keep building" goal).

How: the API returns `usage` on every assistant turn; Claude Code writes it to transcript_path
(JSONL). We read the tail, find the latest usage, sum input + cache_creation + cache_read +
output = the current context occupancy, and compare to the model window.

POST-/COMPACT STALENESS FIX (2026-06-14, second failure): right after a /compact the latest usage
line in the transcript is the PRE-compaction value (huge) — the freshly-reduced context's usage is
not logged until the next turn completes, and this PostToolUse hook fires mid-turn. Reporting that
stale value told the model "~28% remaining, stop" when it actually had ~88%. Fix: scan newest→oldest;
if a compaction boundary (`isCompactSummary` / `compact_file_reference`) is newer than any usage line,
the latest usage is stale → emit a "re-syncs next turn" note instead of a wrong number.

Output: a hookSpecificOutput.additionalContext line — THROTTLED so it's quiet when context is
plentiful and vocal as it approaches the 30% line (the common `/goal "...<30% context"` threshold).

Pure stdlib; never throws out (a gauge must never block a tool). Tail-read keeps it O(1) on big
transcripts. An internal failure is swallowed for the session and logged to the hook-error log.
"""
# ---- hook-error log: the same block in every clanker python hook ----------------
# A failure that the hook swallows (the hook stays fail-open for the session)
# appends one JSON line {ts, hook, session_id, cwd, rc, stderr_tail} to
# $CLANKER_DATA/raw/health/hook-errors-<UTC day>.jsonl (default /data/clanker),
# where `clanker doctor --harness` counts it. hook_err never raises, never
# blocks and never writes to stdout. Usage: hook_err(rc, step, error text or
# exception); stderr_tail is "<step>: " plus the end of the text (of the
# traceback, for an exception), 300 characters at most. Set
# HOOK_ERR["session_id"] and HOOK_ERR["cwd"] once the payload is parsed; a
# script read from stdin has no __file__ and sets HOOK_ERR["hook"] as well.
import os as _he_os
import sys as _he_sys

HOOK_ERR = {"hook": _he_os.path.basename(globals().get("__file__") or "") or "?",
            "session_id": "", "cwd": ""}


def hook_err(rc, step, err=""):
    try:
        import json
        import time
        import traceback
        if isinstance(err, BaseException):
            err = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        step, err = str(step), str(err or "").strip()
        room = 298 - len(step)
        msg = (step + ": " + err[-room:]) if err and room > 0 else step
        try:
            cwd = HOOK_ERR.get("cwd") or _he_os.getcwd()
        except OSError:
            cwd = ""
        rc = int(rc) if str(rc).lstrip("-").isdigit() else 1
        now = time.gmtime()
        d = _he_os.path.join(_he_os.environ.get("CLANKER_DATA") or "/data/clanker",
                             "raw", "health")
        _he_os.makedirs(d, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", now),
               "hook": str(HOOK_ERR.get("hook") or "?"),
               "session_id": str(HOOK_ERR.get("session_id") or ""), "cwd": str(cwd),
               "rc": rc, "stderr_tail": msg[:300]}
        path = _he_os.path.join(d, "hook-errors-" + time.strftime("%Y-%m-%d", now) + ".jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass
# ---- end of hook-error log --------------------------------------------------------

import json
import os
import sys

# Model context windows (tokens). Default 200k; the [1m] opus/sonnet tiers are 1M.
WINDOWS = {
    "1m": 1_000_000,
    "opus-4-8": 1_000_000,   # opus-4-8[1m]
    "fable": 1_000_000,      # claude-fable-5 (Mythos-class) — 1M window; operator-confirmed 2026-07-02
                             # (gauge said 2% left at 197k used while the real window had ~80% free)
    "default": 200_000,
}
TAIL_BYTES = 262_144  # read only the last 256 KiB to find the latest usage (fast on huge transcripts)
SETTINGS = os.path.expanduser("~/.claude/settings.json")
_configured_window = None


def window_for(model: str) -> int:
    m = (model or "").lower()
    if "[1m]" in m or "1m" in m:
        return WINDOWS["1m"]
    if "opus-4-8" in m:
        return WINDOWS["opus-4-8"]
    if "fable" in m:
        return WINDOWS["fable"]
    return WINDOWS["default"]


def configured_window() -> int:
    """Window implied by the SESSION's configured model (settings.json `model`).

    FALLBACK-SHRINK BUG (2026-07-24, operator-caught): when the session
    temporarily falls back to another model (e.g. opus), the transcript's
    per-message `model` is that fallback's id — which matches none of the 1M
    keys above, so window_for() returned the 200k default and the gauge
    reported "~2% remaining" to a session that really had ~80% free. It spent a
    whole investigation nagging the model to wrap up early. The configured
    model is the session's real ceiling, so it floors the observed value below.
    Missing/unreadable settings -> 0, i.e. no floor, old behavior."""
    global _configured_window
    if _configured_window is None:
        _configured_window = 0
        try:
            with open(SETTINGS) as f:
                m = (json.load(f) or {}).get("model") or ""
            if m:
                w = window_for(m)
                # Only an explicit 1M signal floors the window — a bare model id
                # resolving to the 200k default must not masquerade as evidence.
                _configured_window = w if w > WINDOWS["default"] else 0
        except FileNotFoundError:
            _configured_window = 0
        except Exception as e:
            hook_err(1, "read the configured model from settings.json", e)
            _configured_window = 0
    return _configured_window


def resolve_window(model: str) -> int:
    """Effective window: never let a transient model fallback shrink it below
    what this session is configured for."""
    return max(window_for(model), configured_window())


def latest_usage(path):
    """Return (used_tokens, model, stale). stale=True ⇒ a /compact is newer than any usage line, so
    the latest usage is the pre-compaction value and must NOT be reported as the current context."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            chunk = f.read()
    except OSError:
        return None, "", False
    model = ""
    for raw in reversed(chunk.splitlines()):  # newest → oldest
        # A compaction boundary encountered BEFORE any usage line means the context was just reset
        # and no fresh usage has been logged yet → the next usage we'd find is stale (pre-compaction).
        if b'"isCompactSummary":true' in raw or b'"compact_file_reference"' in raw:
            return None, model, True
        if b'"usage"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        msg = obj.get("message") or {}
        u = msg.get("usage") or obj.get("usage")
        if not u:
            continue
        model = msg.get("model") or obj.get("model") or ""
        used = (
            (u.get("input_tokens") or 0)
            + (u.get("cache_creation_input_tokens") or 0)
            + (u.get("cache_read_input_tokens") or 0)
            + (u.get("output_tokens") or 0)
        )
        return (used if used > 0 else None), model, False
    return None, model, False


def _key_for(agent_id, tp):
    """Cache/marker key — MUST match context-gauge.sh's derivation exactly."""
    import re as _re
    if agent_id:
        return "ag-" + _re.sub(r"[^a-zA-Z0-9_-]", "_", str(agent_id))
    base = os.path.basename(tp or "x")
    return "tp-" + (base[:-6] if base.endswith(".jsonl") else base)


def _claim_once(path):
    """Atomically create `path`. True ONLY for the one caller that created it.

    RACE FIX (2026-09-24): the old exists()-then-write() pair let several
    PostToolUse hooks from one batch of parallel tool calls all see "no marker"
    and all emit. O_CREAT|O_EXCL is atomic on the local fs, so exactly one wins.
    Any error (marker exists, /tmp unwritable) -> False: a missed grounding line
    is harmless, a repeated one reads to the model like a fresh instruction."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return False
    try:
        os.write(fd, b"1")
    finally:
        os.close(fd)
    return True


def _write_cache(key, tp, limit, pct):
    """Fast-path cache consumed by context-gauge.sh: 'size window pct' + resolved
    transcript path on line 2. Only written after a SUCCESSFUL fresh measurement —
    stale/unmeasurable paths skip it so the wrapper keeps sending us the call."""
    try:
        size = os.path.getsize(tp)
        with open(f"/tmp/cc-ctxgauge-fast-{key}", "w") as f:
            f.write(f"{size} {limit} {int(pct)}\n{tp}\n")
    except Exception as e:
        hook_err(1, "write the fast-path cache", e)


def main() -> None:
    probe = os.environ.get("CCG_PROBE")   # selftest instrumentation only
    if probe:
        try:
            open(probe, "w").write("1")
        except Exception:
            pass
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        hook_err(1, "parse hook input", e)
        return
    if not isinstance(data, dict):
        hook_err(1, "parse hook input", "payload is not a JSON object")
        return
    HOOK_ERR.update(session_id=str(data.get("session_id") or ""), cwd=str(data.get("cwd") or ""))
    tp = data.get("transcript_path")
    # ── SUBAGENT FIX (2026-06-22): the harness hands a subagent the PARENT's transcript_path AND the
    # parent's session_id, so reading transcript_path reports the PARENT's context % to the subagent.
    # That made every research subagent wrongly scope down / defer / hand off early once the parent
    # session had aged (MEASURED: subagents whose OWN window was 52k–167k/1M = 83–95% FREE were told
    # "~28% remaining, wrap up" — e.g. the GEX-regime run deferred its full sweep). Subagent payloads
    # carry an `agent_id` key (the parent's payload does NOT); the subagent's OWN transcript lives at
    # …/tasks/<agent_id>.output. Resolve it so the gauge reflects the subagent's real (fresh) window. ──
    agent_id = data.get("agent_id")
    if agent_id:
        agent_key = str(agent_id).replace("/", "_")
        # Perf: this hook fires on EVERY tool call; cache the resolved transcript
        # path per agent so the recursive /tmp glob runs once, not per call.
        tp_cache = f"/tmp/cc-ctxgauge-tp-{agent_key}"
        cands = []
        try:
            with open(tp_cache) as f:
                cached = f.read().strip()
            if cached and os.path.exists(cached):
                cands = [cached]
        except Exception:
            pass
        if not cands:
            import glob
            cands = [c for c in glob.glob(f"/tmp/claude-*/**/tasks/{agent_id}.output", recursive=True)
                     if os.path.exists(c)]
        if not cands and tp:
            # Current layout (verified 2026-07-05): subagent transcripts live in the
            # parent transcript's SIDECAR dir — <dir>/<parent-sid>/subagents/agent-*.jsonl.
            import glob
            side = os.path.join(os.path.dirname(tp),
                                os.path.basename(tp).removesuffix(".jsonl"), "subagents")
            cands = [c for c in glob.glob(os.path.join(side, f"agent-*{agent_key}*.jsonl"))
                     if os.path.exists(c)] or \
                    [c for c in glob.glob(os.path.join(side, "agent-*.jsonl")) if os.path.exists(c)]
        if cands:
            try:
                tp = max(cands, key=os.path.getmtime)   # the subagent's own fresh transcript
            except (OSError, ValueError):
                tp = cands[0]
            try:
                with open(tp_cache, "w") as f:
                    f.write(tp)
            except Exception as e:
                hook_err(1, "write the subagent transcript cache", e)
        else:
            # Can't locate own transcript → FAIL SAFE: never push a subagent to wrap up on the parent's %.
            marker = f"/tmp/cc-ctxgauge-sub-{agent_key}"
            if _claim_once(marker):
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": (
                    "CONTEXT GAUGE: you are a SUBAGENT with your OWN fresh ~1M-token window, independent of the "
                    "parent. Your true usage isn't measurable here, so IGNORE any parent-derived % — do NOT scope "
                    "down, defer, or hand off early; complete the FULL task.")}}))
            return
    if not tp or not os.path.exists(tp):
        return
    used, model, stale = latest_usage(tp)
    if stale:
        # Post-/compact window: say so explicitly so a low stale reading is never mistaken for "stop now".
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": (
            "CONTEXT GAUGE: a /compact just occurred — the context window was freshly REDUCED by the "
            "summary, but the new turn's token usage is not logged yet, so any low reading THIS turn is "
            "the STALE pre-compaction value. Do NOT treat it as real and do NOT stop on it; the gauge "
            "re-syncs to the true (much higher) remaining % on the next tool call."
        )}}))
        return
    if not used:
        return
    limit = resolve_window(model or data.get("model") or "opus-4-8[1m]")
    remaining_pct = max(0.0, (limit - used) / limit * 100.0)

    used_k = used / 1000.0

    # ── ONE-TIME GROUNDING (root fix for the subagent budget-FABRICATION failure, 2026-06-14) ──
    # A fresh agent — ESPECIALLY a subagent — never hears the gauge while context is plentiful (it only
    # speaks <=65% remaining), so it GUESSES its budget and self-aborts. MEASURED instance: subagent
    # a5a520aa peaked at 53k/1M = 94.7% FREE yet handed off claiming "~5% remaining" and skipped the
    # compute. Fix: emit ONE explicit grounding line on the first tool call of each transcript (every
    # subagent has its own transcript_path → grounds exactly once), forbidding guessing/early-abort.
    # Key from the PARSED top-level agent_id / transcript_path (2026-09-24). The
    # wrapper's CCG_KEY is ignored: its old substring match picked up NESTED
    # agent_id values (an Agent tool_response naming the spawned teammate), so
    # every Agent spawn minted a fresh key and the parent session re-received
    # its "first reading" once per spawned agent.
    key = _key_for(agent_id, tp)
    grounded_marker = f"/tmp/cc-ctxgauge-grounded-{key}"
    if _claim_once(grounded_marker):
        gnote = (
            f"CONTEXT GAUGE (first reading, window {limit // 1000}k): ~{remaining_pct:.0f}% free "
            f"({used_k:.0f}k used), measured from your own transcript. Use it; never guess your budget or stop early."
        )
        gnote += " Below 30%: finish one bounded step, then wrap up." if remaining_pct < 30 else " Re-speaks below 32%."
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": gnote}}))
        _write_cache(key, tp, limit, remaining_pct)
        return

    # throttle: emit on a 5%-bucket change once <=65% remaining (so the descent is visible from early
    # on, not just near the cliff), and ALWAYS when <32% (the common stop threshold).
    session = str(agent_id or data.get("session_id") or "x").replace("/", "_")
    state = f"/tmp/cc-ctxgauge-{session}.last"
    bucket = int(remaining_pct // 5) * 5
    prev = None
    try:
        with open(state) as f:
            prev = int(f.read().strip())
    except Exception:
        prev = None
    try:
        with open(state, "w") as f:
            f.write(str(bucket))
    except Exception as e:
        hook_err(1, "write the throttle state", e)
    near = remaining_pct < 32
    if not (near or (remaining_pct <= 65 and bucket != prev)):
        _write_cache(key, tp, limit, remaining_pct)
        return

    note = (
        f"CONTEXT GAUGE (measured from transcript token usage; window {limit // 1000}k): "
        f"~{remaining_pct:.0f}% of the context window REMAINING ({used_k:.0f}k used). "
        f"This is a MEASURED number — do NOT estimate your own context; use this gauge."
    )
    if remaining_pct < 30:
        note += (
            " You are now BELOW 30% remaining: if a '<30% context' goal/stop-condition is active it is "
            "satisfied — finish the current committed increment and wrap up gracefully (do not run to the wall)."
        )
    elif remaining_pct < 40:
        note += " Approaching the 30% line — keep building, but start pacing toward a clean committed stopping point."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}))
    _write_cache(key, tp, limit, remaining_pct)


try:
    main()
except Exception as e:
    # A gauge must NEVER break a tool call — swallow internal errors (fail-open),
    # but log them.
    hook_err(1, "main", e)
