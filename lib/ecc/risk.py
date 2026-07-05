"""Per-tool-call danger scorer — ported from ECC's `observability::ToolCallEvent::compute_risk`.

Pure stdlib. Assigns an additive 0..1 risk score to a tool call from four factors
(base tool risk + file sensitivity + blast radius + irreversibility), clamps to
0..1, and maps the score to a suggested action via three thresholds.

Provenance: ecc2/src/observability/mod.rs (affaan-m/ECC). The pattern lists and
per-factor weights below are a verbatim port of the Rust constants. The Rust code
scores a single normalized `input_summary` string; here we flatten the relevant
values out of a structured `tool_input` dict (command / file_path / path / content)
into one lowercased haystack and run the identical substring matching.

Public API:
    score_tool_call(tool_name, tool_input, thresholds=None) -> dict
    score_session(tool_events, thresholds=None) -> dict
"""

from typing import Optional

# --- Thresholds (clanker defaults) -------------------------------------------
# ECC's RiskThresholds in Rust; these specific numbers are clanker's defaults.
# review/confirm/block are the *minimum* score for each action (inclusive).
DEFAULT_THRESHOLDS = {"review": 0.35, "confirm": 0.60, "block": 0.85}

# --- Pattern lists (ported VERBATIM from mod.rs) -----------------------------
# base_tool_risk(): keyed by lowercased tool name.
_BASE_TOOL_RISK = {
    "bash": (0.20, "shell execution can modify local or shared state"),
    "write": (0.15, "writes files directly"),
    "multiedit": (0.15, "writes files directly"),
    "edit": (0.10, "modifies existing files"),
}
_BASE_TOOL_DEFAULT = (0.05, None)

# assess_file_sensitivity()
_SECRET_PATTERNS = (
    ".env", "secret", "credential", "token", "api_key", "apikey",
    "auth", "id_rsa", ".pem", ".key",
)
_SHARED_INFRA_PATTERNS = (
    "cargo.toml", "package.json", "dockerfile", ".github/workflows",
    "schema", "migration", "production",
)

# assess_blast_radius()
_LARGE_SCOPE_PATTERNS = (
    "**", "/*", "--all", "--recursive", "entire repo", "all files",
    "across src/", "find ", " xargs ",
)
_SHARED_STATE_PATTERNS = (
    "git push --force", "git push -f", "origin main", "origin master",
    "rm -rf .", "rm -rf /",
)

# assess_irreversibility()
_HIGH_IRREVERSIBILITY_PATTERNS = (
    "rm -rf", "git reset --hard", "git clean -fd", "drop database",
    "drop table", "truncate ", "shred ",
)
_MODERATE_IRREVERSIBILITY_PATTERNS = (
    "rm -f", "git push --force", "git push -f", "delete from",
)


def _contains_any(haystack, patterns):
    """True if `haystack` contains any of `patterns` as a substring."""
    return any(p in haystack for p in patterns)


def _base_tool_risk(tool_name):
    """(score, reason) for the tool type. `tool_name` must be lowercased."""
    return _BASE_TOOL_RISK.get(tool_name, _BASE_TOOL_DEFAULT)


def _assess_file_sensitivity(haystack):
    """(score, reason) for secret / shared-infra file targeting. `haystack` lowercased."""
    if _contains_any(haystack, _SECRET_PATTERNS):
        return 0.25, "targets a sensitive file or credential surface"
    if _contains_any(haystack, _SHARED_INFRA_PATTERNS):
        return 0.15, "targets shared infrastructure or release-critical files"
    return 0.0, None


def _assess_blast_radius(haystack):
    """(score, reason) for blast radius. Shared-state beats large-scope. `haystack` lowercased."""
    if _contains_any(haystack, _SHARED_STATE_PATTERNS):
        return 0.35, "has a broad blast radius across shared state or history"
    if _contains_any(haystack, _LARGE_SCOPE_PATTERNS):
        return 0.25, "has a broad blast radius across multiple files or directories"
    return 0.0, None


def _assess_irreversibility(haystack):
    """(score, reason) for destructiveness. High beats moderate. `haystack` lowercased."""
    if _contains_any(haystack, _HIGH_IRREVERSIBILITY_PATTERNS):
        return 0.45, "includes an irreversible or destructive operation"
    if _contains_any(haystack, _MODERATE_IRREVERSIBILITY_PATTERNS):
        return 0.40, "includes an irreversible or difficult-to-undo operation"
    return 0.0, None


def _flatten_input(tool_input):
    """Flatten a structured tool_input dict into one string haystack.

    The Rust scorer matched against a single `input_summary` string. We assemble
    the equivalent surface from the dict values that carry intent: the shell
    `command` (Bash), the target path (`file_path` or `path`), and any `content`.
    Unknown keys are appended too so nothing meaningful is missed. A bare string
    is accepted as-is (mirrors the original single-string entry point).
    """
    if tool_input is None:
        return ""
    if isinstance(tool_input, str):
        return tool_input
    if not isinstance(tool_input, dict):
        return str(tool_input)

    parts = []
    # Prefer the high-signal keys first, then sweep the rest for completeness.
    primary = ("command", "file_path", "path", "content")
    for key in primary:
        val = tool_input.get(key)
        if val:
            parts.append(str(val))
    for key, val in tool_input.items():
        if key in primary or val in (None, "", [], {}):
            continue
        parts.append(str(val))
    return " ".join(parts)


def _action_from_score(score, thresholds):
    """Map a clamped score to an action via the threshold ladder (port of from_score)."""
    if score >= thresholds["block"]:
        return "block"
    if score >= thresholds["confirm"]:
        return "confirm"
    if score >= thresholds["review"]:
        return "review"
    return "allow"


def _resolve_thresholds(thresholds):
    """Merge caller overrides onto the clanker defaults (missing keys keep defaults)."""
    if not thresholds:
        return dict(DEFAULT_THRESHOLDS)
    merged = dict(DEFAULT_THRESHOLDS)
    merged.update(thresholds)
    return merged


def score_tool_call(tool_name, tool_input, thresholds=None):
    """Score a single tool call.

    Args:
        tool_name: the tool name (e.g. "Bash", "Write", "Edit"); case-insensitive.
        tool_input: a dict with keys like "command", "file_path"/"path", "content";
                    a bare string is also accepted.
        thresholds: optional {"review","confirm","block"} overrides; missing keys
                    fall back to the clanker defaults.

    Returns:
        {
          "score": float in [0, 1],
          "action": "allow" | "review" | "confirm" | "block",
          "factors": {"base", "file_sensitivity", "blast_radius", "irreversibility"},
        }
    """
    thr = _resolve_thresholds(thresholds)
    normalized_tool = (tool_name or "").lower()
    haystack = _flatten_input(tool_input).lower()

    base, _ = _base_tool_risk(normalized_tool)
    file_sensitivity, _ = _assess_file_sensitivity(haystack)
    blast_radius, _ = _assess_blast_radius(haystack)
    irreversibility, _ = _assess_irreversibility(haystack)

    raw = base + file_sensitivity + blast_radius + irreversibility
    score = min(1.0, max(0.0, raw))  # clamp 0..1

    return {
        "score": score,
        "action": _action_from_score(score, thr),
        "factors": {
            "base": base,
            "file_sensitivity": file_sensitivity,
            "blast_radius": blast_radius,
            "irreversibility": irreversibility,
        },
    }


# Action severity ordering, for picking the "worst" action across a session.
_ACTION_RANK = {"allow": 0, "review": 1, "confirm": 2, "block": 3}


def score_session(tool_events, thresholds=None):
    """Aggregate risk over a session's tool calls.

    Args:
        tool_events: list of dicts, each with "tool_name" and "tool_input"
                     (a per-event "thresholds" override is honored if present).
        thresholds: optional default overrides applied to every event.

    Returns:
        {
          "max_score": float,            # highest score in the session (0.0 if empty)
          "max_action": str,             # worst action by severity (allow if empty)
          "n_review": int,               # count of events at each action level
          "n_confirm": int,
          "n_block": int,
          "riskiest": dict | None,       # the highest-scoring event's result, plus
                                         # its "tool_name"/"tool_input"; None if empty.
        }
    """
    counts = {"review": 0, "confirm": 0, "block": 0}
    max_score = 0.0
    max_action = "allow"
    riskiest = None

    for event in tool_events or []:
        tool_name = event.get("tool_name", "")
        tool_input = event.get("tool_input")
        thr = event.get("thresholds", thresholds)
        result = score_tool_call(tool_name, tool_input, thr)
        action = result["action"]

        if action in counts:
            counts[action] += 1
        if _ACTION_RANK[action] > _ACTION_RANK[max_action]:
            max_action = action
        if riskiest is None or result["score"] > max_score:
            max_score = result["score"]
            riskiest = dict(result)
            riskiest["tool_name"] = tool_name
            riskiest["tool_input"] = tool_input

    return {
        "max_score": max_score,
        "max_action": max_action,
        "n_review": counts["review"],
        "n_confirm": counts["confirm"],
        "n_block": counts["block"],
        "riskiest": riskiest,
    }
