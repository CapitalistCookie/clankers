"""Per-model cost pricing (with cache tiers) + budget alert ladder.

Ported from affaan-m/ECC (OFF by default — see lib/ecc/__init__.py):
  - the per-1M rate table incl. cacheWrite/cacheRead + the getRates substring
    matcher        (scripts/hooks/cost-tracker.js)
  - the Normal/Alert50/Alert75/Alert90/OverBudget ladder + budget_ratio
                   (ecc2/src/tui/widgets.rs :: budget_state / budget_ratio)
  - the advisory 0.50 / warning 0.75 / critical 0.90 thresholds
                   (ecc2/src/config/mod.rs :: BUDGET_ALERT_THRESHOLDS)
  - the green->yellow->red gauge gradient
                   (ecc2/src/tui/widgets.rs :: gradient_color / interpolate_rgb)

PRICING uses Anthropic public list rates ($/1M tokens). cache_write is ~1.25x
the input rate, cache_read ~0.1x the input rate.

ESTIMATE ONLY — two real billing multipliers are deliberately NOT modeled here:
  - the >200K-context "long-context" 2x tier (Opus/Sonnet bill input+output at
    2x once the prompt crosses 200K tokens), and
  - the 1h ("extended") prompt-cache 2x cache-write tier.
On long sessions this under-counts. ECC's cost hook prefers the harness's
authoritative `cost.total_cost_usd` when available and keeps this table only as
a fallback; treat any number it produces as a lower-bound estimate.

Pure stdlib. No imports.
"""

# Per-1M-token billing rates (USD): (input, output, cache_write, cache_read).
# cache_write ~= 1.25x input, cache_read ~= 0.1x input.
PRICING = {
    "opus":   (15.00, 75.0, 18.75, 1.50),
    "sonnet": (3.00,  15.0, 3.75,  0.30),
    "haiku":  (0.80,  4.0,  1.00,  0.08),
}

# clanker's own usage is mostly Opus, so an unknown/unmatched model defaults to
# opus (NOT sonnet as in the upstream JS hook — see getRates there).
DEFAULT_FAMILY = "opus"

# Budget alert ladder thresholds (advisory / warning / critical), matching
# ecc2 Config::BUDGET_ALERT_THRESHOLDS.
DEFAULT_THRESHOLDS = (0.50, 0.75, 0.90)


def rates_for(model):
    """Substring-match a model id to a pricing family; return its 4-tuple.

    (input, output, cache_write, cache_read) in $/1M tokens. Matching mirrors
    cost-tracker.js getRates (case-insensitive substring), but the fall-through
    is opus, not sonnet. None / "" / unknown -> DEFAULT_FAMILY.
    """
    m = str(model or "").lower()
    if "haiku" in m:
        return PRICING["haiku"]
    if "opus" in m:
        return PRICING["opus"]
    if "sonnet" in m:
        return PRICING["sonnet"]
    return PRICING[DEFAULT_FAMILY]


def session_cost(tokens, model=None):
    """USD cost for a usage dict at the given model's rates.

    `tokens` keys (all optional, default 0):
        input        -> input_tokens
        output       -> output_tokens
        cache_read   -> cache_read_input_tokens
        cache_create -> cache_creation_input_tokens (alias: cache_write)

    Returns a float rounded to 6 decimals (micro-dollars), matching the
    rounding cost-tracker.js applies.
    """
    tokens = tokens or {}

    def _n(key, *aliases):
        for k in (key,) + aliases:
            if k in tokens:
                try:
                    v = float(tokens[k])
                except (TypeError, ValueError):
                    return 0.0
                return v if v == v else 0.0  # NaN guard
        return 0.0

    inp, out, cw, cr = rates_for(model)
    cost = (
        (_n("input") / 1e6) * inp
        + (_n("output") / 1e6) * out
        + (_n("cache_create", "cache_write") / 1e6) * cw
        + (_n("cache_read") / 1e6) * cr
    )
    return round(cost, 6)


def evaluate_budget(used_usd, limit_usd, thresholds=DEFAULT_THRESHOLDS):
    """Classify spend against a budget limit, mirroring ecc2 budget_state.

    Returns:
        {"state": <str>, "ratio": float|None, "remaining": float|None}

    state is one of:
        "unconfigured"  -> no usable limit (None or <= 0)
        "normal"        -> below the advisory threshold
        "alert50"       -> >= advisory (default 0.50)
        "alert75"       -> >= warning  (default 0.75)
        "alert90"       -> >= critical (default 0.90)
        "over"          -> >= 1.0 (at or over budget)

    ratio = used/limit (None when unconfigured); remaining = limit-used,
    floored at 0.0 (None when unconfigured).
    """
    advisory, warning, critical = thresholds

    if limit_usd is None or limit_usd <= 0.0:
        return {"state": "unconfigured", "ratio": None, "remaining": None}

    used = float(used_usd)
    ratio = used / float(limit_usd)
    remaining = max(0.0, float(limit_usd) - used)

    if ratio >= 1.0:
        state = "over"
    elif ratio >= critical:
        state = "alert90"
    elif ratio >= warning:
        state = "alert75"
    elif ratio >= advisory:
        state = "alert50"
    else:
        state = "normal"

    return {"state": state, "ratio": ratio, "remaining": remaining}


# Gauge gradient anchor colours (RGB), matching ecc2 widgets.rs gradient_color.
_GREEN = (34, 197, 94)
_YELLOW = (234, 179, 8)
_RED = (239, 68, 68)


def _interpolate_rgb(frm, to, t):
    """Linearly interpolate two RGB triples; t clamped to [0, 1]."""
    t = min(1.0, max(0.0, t))
    return tuple(round(a + (b - a) * t) for a, b in zip(frm, to))


def budget_color(ratio, thresholds=DEFAULT_THRESHOLDS):
    """Hex colour (#rrggbb) for a usage ratio: green -> yellow -> red.

    Green at ratio 0, yellow at the `warning` threshold, red at ratio >= 1.0,
    with linear interpolation between anchors. Mirrors ecc2 gradient_color
    (which uses `warning` as the green->yellow breakpoint).
    """
    _, warning, _ = thresholds
    clamped = min(1.0, max(0.0, ratio))
    if clamped <= warning:
        denom = warning if warning > 0.0 else 1e-12
        rgb = _interpolate_rgb(_GREEN, _YELLOW, clamped / denom)
    else:
        span = 1.0 - warning
        denom = span if span > 0.0 else 1e-12
        rgb = _interpolate_rgb(_YELLOW, _RED, (clamped - warning) / denom)
    return "#{:02x}{:02x}{:02x}".format(*rgb)
