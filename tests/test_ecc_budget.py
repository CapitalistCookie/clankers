"""Hermetic tests for lib/ecc/budget.py — per-model cost (with cache tiers),
the budget alert ladder, and the gauge-colour gradient.

No I/O, no env, no network — pure arithmetic against known Anthropic rates.
Run: python3 -m pytest tests/test_ecc_budget.py -v   (or: python3 tests/test_ecc_budget.py)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from ecc import budget  # noqa: E402

M = 1_000_000  # 1M tokens


def test_pricing_table_matches_anthropic_public_rates():
    assert budget.PRICING["opus"] == (15.00, 75.0, 18.75, 1.50)
    assert budget.PRICING["sonnet"] == (3.00, 15.0, 3.75, 0.30)
    assert budget.PRICING["haiku"] == (0.80, 4.0, 1.00, 0.08)


def test_rates_for_substring_match_and_opus_default():
    # exact family ids
    assert budget.rates_for("claude-opus-4-8") == budget.PRICING["opus"]
    assert budget.rates_for("claude-3-5-sonnet-20241022") == budget.PRICING["sonnet"]
    assert budget.rates_for("claude-3-5-haiku") == budget.PRICING["haiku"]
    # case-insensitive
    assert budget.rates_for("CLAUDE-OPUS-4-8") == budget.PRICING["opus"]
    # unknown / None / "" -> opus (clanker default; NOT sonnet like the JS hook)
    assert budget.rates_for("gpt-4o") == budget.PRICING["opus"]
    assert budget.rates_for(None) == budget.PRICING["opus"]
    assert budget.rates_for("") == budget.PRICING["opus"]


def test_known_cost_computations():
    # 1M opus cache_read -> $1.50
    assert budget.session_cost({"cache_read": M}, "opus") == 1.50
    # 1M sonnet output -> $15
    assert budget.session_cost({"output": M}, "claude-sonnet") == 15.0
    # 1M opus input -> $15 ; 1M opus output -> $75
    assert budget.session_cost({"input": M}, "opus") == 15.0
    assert budget.session_cost({"output": M}, "opus") == 75.0
    # 1M haiku input -> $0.80
    assert budget.session_cost({"input": M}, "haiku") == 0.80


def test_cache_tier_rates():
    # cache_write ~1.25x input; cache_read ~0.1x input — verify per family.
    assert budget.session_cost({"cache_create": M}, "opus") == 18.75   # 1.25 * 15
    assert budget.session_cost({"cache_read": M}, "opus") == 1.50      # 0.10 * 15
    assert budget.session_cost({"cache_create": M}, "sonnet") == 3.75  # 1.25 * 3
    assert budget.session_cost({"cache_read": M}, "sonnet") == 0.30    # 0.10 * 3
    assert budget.session_cost({"cache_create": M}, "haiku") == 1.00
    assert budget.session_cost({"cache_read": M}, "haiku") == 0.08


def test_cache_create_and_cache_write_are_aliases():
    a = budget.session_cost({"cache_create": M}, "opus")
    b = budget.session_cost({"cache_write": M}, "opus")
    assert a == b == 18.75


def test_session_cost_sums_all_four_buckets():
    # 1M each of input/output/cache_create/cache_read on opus.
    cost = budget.session_cost(
        {"input": M, "output": M, "cache_create": M, "cache_read": M}, "opus"
    )
    assert cost == 15.0 + 75.0 + 18.75 + 1.50  # 110.25


def test_session_cost_defaults_and_garbage():
    assert budget.session_cost({}, "opus") == 0.0
    assert budget.session_cost(None, "opus") == 0.0
    # unparseable token value -> treated as 0
    assert budget.session_cost({"input": "lots"}, "opus") == 0.0
    # default model (None) prices at opus
    assert budget.session_cost({"output": M}) == 75.0


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
