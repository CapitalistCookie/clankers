"""lib/pricing.py: the single price table (2026-09-24).

hooks/session-end.sh prices each session row when it writes it and cannot
import lib/ (sync ships no lib/), so it carries the pricing block byte for
byte. The dashboard and `clanker ecc budget` price rows through this module,
subagent cost included; the tables they carried (Opus $15/$75) are gone."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import pricing  # noqa: E402

HOOK = os.path.join(ROOT, "hooks", "session-end.sh")
MODULE = os.path.join(ROOT, "lib", "pricing.py")
M = 1_000_000


def _block(path):
    text = open(path, encoding="utf-8").read()
    a = text.index("# ---- pricing ---")
    b = text.index("# ---- end of pricing ---")
    return text[a:text.index("\n", b) + 1]


def test_hook_and_module_carry_the_same_block():
    hook, module = _block(HOOK), _block(MODULE)
    assert "PRICES = {" in module and "def totals(calls)" in module
    assert hook == module


def test_no_other_price_table_in_lib():
    """dashboard_data and ecc/budget carried their own tables; none may return."""
    for rel in ("lib/dashboard_data.py", "lib/ecc/budget.py", "lib/ecc/cli.py"):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert "75.0" not in src and "_PRICING" not in src and "PRICING = {" not in src, rel


def test_price_table_is_the_documented_one():
    p = pricing.price_for
    assert p("claude-fable-5-1") == (10.0, 50.0, 0.25, 12.50, 20.0)
    assert p("claude-opus-5-5[1m]") == (4.0, 20.0, 0.20, 5.00, 8.0)
    assert p("claude-sonnet-5") == (2.0, 10.0, 0.20, 2.50, 4.0)
    assert p("claude-haiku-4-5-20251001") == (1.0, 5.0, 0.10, 1.25, 2.0)
    assert p("claude-mythos-5-1") == p("claude-fable-5-1")
    assert p(None) == p("claude-sonnet-5")


def test_call_cost_splits_one_hour_writes():
    # 1M writes on Opus 5.5, a quarter of them 1-hour: 0.75*5 + 0.25*8
    assert pricing.call_cost("claude-opus-5-5", 0, 0, 0, M, M // 4) == pytest.approx(5.75)
    # a 1-hour part larger than the writes is capped at the writes
    assert pricing.call_cost("claude-opus-5-5", 0, 0, 0, M, 2 * M) == pytest.approx(8.0)


def test_totals_dedupes_by_message_key():
    calls = {}
    u = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
         "cache_creation_input_tokens": 50}
    for _ in range(3):                       # one line per content block
        pricing.note_usage(calls, "msg_1", "claude-sonnet-5", u)
    tokens, cost, n = pricing.totals(calls)
    assert n == 1 and tokens == {"input": 10, "output": 5, "cache_read": 100, "cache_create": 50}
    assert cost == pytest.approx(pricing.call_cost("claude-sonnet-5", 10, 5, 100, 50))


def test_row_cost_and_row_tokens():
    hook_row = {"api_calls": 2, "estimated_cost_usd": 3.0, "model": "claude-fable-5-1",
                "tokens": {"input": 1, "output": 2, "cache_read": 3, "cache_create": 4},
                "subagent_cost_usd": 1.25,
                "subagent_tokens": {"input": 10, "output": 20, "cache_read": 30, "cache_create": 40}}
    assert pricing.row_cost(hook_row) == pytest.approx(4.25)
    assert pricing.row_tokens(hook_row) == {"input": 11, "output": 22, "cache_read": 33,
                                            "cache_create": 44}
    legacy = {"model": "claude-opus-4-8", "estimated_cost_usd": 75.0, "tokens": {"output": M}}
    assert pricing.row_cost(legacy) == pytest.approx(25.0)      # was $75 at $15/$75
    stub = {"outcome": "open", "session_id": "x"}
    assert pricing.row_cost(stub) == 0.0
    assert pricing.row_tokens(stub) == dict.fromkeys(pricing.TOKEN_KEYS, 0)
    assert pricing.row_cost(None) == 0.0
    assert pricing.row_cost({"subagent_cost_usd": "garbage", "estimated_cost_usd": float("nan")}) == 0.0


def test_dashboard_totals_include_subagents(tmp_path, monkeypatch):
    import alerts
    import analyze
    import dashboard_data
    import propose
    import wiki
    sessions = tmp_path / "raw" / "sessions"
    sessions.mkdir(parents=True)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [
        {"timestamp": ts, "session_id": "a", "project": "p", "cwd": str(tmp_path),
         "api_calls": 2, "estimated_cost_usd": 3.0, "model": "claude-fable-5-1",
         "tokens": {"input": 1_000, "output": 0, "cache_read": 0, "cache_create": 0},
         "subagent_cost_usd": 2.0,
         "subagent_tokens": {"input": 0, "output": 500_000, "cache_read": 0, "cache_create": 0}},
        {"timestamp": ts, "session_id": "b", "project": "p", "cwd": str(tmp_path),
         "model": "claude-opus-4-8", "estimated_cost_usd": 75.0,
         "tokens": {"input": 0, "output": M, "cache_read": 0, "cache_create": 0}},
    ]
    (sessions / (now.strftime("%Y-%m-%d") + ".jsonl")).write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(analyze, "SESSIONS_DIR", str(sessions))
    analyze._sessions_memo.clear()
    monkeypatch.setattr(alerts, "ALERTS_DIR", str(tmp_path / "alerts"))
    monkeypatch.setattr(propose, "LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setattr(wiki, "DATA_DIR", str(tmp_path))
    reg = tmp_path / "registry.yaml"
    reg.write_text("projects:\n  p:\n    archetype: tool\n")
    monkeypatch.setenv("CLANKER_REGISTRY", str(reg))
    monkeypatch.setenv("CLANKER_PROJECT_ROOTS", str(tmp_path / "no-roots"))
    data = dashboard_data.generate_dashboard_data()
    analyze._sessions_memo.clear()
    assert data["summary"]["total_cost"] == pytest.approx(3.0 + 2.0 + 25.0)
    assert data["summary"]["total_tokens_m"] == pytest.approx(1.5, abs=0.05)
    assert data["token_types"]["output"] == 1_500_000
