"""Hermetic tests for the dashboard auth (webauth): password, TOTP, recovery codes,
enrollment/activation, and the login state machine.

Uses an isolated CLANKER_DATA temp dir — does NOT touch production /data/clanker.
Run: python3 -m pytest tests/test_webauth.py -v   (or: python3 tests/test_webauth.py)
"""
import base64
import os
import stat
import sys
import tempfile

# Isolate the user store BEFORE importing webauth (it captures CLANKER_DATA into
# module constants at import). Restore the process env right after: webauth keeps
# the temp paths it captured, and nothing else in the pytest process is dragged
# onto the temp dir — pytest imports every test module at COLLECTION time, so a
# leaked env here poisons subprocess-spawning tests in other files in an order-
# dependent way (pytest-randomly made that a coin-flip flake).
_OLD_DATA = os.environ.get("CLANKER_DATA")
os.environ["CLANKER_DATA"] = tempfile.mkdtemp(prefix="clk-webauth-test-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import webauth  # noqa: E402

if _OLD_DATA is None:
    del os.environ["CLANKER_DATA"]
else:
    os.environ["CLANKER_DATA"] = _OLD_DATA


def test_totp_matches_rfc6238_vectors():
    # RFC 6238 Appendix B test secret (ASCII "12345678901234567890"), 6-digit SHA1.
    secret = base64.b32encode(b"12345678901234567890").decode()
    vectors = {59: "287082", 1111111109: "081804", 1234567890: "005924",
               2000000000: "279037", 20000000000: "353130"}
    for t, expected in vectors.items():
        assert webauth.totp(secret, for_time=t) == expected, f"T={t}"


def test_verify_totp_window_and_rejects_junk():
    s = webauth.gen_totp_secret()
    assert webauth.verify_totp(s, webauth.totp(s)) is True
    assert webauth.verify_totp(s, "abc") is False
    assert webauth.verify_totp(s, "") is False
    assert webauth.verify_totp(s, None) is False


def test_password_hash_roundtrip():
    salt, ph, it = webauth.hash_password("hunter2hunter2")
    assert webauth.verify_password("hunter2hunter2", salt, ph, it) is True
    assert webauth.verify_password("nope", salt, ph, it) is False


def test_add_user_starts_unactivated_and_validates():
    webauth.add_user("alice", "password123")
    assert webauth.needs_totp_setup("alice") is True
    assert webauth.check_password("alice", "password123") is True
    # login is blocked until TOTP is activated, even with a valid code
    sec = webauth._load()["alice"]["totp_secret"]
    assert webauth.verify_login("alice", "password123", webauth.totp(sec)) is False
    # weak password + duplicate are rejected
    for bad in (lambda: webauth.add_user("bob", "short"),
                lambda: webauth.add_user("alice", "password123")):
        try:
            bad()
            assert False, "expected ValueError"
        except ValueError:
            pass


def test_activation_issues_recovery_and_enables_login():
    webauth.add_user("carol", "password123")
    sec = webauth._load()["carol"]["totp_secret"]
    assert webauth.activate_totp("carol", "000000") is None        # wrong code
    codes = webauth.activate_totp("carol", webauth.totp(sec))
    assert isinstance(codes, list) and len(codes) == webauth.RECOVERY_CODE_COUNT
    assert webauth.needs_totp_setup("carol") is False
    assert webauth.verify_login("carol", "password123", webauth.totp(sec)) is True
    assert webauth.verify_login("carol", "WRONGPASS", webauth.totp(sec)) is False


def test_recovery_codes_are_one_time():
    webauth.add_user("dave", "password123")
    sec = webauth._load()["dave"]["totp_secret"]
    codes = webauth.activate_totp("dave", webauth.totp(sec))
    n0 = webauth.recovery_remaining("dave")
    assert webauth.verify_login("dave", "password123", codes[0]) is True   # recovery works
    assert webauth.recovery_remaining("dave") == n0 - 1                     # consumed
    assert webauth.verify_login("dave", "password123", codes[0]) is False  # not reusable
    assert webauth.verify_login("dave", "password123", codes[1]) is True   # other code ok
    before = webauth.recovery_remaining("dave")
    assert webauth.verify_login("dave", "password123", "zzzzz-zzzzz") is False  # junk
    assert webauth.recovery_remaining("dave") == before                    # junk doesn't consume


def test_regenerate_invalidates_old_codes():
    webauth.add_user("erin", "password123")
    sec = webauth._load()["erin"]["totp_secret"]
    old = webauth.activate_totp("erin", webauth.totp(sec))
    new = webauth.regenerate_recovery_codes("erin")
    assert set(old) != set(new)
    assert webauth.verify_login("erin", "password123", old[0]) is False
    assert webauth.verify_login("erin", "password123", new[0]) is True


def test_store_is_chmod_600():
    webauth.add_user("frank", "password123")
    assert stat.S_IMODE(os.stat(webauth.USERS_PATH).st_mode) == 0o600


def test_nonexistent_user_never_authenticates():
    assert webauth.verify_login("ghost", "password123", "000000") is False
    assert webauth.check_password("ghost", "x") is False
    assert webauth.use_recovery_code("ghost", "aaaaa-aaaaa") is False


def test_reset_totp_forces_resetup():
    webauth.add_user("heidi", "password123")
    sec = webauth._load()["heidi"]["totp_secret"]
    webauth.activate_totp("heidi", webauth.totp(sec))
    assert webauth.needs_totp_setup("heidi") is False
    webauth.reset_totp("heidi")
    assert webauth.needs_totp_setup("heidi") is True
    new_sec = webauth._load()["heidi"]["totp_secret"]
    assert new_sec != sec  # fresh secret


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
