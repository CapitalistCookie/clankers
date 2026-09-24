"""Hermetic tests for lib/ecc/budget.py — per-model cost (with cache tiers),
the budget alert ladder, and the gauge-colour gradient.

Prices come from the single price table (lib/pricing.py, 2026-09-24), the one
the session-end hook prices session rows with; the ECC table (Opus $15/$75)
is gone. No I/O, no env, no network — pure arithmetic against that table.
Run: python3 -m pytest tests/test_ecc_budget.py -v   (or: python3 tests/test_ecc_budget.py)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from ecc import budget  # noqa: E402
import pricing  # noqa: E402

M = 1_000_000  # 1M tokens


def test_rates_come_from_the_single_price_table():
    # (input, output, cache_write = 5-minute rate, cache_read), $/MTok
    assert budget.rates_for("claude-opus-5-5") == (4.0, 20.0, 5.0, 0.20)
    assert budget.rates_for("claude-sonnet-5") == (2.0, 10.0, 2.5, 0.20)
    assert budget.rates_for("claude-haiku-4-5-20251001") == (1.0, 5.0, 1.25, 0.10)
    assert budget.rates_for("claude-fable-5-1") == (10.0, 50.0, 12.5, 0.25)
    for model, (pin, pout, pcr, p5m, _p1h) in pricing.PRICES.items():
        assert budget.rates_for(model) == (pin, pout, p5m, pcr), model
    assert not hasattr(budget, "PRICING")          # no second table


def test_rates_for_substring_family_and_sonnet_default():
    assert budget.rates_for("claude-opus-4-8") == budget.rates_for("claude-opus-4-8[1m]")
    assert budget.rates_for("CLAUDE-OPUS-5-5") == budget.rates_for("claude-opus-5-5")
    assert budget.rates_for("us.anthropic.claude-sonnet-4-6") == (3.0, 15.0, 3.75, 0.30)
    # the table's rule: unknown / None / "" -> Sonnet 5 (not ECC's opus default)
    sonnet5 = budget.rates_for("claude-sonnet-5")
    assert budget.rates_for("gpt-4o") == sonnet5
    assert budget.rates_for(None) == sonnet5
    assert budget.rates_for("") == sonnet5


def test_known_cost_computations():
    assert budget.session_cost({"cache_read": M}, "claude-opus-5-5") == 0.20
    assert budget.session_cost({"output": M}, "claude-sonnet-5") == 10.0
    assert budget.session_cost({"input": M}, "claude-opus-5-5") == 4.0
    assert budget.session_cost({"output": M}, "claude-opus-5-5") == 20.0
    assert budget.session_cost({"input": M}, "claude-haiku-4-5") == 1.0
    assert budget.session_cost({"cache_read": M}, "claude-fable-5-1") == 0.25


def test_cache_tier_rates():
    # 5-minute writes 1.25x input, 1-hour writes 2x input; reads per the table
    assert budget.session_cost({"cache_create": M}, "claude-opus-5-5") == 5.0
    assert budget.session_cost({"cache_create": M, "cache_create_1h": M},
                               "claude-opus-5-5") == 8.0
    assert budget.session_cost({"cache_create": M, "cache_create_1h": M // 4},
                               "claude-opus-5-5") == 5.75
    assert budget.session_cost({"cache_create": M}, "claude-sonnet-5") == 2.5
    assert budget.session_cost({"cache_read": M}, "claude-sonnet-5") == 0.20
    assert budget.session_cost({"cache_create": M}, "claude-haiku-4-5") == 1.25


def test_cache_create_and_cache_write_are_aliases():
    a = budget.session_cost({"cache_create": M}, "claude-opus-5-5")
    b = budget.session_cost({"cache_write": M}, "claude-opus-5-5")
    assert a == b == 5.0


def test_session_cost_sums_all_four_buckets():
    cost = budget.session_cost(
        {"input": M, "output": M, "cache_create": M, "cache_read": M}, "claude-opus-5-5")
    assert cost == pytest.approx(4.0 + 20.0 + 5.0 + 0.20)


def test_session_cost_defaults_and_garbage():
    assert budget.session_cost({}, "claude-opus-5-5") == 0.0
    assert budget.session_cost(None, "claude-opus-5-5") == 0.0
    assert budget.session_cost({"input": "lots"}, "claude-opus-5-5") == 0.0
    assert budget.session_cost({"output": M}) == 10.0      # no model: Sonnet 5


def test_row_cost_adds_subagents_and_keeps_write_time_prices():
    # a row the 2026-09-24 hook wrote: priced per message at write time
    hook_row = {"api_calls": 3, "estimated_cost_usd": 99.22, "subagent_cost_usd": 22.96,
                "model": "claude-fable-5-1", "tokens": {"output": M}}
    assert budget.row_cost(hook_row) == pytest.approx(122.18)
    # a `clanker wrap` row: Claude Code's own total
    wrap_row = {"cost_source": "claude_code_total_cost_usd", "estimated_cost_usd": 0.5,
                "model": "claude-sonnet-5", "tokens": {"output": M}}
    assert budget.row_cost(wrap_row) == 0.5
    # an older row: re-priced from its totals at the current table
    old_row = {"model": "claude-opus-4-8", "estimated_cost_usd": 999.0,
               "tokens": {"input": M, "output": M, "cache_read": M, "cache_create": M}}
    assert budget.row_cost(old_row) == pytest.approx(5.0 + 25.0 + 0.50 + 6.25)
    assert budget.row_cost({"estimated_cost_usd": 1.5}) == 1.5      # no tokens: stored
    assert budget.row_cost({}) == 0.0


def test_budget_action_sums_whole_rows(monkeypatch, capsys):
    from ecc import cli
    rows = [{"api_calls": 1, "estimated_cost_usd": 10.0, "subagent_cost_usd": 5.0},
            {"model": "claude-sonnet-5", "tokens": {"output": M}}]
    monkeypatch.setattr(cli, "_clanker_sessions", lambda last_days=30: rows)

    class A:
        last, limit = 30, 100.0
    ev = cli._budget(A())
    assert ev["ratio"] == pytest.approx(0.25)                 # 10 + 5 + 10 of 100
    assert "$25.00 / $100.00" in capsys.readouterr().out


def test_budget_ladder_states():
    # limit 100 so used == ratio*100
    assert budget.evaluate_budget(0.0, 100.0)["state"] == "normal"
    assert budget.evaluate_budget(50.0, 100.0)["state"] == "alert50"
    assert budget.evaluate_budget(75.0, 100.0)["state"] == "alert75"
    assert budget.evaluate_budget(90.0, 100.0)["state"] == "alert90"
    assert budget.evaluate_budget(110.0, 100.0)["state"] == "over"
    # exactly at budget is "over" (>= 1.0)
    assert budget.evaluate_budget(100.0, 100.0)["state"] == "over"
    # just under advisory stays normal
    assert budget.evaluate_budget(49.99, 100.0)["state"] == "normal"


def test_budget_ratio_and_remaining():
    r = budget.evaluate_budget(30.0, 120.0)
    assert r["ratio"] == 0.25
    assert r["remaining"] == 90.0
    # remaining floored at 0 once over budget
    over = budget.evaluate_budget(150.0, 100.0)
    assert over["remaining"] == 0.0
    assert over["ratio"] == 1.5


def test_unconfigured_when_limit_missing_or_nonpositive():
    for lim in (None, 0.0, -5.0):
        r = budget.evaluate_budget(42.0, lim)
        assert r == {"state": "unconfigured", "ratio": None, "remaining": None}


def test_custom_thresholds():
    # warning at 0.70, critical at 0.85: 0.72 -> alert75, 0.86 -> alert90
    th = (0.40, 0.70, 0.85)
    assert budget.evaluate_budget(72.0, 100.0, th)["state"] == "alert75"
    assert budget.evaluate_budget(86.0, 100.0, th)["state"] == "alert90"
    assert budget.evaluate_budget(45.0, 100.0, th)["state"] == "alert50"


def test_budget_color_endpoints():
    # ratio 0 -> green ; warning (0.75) -> yellow ; >=1.0 -> red
    assert budget.budget_color(0.0) == "#22c55e"   # (34,197,94)
    assert budget.budget_color(0.75) == "#eab308"  # (234,179,8)
    assert budget.budget_color(1.0) == "#ef4444"   # (239,68,68)
    # clamps above 1.0 to red
    assert budget.budget_color(2.0) == "#ef4444"


def test_budget_color_interpolates_between_anchors():
    # well-formed hex of length 7 across the range, and not stuck on an anchor
    mid_low = budget.budget_color(0.375)   # halfway green->yellow
    mid_high = budget.budget_color(0.875)  # halfway yellow->red
    for c in (mid_low, mid_high):
        assert c.startswith("#") and len(c) == 7
        int(c[1:], 16)  # parses as hex
    assert mid_low not in ("#22c55e", "#eab308")
    assert mid_high not in ("#eab308", "#ef4444")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
