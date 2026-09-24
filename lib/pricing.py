"""Model prices and per-call usage accounting: the single price table.

The block between the `# ---- pricing` and `# ---- end of pricing` markers is
byte-identical to the one in hooks/session-end.sh, which prices every session
row when it writes it. `clanker sync` ships the hooks without lib/, and a hook
must not import it, so the hook carries its own copy of the block;
tests/test_pricing.py fails when the two copies differ.

Readers of session rows (dashboard_data, ecc/budget) price them with
row_cost(), which adds the subagent cost the hook records beside the main
figures, and count tokens with row_tokens().
"""
# ---- pricing ------------------------------------------------------------------
# One price table and one way to count API calls. This block is byte-identical
# in hooks/session-end.sh and in the pricing module that the dashboard and the
# budget report import. The hook ships alone and cannot import that module, so
# it carries the block itself; tests/test_pricing.py fails when the two copies
# differ. Edit both copies together.
#
# $/MTok: (input, output, cache read, 5-minute cache write, 1-hour cache write).
# Source: the claude-api skill bundled with Claude Code 2.1.280, read
# 2026-09-24 (shared/models.md, shared/model-migration.md,
# shared/prompt-caching.md). 5-minute writes are 1.25x input and 1-hour writes
# 2x input on every model. Reads are 0.1x input, except Fable 5.1 ($0.25,
# 0.025x) and Opus 5.5 ($0.20, 0.05x). The table this replaced priced every
# write at 1.25x, Fable 5.1 reads at $1.00, Sonnet 5 at $3/$15, and a whole
# session at its dominant model's rate.
import itertools

PRICES = {
    "claude-fable-5-1":  (10.0, 50.0, 0.25, 12.50, 20.0),
    "claude-fable-5":    (10.0, 50.0, 1.00, 12.50, 20.0),
    "claude-opus-5-5":   (4.0, 20.0, 0.20, 5.00, 8.0),
    "claude-opus-5":     (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-8":   (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-7":   (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-6":   (5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-sonnet-5":   (2.0, 10.0, 0.20, 2.50, 4.0),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75, 6.0),
    "claude-haiku-4-5":  (1.0, 5.0, 0.10, 1.25, 2.0),
}
# An id outside the table is priced by family; the Mythos 5.1 cache-read rate
# is unconfirmed, so Mythos takes Fable 5.1's. Anything else: Sonnet 5.
FAMILY = (("fable", "claude-fable-5-1"), ("mythos", "claude-fable-5-1"),
          ("opus", "claude-opus-5"), ("sonnet", "claude-sonnet-5"),
          ("haiku", "claude-haiku-4-5"))


def price_for(model):
    m = (model or "").lower().split("[")[0].strip()
    i = m.find("claude-")
    m = m[i:] if i >= 0 else m
    keys = [k for k in PRICES if m == k or m.startswith(k + "-")]
    if keys:
        return PRICES[max(keys, key=len)]
    for fam, k in FAMILY:
        if fam in m:
            return PRICES[k]
    return PRICES["claude-sonnet-5"]


def call_cost(model, inp, out, cache_read, cache_create, cache_create_1h=0):
    """USD for usage on one model: the 1-hour part of the cache writes at the
    1-hour rate, the rest of them at the 5-minute rate."""
    pin, pout, pcr, p5m, p1h = price_for(model)
    cc1h = min(cache_create_1h, cache_create)
    return (inp * pin + out * pout + cache_read * pcr
            + (cache_create - cc1h) * p5m + cc1h * p1h) / 1e6


def _n(v):
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def note_usage(calls, key, model, usage):
    """Record one transcript line's usage under its message key. Lines of one
    message repeat its usage; keep each field's largest value."""
    cc = usage.get("cache_creation")
    cc = cc if isinstance(cc, dict) else {}
    vals = [_n(usage.get("input_tokens")), _n(usage.get("output_tokens")),
            _n(usage.get("cache_read_input_tokens")),
            _n(usage.get("cache_creation_input_tokens")),
            _n(cc.get("ephemeral_1h_input_tokens"))]
    row = calls.get(key)
    if row is None:
        calls[key] = [model] + vals
    else:
        for i, v in enumerate(vals, 1):
            if v > row[i]:
                row[i] = v


SEQ = itertools.count()


def message_key(obj, msg):
    """The API message id; the request id or line uuid when a line has none."""
    return msg.get("id") or obj.get("requestId") or obj.get("uuid") or ("line", next(SEQ))


def totals(calls):
    """(tokens dict, cost USD, api calls) over deduplicated calls."""
    t = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    cost = 0.0
    for model, i, o, cr, cc, cc1h in calls.values():
        t["input"] += i
        t["output"] += o
        t["cache_read"] += cr
        t["cache_create"] += cc
        cost += call_cost(model, i, o, cr, cc, cc1h)
    return t, cost, len(calls)

def rewrites(calls, order):
    """Keys of the calls that re-wrote a cached prefix, in call order. Of two
    consecutive calls on one model, the later one re-wrote when it read less
    from the cache than the earlier one did: its cached prefix shrank."""
    out, prev = [], None
    for k in order:
        row = calls.get(k)
        if row is None:
            continue
        if prev is not None and row[0] == prev[0] and prev[3] > row[3]:
            out.append(k)
        prev = row
    return out


def rewrite_tokens(calls, order):
    """Cache-write tokens of the calls that rewrites() names."""
    return sum(calls[k][4] for k in rewrites(calls, order))
# ---- end of pricing -----------------------------------------------------------


# ---- session rows (this module only) ----------------------------------------------

TOKEN_KEYS = ("input", "output", "cache_read", "cache_create")


def _num(v):
    """A token count or a dollar figure as a float; garbage, NaN and negative
    values count as 0."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return v if v == v and v > 0 else 0.0


def tokens_cost(tokens, model):
    """USD for a token-count dict at `model`'s rate. Keys, all optional:
    input, output, cache_read, cache_create (alias cache_write) and
    cache_create_1h, the 1-hour part of cache_create. Without that part every
    cache write is priced at the 5-minute rate."""
    t = tokens if isinstance(tokens, dict) else {}
    cc = t.get("cache_create", t.get("cache_write"))
    return call_cost(model, _num(t.get("input")), _num(t.get("output")),
                     _num(t.get("cache_read")), _num(cc), _num(t.get("cache_create_1h")))


def main_cost(row):
    """USD of a session row's main transcript, subagents apart.

    A row priced when it was written keeps that price: since 2026-09-24 the
    session-end hook prices each API call at its own model (those rows carry
    api_calls), and a `clanker wrap` row carries Claude Code's own total
    (cost_source). An older row is re-priced from its token totals at its
    model's rate, every cache write at the 5-minute rate, because it records
    no 1-hour split. Its totals count each message once per content block
    (2-3 times), so its figure stays high."""
    if not isinstance(row, dict):
        return 0.0
    stored = row.get("estimated_cost_usd")
    if ("api_calls" in row or row.get("cost_source")) and stored is not None:
        return _num(stored)
    t = row.get("tokens")
    if isinstance(t, dict) and t:
        return tokens_cost(t, row.get("model"))
    return _num(stored)


def row_cost(row):
    """USD of a session row: its main transcript plus its subagents."""
    if not isinstance(row, dict):
        return 0.0
    return main_cost(row) + _num(row.get("subagent_cost_usd"))


def row_tokens(row):
    """{input, output, cache_read, cache_create} of a session row: its main
    transcript plus its subagents."""
    out = dict.fromkeys(TOKEN_KEYS, 0)
    if not isinstance(row, dict):
        return out
    for part in (row.get("tokens"), row.get("subagent_tokens")):
        if isinstance(part, dict):
            for k in TOKEN_KEYS:
                out[k] += int(_num(part.get(k)))
    return out
