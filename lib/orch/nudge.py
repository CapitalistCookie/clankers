"""Risk-gated auto-continue of routine waiting sessions (MVP-2 — the autonomy payoff).

When an orchestrated session is sitting in the `waiting` state (a coding agent has
paused for a permission / continue / "should I proceed?" prompt), this module decides
whether it is SAFE to auto-nudge it forward ("use your best judgment, proceed") or
whether it MUST be surfaced to the human.

The whole feature is gated behind `control.auto_nudge` (default False) and a risk
ceiling `control.nudge_risk_max`: we never auto-nudge if the pending action's risk
meets or exceeds that ceiling. Everything fails SAFE — any ambiguity or parse error
resolves to "do not nudge, surface to human".

The pending-danger check reuses the ECC classifiers verbatim: we scrape the most
recent shell command line out of the pane / last-assistant text and run it through
`guard.classify_command` + `guard.detect_governance` + `risk.score_tool_call`. If any
of those flags it destructive or scores it at `confirm`/`block`, the wait is a
"destructive_confirm" and is never nudgeable.

Public API:
    classify_wait(pane_text, last_assistant_text="") -> dict
    nudge_text() -> str
    _risk_rank(action) -> int
    should_nudge(session, pane_text, config=None, last_assistant_text="", now=None)
        -> (bool, str)
"""
import re
from datetime import datetime, timezone

from ecc import risk, guard

# Risk-action ladder severity, mirroring ecc.risk._ACTION_RANK. Higher == more dangerous.
_RISK_RANK = {"allow": 0, "review": 1, "confirm": 2, "block": 3}

# The message we send to auto-continue a routine waiting session.
_NUDGE_MESSAGE = "Use your best judgment and continue."

# A shell command is detected as the most recent line that either starts with a
# prompt sigil ($ / # / >>> / a fenced code marker) or that, after stripping a
# leading prompt, looks like a command invocation. We keep this deliberately
# conservative: the goal is to FIND a candidate to hand to the ECC classifiers,
# which then do the real (quote-aware, subshell-aware) destructiveness analysis.
_PROMPT_SIGILS = ("$", "#", "❯", ">")

# Lines we never treat as a candidate shell command.
_CODE_FENCE_RE = re.compile(r"^\s*```")

# Decision-fork heuristics: a numbered option list, or an explicit ask-the-human phrase.
_NUMBERED_OPTION_RE = re.compile(r"^\s*\(?\d+\s*[\.\)]\s+\S")  # "1) foo" / "2. bar" / "(3) baz"
_DECISION_PHRASES = (
    "which option",
    "which approach",
    "which one",
    "do you want me to",
    "would you like me to",
    "should i proceed with",
)
# "should I X or Y" — an either/or fork (kept separate so we can require the "or").
_SHOULD_X_OR_Y_RE = re.compile(r"should i\b.*\bor\b", re.IGNORECASE | re.DOTALL)

# Error / traceback heuristics — an unresolved failure awaiting a human.
_ERROR_MARKERS = (
    "traceback (most recent call last)",
    "error:",
    "fatal:",
    "exception:",
    "panic:",
    "segmentation fault",
)
# "failed" is noisier, so we require it to look like a real failure line.
_FAILED_RE = re.compile(r"\b(?:command failed|build failed|test[s]? failed|"
                        r"\d+ failed|failed with|failed:|❌)\b", re.IGNORECASE)

# Routine continue / permission prompts — the only nudgeable shape.
_ROUTINE_PHRASES = (
    "continue?",
    "proceed?",
    "(y/n)",
    "[y/n]",
    "(yes/no)",
    "press enter",
    "press return",
    "hit enter",
    "do you want to continue",
    "do you want to proceed",
    "ok to continue",
    "shall i continue",
    "shall i proceed",
)
# A bare interactive prompt sitting at the end of the pane with nothing pending.
_BARE_PROMPT_RE = re.compile(r"(?:^|\n)\s*[❯>$#]\s*$")


def _risk_rank(action):
    """Severity rank of a risk action: allow=0, review=1, confirm=2, block=3.

    Unknown / missing actions are treated as the most dangerous (block-level) so
    they can never slip under a nudge ceiling. This is the fail-safe direction.
    """
    return _RISK_RANK.get((action or "").strip().lower(), _RISK_RANK["block"])


def nudge_text():
    """The message to send to auto-continue a routine waiting session."""
    return _NUDGE_MESSAGE


def _last_nonblank_lines(text, limit=40):
    """The last `limit` non-blank lines of `text`, oldest→newest order."""
    if not text:
        return []
    lines = [ln.rstrip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    return lines[-limit:]


def _strip_prompt_sigil(line):
    """Strip a single leading shell-prompt sigil ($, #, ❯, >, >>>) + following space.

    Returns the remaining command text, or "" if the line is just a bare sigil.
    Only a real prompt sigil is stripped — a line like `rm -rf x` is returned as-is.
    """
    s = line.strip()
    # Python REPL continuation/prompt.
    for sig in (">>>", "...", "❯❯"):
        if s.startswith(sig):
            return s[len(sig):].strip()
    if s and s[0] in _PROMPT_SIGILS:
        return s[1:].strip()
    return s


def _looks_like_command(text):
    """Cheap test that `text` resembles a shell command invocation.

    First token is a bare word (a command name / path), optionally with flags or
    arguments. Avoids treating prose ("Should I deploy this?") as a command.
    """
    if not text:
        return False
    first = text.split()[0]
    # A command token is a path or a plain executable name (no spaces, not prose
    # punctuation). Allow letters/digits/_/-/./ and a leading path.
    return bool(re.match(r"^[\w./-]+$", first))


def _extract_last_command(*texts):
    """Find the most recent candidate shell command across the given texts.

    Scans each text's tail newest-line-first and returns the first line that
    looks like a command — preferring lines that carry an explicit prompt sigil
    (`$ rm -rf /`) but also accepting a bare command line inside a code block.
    Returns "" if nothing command-like is found (caller then can't classify it
    as destructive, which is the safe default).
    """
    # Pass 1: explicit `$ ...` / `# ...` / `❯ ...` prompted command lines (highest signal).
    for text in texts:
        for line in reversed(_last_nonblank_lines(text)):
            if _CODE_FENCE_RE.match(line):
                continue
            stripped = line.strip()
            if stripped and stripped[0] in _PROMPT_SIGILS:
                cmd = _strip_prompt_sigil(line)
                if cmd and _looks_like_command(cmd):
                    return cmd
    # Pass 2: a bare command-looking line (e.g. inside a fenced code block).
    for text in texts:
        for line in reversed(_last_nonblank_lines(text)):
            if _CODE_FENCE_RE.match(line):
                continue
            cmd = _strip_prompt_sigil(line)
            if cmd and _looks_like_command(cmd) and (" " in cmd or "/" in cmd):
                return cmd
    return ""


def _worst_action(*actions):
    """The most severe action (highest rank) among the given actions."""
    worst = "allow"
    for a in actions:
        if _risk_rank(a) > _risk_rank(worst):
            worst = a
    return worst


def _assess_pending_command(pane_text, last_assistant_text):
    """Inspect the most recent shell command for danger.

    Returns (is_destructive: bool, risk_action: str). `risk_action` is the worst
    action found across the ECC risk score, the destructive-command classifier
    (treated as `confirm`), and any governance `approval` finding (also `confirm`).
    If no command is found, returns (False, "allow").
    """
    command = _extract_last_command(pane_text, last_assistant_text)
    if not command:
        return False, "allow"

    actions = []

    # 1. ECC per-tool-call risk score (as if this were a Bash call).
    scored = risk.score_tool_call("Bash", {"command": command})
    actions.append(scored.get("action", "allow"))

    # 2. Destructive-command classifier (rm -rf, git reset --hard, drop table, …).
    classified = guard.classify_command(command)
    destructive = bool(classified.get("destructive"))
    if destructive:
        # A destructive command is at least a confirm-level concern.
        actions.append("confirm")

    # 3. Governance scan — an "approval"-type finding is a sign-off-required op,
    #    a "secret"/"security" finding is also confirm-worthy.
    for finding in guard.detect_governance(command):
        if finding.get("type") in ("approval", "secret", "security"):
            actions.append("confirm")
            destructive = True

    worst = _worst_action(*actions)
    # If the risk ladder itself put it at confirm/block, that's a destructive gate too.
    if _risk_rank(worst) >= _RISK_RANK["confirm"]:
        destructive = True
    return destructive, worst


def _has_decision_fork(lines, blob):
    """True if the pane shows a genuine multiple-choice fork."""
    # Two or more numbered option lines.
    numbered = [ln for ln in lines if _NUMBERED_OPTION_RE.match(ln)]
    if len(numbered) >= 2:
        return True
    # An explicit ask-the-human phrase.
    if any(p in blob for p in _DECISION_PHRASES):
        return True
    # "should I X or Y" either/or fork.
    if _SHOULD_X_OR_Y_RE.search(blob):
        return True
    return False


def _has_unresolved_error(lines, blob):
    """True if the pane shows an unresolved error/traceback awaiting a human."""
    if any(m in blob for m in _ERROR_MARKERS):
        return True
    # A "failed"-style line near the tail.
    tail_blob = "\n".join(lines[-12:]).lower()
    if _FAILED_RE.search(tail_blob):
        return True
    return False


def _is_routine_prompt(lines, blob):
    """True if the pane shows a generic permission / continue prompt."""
    if any(p in blob for p in _ROUTINE_PHRASES):
        return True
    # A bare interactive prompt at the very end with nothing pending.
    joined = "\n".join(lines)
    if _BARE_PROMPT_RE.search(joined):
        return True
    return False


def classify_wait(pane_text, last_assistant_text=""):
    """Classify what a waiting session is waiting FOR, and whether it's nudgeable.

    Precedence (most-dangerous wins): a pending destructive/high-risk command is
    always `destructive_confirm` (never nudgeable) regardless of how routine the
    surrounding prompt looks. Then unresolved errors, then genuine decision forks,
    then routine continue prompts. Anything we can't place is `unknown` (fail safe).

    Args:
        pane_text: the visible tmux pane / terminal text the session is showing.
        last_assistant_text: the agent's most recent message (optional extra signal).

    Returns:
        {
          "kind": "routine"|"decision"|"destructive_confirm"|"error"|"unknown",
          "nudgeable": bool,          # True ONLY for "routine"
          "reason": str,              # human-readable explanation
          "risk_action": str,         # worst risk action for any pending command
        }
    """
    try:
        pane_text = pane_text or ""
        last_assistant_text = last_assistant_text or ""

        # A single lowercased haystack of both surfaces for phrase matching.
        blob = (pane_text + "\n" + last_assistant_text).lower()
        # Combined line list (pane first, then assistant) for numbered-option / tail checks.
        lines = _last_nonblank_lines(pane_text) + _last_nonblank_lines(last_assistant_text)

        # 1. Pending destructive / high-risk command — highest precedence.
        destructive, risk_action = _assess_pending_command(pane_text, last_assistant_text)
        if destructive or _risk_rank(risk_action) >= _RISK_RANK["confirm"]:
            return {
                "kind": "destructive_confirm",
                "nudgeable": False,
                "reason": f"pending command is dangerous (risk={risk_action}); needs human confirm",
                "risk_action": risk_action,
            }

        # 2. Unresolved error / traceback awaiting a human.
        if _has_unresolved_error(lines, blob):
            return {
                "kind": "error",
                "nudgeable": False,
                "reason": "pane shows an unresolved error/traceback; surface to human",
                "risk_action": risk_action,
            }

        # 3. Genuine multiple-choice decision fork.
        if _has_decision_fork(lines, blob):
            return {
                "kind": "decision",
                "nudgeable": False,
                "reason": "pane shows a genuine decision fork; human must choose",
                "risk_action": risk_action,
            }

        # 4. Routine continue / permission prompt — the only nudgeable case.
        if _is_routine_prompt(lines, blob):
            return {
                "kind": "routine",
                "nudgeable": True,
                "reason": "routine continue/permission prompt with no pending danger",
                "risk_action": risk_action,
            }

        # 5. Can't tell — fail safe.
        return {
            "kind": "unknown",
            "nudgeable": False,
            "reason": "could not classify the wait; not nudging (fail safe)",
            "risk_action": risk_action,
        }
    except Exception as exc:  # any parse error → fail safe
        return {
            "kind": "unknown",
            "nudgeable": False,
            "reason": f"classify error ({type(exc).__name__}); not nudging (fail safe)",
            "risk_action": "block",
        }


def _parse_ts(value):
    """Parse an ISO-8601 timestamp (with or without trailing Z) → aware datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _waiting_seconds(session, now):
    """Seconds the session has been waiting.

    Prefers an explicit `session["waiting_secs"]` (the conservative path the
    contract asks the caller to provide when timestamps are unknown). Otherwise
    derives it from `now` minus the most recent of heartbeat_at / updated_at.
    Returns None if it cannot be determined (caller then must NOT nudge).
    """
    explicit = session.get("waiting_secs")
    if explicit is not None:
        try:
            return max(0.0, float(explicit))
        except (TypeError, ValueError):
            return None

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Most recent activity timestamp we can find.
    candidates = [_parse_ts(session.get("heartbeat_at")), _parse_ts(session.get("updated_at"))]
    candidates = [c for c in candidates if c is not None]
    if not candidates:
        return None
    last_active = max(candidates)
    return max(0.0, (now - last_active).total_seconds())


def should_nudge(session, pane_text, config=None, last_assistant_text="", now=None):
    """Decide whether a waiting session may be auto-nudged forward.

    Returns (True, reason) ONLY if ALL of the following hold:
      - config["auto_nudge"] is enabled,
      - the session state is "waiting",
      - the session has been waiting >= config["nudge_idle_secs"]
        (from session["waiting_secs"], else now − heartbeat_at/updated_at),
      - classify_wait(...) is nudgeable (a routine prompt with no pending danger),
      - the classified risk_action rank is strictly below rank(config["nudge_risk_max"]).

    Otherwise returns (False, reason) explaining the first gate that blocked it.
    Any parse error fails safe to (False, reason).

    Args:
        session: dict-like with at least "state" and a way to derive waiting time
                 (either "waiting_secs", or "heartbeat_at"/"updated_at" ISO strings).
        pane_text: the session's current pane text.
        config: the orch config dict (defaults to control.get_config()).
        last_assistant_text: the agent's last message (optional extra signal).
        now: override for "current time" (aware or naive UTC datetime); defaults to utcnow.

    Returns:
        (bool, str) — (should we nudge, human-readable reason).
    """
    try:
        if config is None:
            from orch import control
            config = control.get_config()

        if not config.get("auto_nudge"):
            return False, "auto_nudge disabled"

        if not isinstance(session, dict):
            return False, "session is not a dict (fail safe)"

        state = session.get("state")
        if state != "waiting":
            return False, f"session state is {state!r}, not 'waiting'"

        idle_required = config.get("nudge_idle_secs", 0)
        waited = _waiting_seconds(session, now)
        if waited is None:
            return False, ("cannot determine wait duration "
                           "(no waiting_secs/heartbeat_at/updated_at); not nudging")
        if waited < idle_required:
            return False, (f"waited {waited:.0f}s < nudge_idle_secs {idle_required}s")

        verdict = classify_wait(pane_text, last_assistant_text)
        if not verdict.get("nudgeable"):
            return False, f"not nudgeable: {verdict.get('reason', verdict.get('kind'))}"

        ceiling = config.get("nudge_risk_max", "review")
        risk_action = verdict.get("risk_action", "allow")
        if _risk_rank(risk_action) >= _risk_rank(ceiling):
            return False, (f"pending risk {risk_action!r} >= nudge_risk_max {ceiling!r}")

        return True, (f"routine wait ({waited:.0f}s idle), risk {risk_action!r} "
                      f"< ceiling {ceiling!r} — auto-continuing")
    except Exception as exc:  # fail safe on any error
        return False, f"should_nudge error ({type(exc).__name__}); not nudging (fail safe)"
