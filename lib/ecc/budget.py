"""Per-model cost pricing (with cache tiers) + budget alert ladder.

Ported from affaan-m/ECC (OFF by default — see lib/ecc/__init__.py):
  - the Normal/Alert50/Alert75/Alert90/OverBudget ladder + budget_ratio
                   (ecc2/src/tui/widgets.rs :: budget_state / budget_ratio)
  - the advisory 0.50 / warning 0.75 / critical 0.90 thresholds
                   (ecc2/src/config/mod.rs :: BUDGET_ALERT_THRESHOLDS)
  - the green->yellow->red gauge gradient
                   (ecc2/src/tui/widgets.rs :: gradient_color / interpolate_rgb)

Prices come from the single price table, lib/pricing.py: the table the
session-end hook prices every session row with (2026-09-24). The ECC table
this module carried before (Opus $15/$75, cache_read 0.1x input for every
model) no longer applied to any model in use.

row_cost() prices a whole session row: its main transcript plus the subagent
cost the hook records beside it (subagent_cost_usd). session_cost() prices a
bare token dict at one model's rate, every cache write at the 5-minute rate
unless the dict carries cache_create_1h. On rows written before 2026-09-24
the token totals count each message 2-3 times, so their figures stay high.

Pure stdlib apart from lib/pricing.py.
"""

from pricing import price_for, row_cost as _row_cost, tokens_cost


def rates_for(model):
    """(input, output, cache_write, cache_read) in $/1M tokens for a model id,
    from the single price table; cache_write is the 5-minute rate. An id
    outside the table is priced by family, anything else at Sonnet 5 (the
    table's rule, not ECC's opus default)."""
    pin, pout, pcr, p5m, _p1h = price_for(model)
    return (pin, pout, p5m, pcr)


def session_cost(tokens, model=None):
    """USD cost for a usage dict at the given model's rates.

    `tokens` keys (all optional, default 0):
        input           -> input_tokens
        output          -> output_tokens
        cache_read      -> cache_read_input_tokens
        cache_create    -> cache_creation_input_tokens (alias: cache_write)
        cache_create_1h -> the 1-hour part of cache_create
    Unparseable values count as 0. Returns a float rounded to 6 decimals
    (micro-dollars), matching the rounding cost-tracker.js applies.
    """
    return round(tokens_cost(tokens or {}, model), 6)


def row_cost(row):
    """USD of one clanker session row: main transcript plus subagents."""
    return round(_row_cost(row), 6)


# Budget alert ladder thresholds (advisory / warning / critical), matching
# ecc2 Config::BUDGET_ALERT_THRESHOLDS.
DEFAULT_THRESHOLDS = (0.50, 0.75, 0.90)


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
