"""Hermetic tests for the ECC-ported tool-call risk scorer (lib/ecc/risk.py).

Stdlib only, no fixtures, no I/O. Includes the 4 Rust unit-test vectors from
ecc2/src/observability/mod.rs (computes_sensitive_file_risk, computes_blast_radius_risk,
computes_irreversible_risk, blocks_combined_high_risk_operations) ported as the same
score-threshold + action assertions, plus per-factor and action-ladder coverage.

Run: python3 -m pytest tests/test_ecc_risk.py -v   (or: python3 tests/test_ecc_risk.py)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from ecc import risk  # noqa: E402
from ecc.risk import score_tool_call, score_session, DEFAULT_THRESHOLDS  # noqa: E402

T = DEFAULT_THRESHOLDS  # {"review": 0.35, "confirm": 0.60, "block": 0.85}


# --- The 4 Rust unit-test vectors (ported) -----------------------------------
# Rust scored a prose input_summary; we feed the equivalent trigger substrings
# through the structured dict and assert the same threshold + action outcomes.

def test_vector_computes_sensitive_file_risk():
    # Rust: Write, "Update .env.production with rotated API token" -> Review.
    r = score_tool_call("Write", {"file_path": ".env.production",
                                  "content": "rotated API token"})
    assert r["score"] >= T["review"]
    assert r["action"] == "review"
    assert r["factors"]["file_sensitivity"] == 0.25  # secret pattern hit
    # base(0.15 Write) + secret(0.25) == 0.40
    assert abs(r["score"] - 0.40) < 1e-9


def test_vector_computes_blast_radius_risk():
    # Rust: Edit, "Apply the same replacement across src/**/*.rs" -> Review.
    r = score_tool_call("Edit", {"content": "Apply the same replacement across src/**/*.rs"})
    assert r["score"] >= T["review"]
    assert r["action"] == "review"
    assert r["factors"]["blast_radius"] == 0.25  # large-scope hit
    # base(0.10 Edit) + blast(0.25) == 0.35
    assert abs(r["score"] - 0.35) < 1e-9


def test_vector_computes_irreversible_risk():
    # Rust: Bash, "rm -f /tmp/ecc-temp.txt" -> RequireConfirmation.
    r = score_tool_call("Bash", {"command": "rm -f /tmp/ecc-temp.txt"})
    assert r["score"] >= T["confirm"]
    assert r["action"] == "confirm"
    assert r["factors"]["irreversibility"] == 0.40  # moderate hit
    # base(0.20 Bash) + moderate-irrev(0.40) == 0.60
    assert abs(r["score"] - 0.60) < 1e-9


def test_vector_blocks_combined_high_risk_operations():
    # Rust: Bash, "rm -rf . && git push --force origin main" -> Block.
    r = score_tool_call("Bash", {"command": "rm -rf . && git push --force origin main"})
    assert r["score"] >= T["block"]
    assert r["action"] == "block"
    # base(0.20) + shared-state-blast(0.35) + high-irrev(0.45) clamps to 1.0
    assert r["score"] == 1.0


# --- Base tool risk factor ----------------------------------------------------

def test_base_tool_risk_per_tool():
    assert score_tool_call("Bash", {})["factors"]["base"] == 0.20
    assert score_tool_call("Write", {})["factors"]["base"] == 0.15
    assert score_tool_call("MultiEdit", {})["factors"]["base"] == 0.15
    assert score_tool_call("Edit", {})["factors"]["base"] == 0.10
    # unknown / read-only tools get the 0.05 floor
    assert score_tool_call("Read", {})["factors"]["base"] == 0.05
    assert score_tool_call("Grep", {})["factors"]["base"] == 0.05


def test_base_tool_name_is_case_insensitive():
    assert score_tool_call("bash", {})["factors"]["base"] == 0.20
    assert score_tool_call("BASH", {})["factors"]["base"] == 0.20
    assert score_tool_call("BaSh", {})["factors"]["base"] == 0.20


def test_benign_read_only_call_is_allowed():
    r = score_tool_call("Read", {"file_path": "src/main.py"})
    assert r["score"] == 0.05
    assert r["action"] == "allow"


# --- File sensitivity factor --------------------------------------------------

def test_file_sensitivity_secret_patterns():
    for hay in (".env", "my-secret.txt", "aws credential file", "token store",
                "api_key", "apikey", "auth config", "id_rsa", "key.pem", "tls.key"):
        f = score_tool_call("Read", {"file_path": hay})["factors"]["file_sensitivity"]
        assert f == 0.25, hay


def test_file_sensitivity_shared_infra_patterns():
    for hay in ("Cargo.toml", "package.json", "Dockerfile", ".github/workflows/ci.yml",
                "schema.sql", "0001_migration.sql", "production.yaml"):
        f = score_tool_call("Read", {"file_path": hay})["factors"]["file_sensitivity"]
        assert f == 0.15, hay


def test_file_sensitivity_secret_beats_shared_infra():
    # A path matching both buckets must take the higher (secret) score.
    r = score_tool_call("Write", {"file_path": "production/secret.pem"})
    assert r["factors"]["file_sensitivity"] == 0.25


def test_file_sensitivity_none_for_plain_path():
    assert score_tool_call("Read", {"file_path": "src/utils/math.py"})["factors"]["file_sensitivity"] == 0.0


# --- Blast radius factor ------------------------------------------------------

def test_blast_radius_large_scope_patterns():
    for hay in ("rm **/*.log", "del /*", "git add --all", "grep --recursive x .",
                "entire repo", "all files", "across src/", "find . -name x", "ls | xargs rm"):
        f = score_tool_call("Bash", {"command": hay})["factors"]["blast_radius"]
        assert f == 0.25, hay


def test_blast_radius_shared_state_patterns():
    for hay in ("git push --force", "git push -f", "deploy to origin main",
                "push origin master", "rm -rf .", "rm -rf /var"):
        f = score_tool_call("Bash", {"command": hay})["factors"]["blast_radius"]
        assert f == 0.35, hay


def test_blast_radius_shared_state_beats_large_scope():
    # "rm -rf /" matches shared-state(0.35); ensure it isn't capped at large-scope.
    r = score_tool_call("Bash", {"command": "rm -rf / --recursive"})
    assert r["factors"]["blast_radius"] == 0.35


def test_blast_radius_none_for_scoped_command():
    assert score_tool_call("Bash", {"command": "echo hello"})["factors"]["blast_radius"] == 0.0


# --- Irreversibility factor ---------------------------------------------------

def test_irreversibility_high_patterns():
    for hay in ("rm -rf build", "git reset --hard HEAD~1", "git clean -fd",
                "DROP DATABASE app", "drop table users", "truncate logs",
                "shred secret"):
        f = score_tool_call("Bash", {"command": hay})["factors"]["irreversibility"]
        assert f == 0.45, hay


def test_irreversibility_moderate_patterns():
    for hay in ("rm -f tmp.txt", "git push --force", "git push -f",
                "DELETE FROM sessions"):
        f = score_tool_call("Bash", {"command": hay})["factors"]["irreversibility"]
        assert f == 0.40, hay


def test_irreversibility_high_beats_moderate():
    # "rm -rf" (high) and "rm -f" (moderate) both substring-match; high must win.
    r = score_tool_call("Bash", {"command": "rm -rf node_modules"})
    assert r["factors"]["irreversibility"] == 0.45


def test_irreversibility_none_for_safe_command():
    assert score_tool_call("Bash", {"command": "ls -la"})["factors"]["irreversibility"] == 0.0


# --- Action ladder + clamping -------------------------------------------------

def test_action_ladder_boundaries():
    # Exactly-at-threshold scores take the higher action (>= is inclusive).
    assert risk._action_from_score(0.00, T) == "allow"
    assert risk._action_from_score(0.349, T) == "allow"
    assert risk._action_from_score(0.35, T) == "review"
    assert risk._action_from_score(0.599, T) == "review"
    assert risk._action_from_score(0.60, T) == "confirm"
    assert risk._action_from_score(0.849, T) == "confirm"
    assert risk._action_from_score(0.85, T) == "block"
    assert risk._action_from_score(1.00, T) == "block"


def test_score_is_clamped_to_unit_interval():
    # base(0.20) + shared-state(0.35) + high-irrev(0.45) = 1.00 before clamp.
    r = score_tool_call("Bash", {"command": "rm -rf / && git push --force origin main"})
    assert 0.0 <= r["score"] <= 1.0
    assert r["score"] == 1.0


def test_threshold_overrides_are_honored():
    # Same input, stricter block threshold flips confirm -> something lower-acting,
    # and a looser one flips review -> allow.
    r_default = score_tool_call("Bash", {"command": "rm -f x.txt"})  # score 0.60
    assert r_default["action"] == "confirm"
    strict = score_tool_call("Bash", {"command": "rm -f x.txt"},
                             thresholds={"confirm": 0.70})
    assert strict["action"] == "review"  # 0.60 now below confirm, still >= review
    loose = score_tool_call("Edit", {"content": "across src/**"},  # score 0.35
                            thresholds={"review": 0.50})
    assert loose["action"] == "allow"


def test_partial_threshold_override_keeps_other_defaults():
    # Overriding only "block" must leave review/confirm at clanker defaults.
    r = score_tool_call("Bash", {"command": "rm -f x.txt"}, thresholds={"block": 0.99})
    assert r["action"] == "confirm"  # 0.60 still >= default confirm(0.60)


# --- Input flattening edge cases ---------------------------------------------

def test_path_key_alias_is_scanned():
    # "path" is honored as an alias for "file_path".
    r = score_tool_call("Read", {"path": "config/secret.pem"})
    assert r["factors"]["file_sensitivity"] == 0.25


def test_bare_string_input_is_accepted():
    # Mirrors the Rust single-string entry point.
    r = score_tool_call("Bash", "rm -rf .")
    assert r["factors"]["blast_radius"] == 0.35


def test_empty_and_none_input_score_only_base():
    assert score_tool_call("Bash", {})["score"] == 0.20
    assert score_tool_call("Bash", None)["score"] == 0.20
    assert score_tool_call("", {})["score"] == 0.05  # unknown tool floor


def test_unknown_dict_keys_still_contribute():
    # A trigger living under an unexpected key is still caught by the sweep.
    r = score_tool_call("Bash", {"weird_key": "rm -rf ."})
    assert r["factors"]["blast_radius"] == 0.35


# --- Session aggregation ------------------------------------------------------

def test_score_session_aggregates_counts_and_riskiest():
    events = [
        {"tool_name": "Read", "tool_input": {"file_path": "a.py"}},               # allow 0.05
        {"tool_name": "Write", "tool_input": {"file_path": ".env"}},              # review 0.40
        {"tool_name": "Bash", "tool_input": {"command": "rm -f x"}},              # confirm 0.60
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf . ; git push --force origin main"}},  # block 1.0
    ]
    s = score_session(events)
    assert s["n_review"] == 1
    assert s["n_confirm"] == 1
    assert s["n_block"] == 1
    assert s["max_score"] == 1.0
    assert s["max_action"] == "block"
    assert s["riskiest"]["tool_name"] == "Bash"
    assert s["riskiest"]["action"] == "block"
    assert s["riskiest"]["tool_input"] == events[3]["tool_input"]


def test_score_session_empty():
    s = score_session([])
    assert s["max_score"] == 0.0
    assert s["max_action"] == "allow"
    assert s["n_review"] == s["n_confirm"] == s["n_block"] == 0
    assert s["riskiest"] is None


def test_score_session_all_benign():
    events = [
        {"tool_name": "Read", "tool_input": {"file_path": "a.py"}},
        {"tool_name": "Grep", "tool_input": {"command": "grep foo b.py"}},
    ]
    s = score_session(events)
    assert s["max_action"] == "allow"
    assert s["n_review"] == s["n_confirm"] == s["n_block"] == 0
    assert s["riskiest"]["action"] == "allow"  # still records the highest (a benign one)


def test_score_session_honors_default_and_per_event_thresholds():
    events = [{"tool_name": "Bash", "tool_input": {"command": "rm -f x"}}]   # 0.60
    # Session-wide override pushes confirm above 0.60 -> downgraded to review.
    s = score_session(events, thresholds={"confirm": 0.70})
    assert s["max_action"] == "review"
    assert s["n_confirm"] == 0
    # A per-event override beats the session default.
    events2 = [{"tool_name": "Bash", "tool_input": {"command": "rm -f x"},
                "thresholds": {"confirm": 0.99}}]
    s2 = score_session(events2, thresholds={"confirm": 0.50})
    assert s2["max_action"] == "review"


# --- Output shape contract ----------------------------------------------------

def test_output_shape_contract():
    r = score_tool_call("Bash", {"command": "ls"})
    assert set(r.keys()) == {"score", "action", "factors"}
    assert set(r["factors"].keys()) == {"base", "file_sensitivity", "blast_radius", "irreversibility"}
    assert isinstance(r["score"], float)
    assert r["action"] in {"allow", "review", "confirm", "block"}
    s = score_session([{"tool_name": "Bash", "tool_input": {"command": "ls"}}])
    assert set(s.keys()) == {"max_score", "max_action", "n_review", "n_confirm", "n_block", "riskiest"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
