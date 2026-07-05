"""Web auth for the clanker dashboard — username + password + TOTP (RFC 6238).

Dependency-free (Python stdlib only). The second factor is a 6-digit time-based
code from the Google Authenticator app (or any TOTP app). Users are enrolled via
the CLI (`clanker serve-user add <name>`), which prints a QR / setup key to scan.

Store: $CLANKER_DATA/.users.json, mode 0600. Per user:
    {username: {pw_salt, pw_hash, pw_iter, totp_secret, created_at}}

Passwords: PBKDF2-HMAC-SHA256. TOTP secrets: 160-bit base32. Both compared with
hmac.compare_digest. On TOTP activation the user is issued one-time recovery codes
(stored hashed) that work in place of a code if the device is lost; they can also
be regenerated from the CLI (`clanker serve-user recovery <name>`).
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from datetime import datetime, timezone

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")
USERS_PATH = os.path.join(DATA_DIR, ".users.json")

PBKDF2_ITERATIONS = 200_000
TOTP_STEP = 30
TOTP_DIGITS = 6
ISSUER = "Clanker"


# ── store ──────────────────────────────────────────────────────────────────
def _load():
    if not os.path.exists(USERS_PATH):
        return {}
    try:
        with open(USERS_PATH) as f:
            return json.load(f).get("users", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _save(users):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = USERS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"users": users}, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, USERS_PATH)


def user_count():
    return len(_load())


def list_users():
    return sorted(_load().keys())


# ── passwords ──────────────────────────────────────────────────────────────
def hash_password(password, iterations=PBKDF2_ITERATIONS):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return salt.hex(), dk.hex(), iterations


def verify_password(password, salt_hex, hash_hex, iterations=PBKDF2_ITERATIONS):
    try:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), iterations)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# ── TOTP (RFC 6238 / HOTP RFC 4226, SHA1) ──────────────────────────────────
def gen_totp_secret():
    """160-bit base32 secret (Google Authenticator compatible)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32, counter, digits=TOTP_DIGITS):
    pad = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = h[-1] & 0x0F
    code = (struct.unpack(">I", h[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp(secret_b32, for_time=None, step=TOTP_STEP, digits=TOTP_DIGITS):
    if for_time is None:
        for_time = time.time()
    return _hotp(secret_b32, int(for_time // step), digits)


def verify_totp(secret_b32, code, window=1, step=TOTP_STEP, digits=TOTP_DIGITS):
    """Verify a code, tolerating ±`window` steps of clock skew (default ±30s)."""
    if not code or not str(code).isdigit():
        return False
    code = str(code).zfill(digits)
    now = int(time.time() // step)
    for w in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret_b32, now + w, digits), code):
            return True
    return False


def otpauth_uri(username, secret, issuer=ISSUER):
    from urllib.parse import quote, urlencode
    label = quote(f"{issuer}:{username}")
    params = urlencode({"secret": secret, "issuer": issuer,
                        "algorithm": "SHA1", "digits": TOTP_DIGITS, "period": TOTP_STEP})
    return f"otpauth://totp/{label}?{params}"


# ── enrollment / verification ──────────────────────────────────────────────
def add_user(username, password, force=False):
    """Create a user + fresh TOTP secret. Returns (secret, otpauth_uri)."""
    username = username.strip()
    if not username:
        raise ValueError("username required")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    users = _load()
    if username in users and not force:
        raise ValueError(f"user '{username}' already exists (use force to reset)")
    salt, ph, it = hash_password(password)
    secret = gen_totp_secret()
    users[username] = {
        "pw_salt": salt, "pw_hash": ph, "pw_iter": it,
        "totp_secret": secret,
        "totp_activated": False,   # confirmed on first browser login (scan QR + enter a code)
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save(users)
    return secret, otpauth_uri(username, secret)


def check_password(username, password):
    """First factor only. Returns True if the password is correct."""
    u = _load().get((username or "").strip())
    if not u:
        return False
    return verify_password(password, u["pw_salt"], u["pw_hash"], u.get("pw_iter", PBKDF2_ITERATIONS))


def needs_totp_setup(username):
    """True if the user exists but hasn't confirmed their authenticator yet."""
    u = _load().get((username or "").strip())
    return bool(u) and not u.get("totp_activated")


def setup_info(username):
    """(secret, otpauth_uri) for the enrollment QR. Caller must have already
    verified the password — this reveals the TOTP secret."""
    u = _load().get((username or "").strip())
    if not u:
        return None
    return u["totp_secret"], otpauth_uri(username, u["totp_secret"])


# ── recovery (backup) codes ─────────────────────────────────────────────────
RECOVERY_CODE_COUNT = 8
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/1/i/l/o ambiguity


def _gen_recovery_codes(n=RECOVERY_CODE_COUNT):
    out = []
    for _ in range(n):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))  # ~49 bits each
        out.append(f"{raw[:5]}-{raw[5:]}")
    return out


def _norm_recovery(code):
    return (code or "").strip().lower().replace("-", "").replace(" ", "")


def _hash_recovery(code):
    return hashlib.sha256(_norm_recovery(code).encode()).hexdigest()


def use_recovery_code(username, code):
    """Consume a one-time recovery code. True if it matched (now spent)."""
    if len(_norm_recovery(code)) < 8:
        return False
    users = _load()
    u = users.get((username or "").strip())
    if not u:
        return False
    target = _hash_recovery(code)
    hashes = u.get("recovery_hashes", [])
    for i, h in enumerate(hashes):
        if hmac.compare_digest(h, target):
            hashes.pop(i)
            u["recovery_hashes"] = hashes
            _save(users)
            return True
    return False


def regenerate_recovery_codes(username):
    """Fresh set of recovery codes (invalidates any old ones). Returns plaintext."""
    users = _load()
    username = (username or "").strip()
    if username not in users:
        return None
    plain = _gen_recovery_codes()
    users[username]["recovery_hashes"] = [_hash_recovery(c) for c in plain]
    _save(users)
    return plain


def recovery_remaining(username):
    u = _load().get((username or "").strip())
    return len(u.get("recovery_hashes", [])) if u else 0


def activate_totp(username, code):
    """Confirm enrollment: if the code matches, mark the authenticator active and
    mint one-time recovery codes. Returns the plaintext recovery codes (show once)
    on success, or None on failure."""
    username = (username or "").strip()
    users = _load()
    u = users.get(username)
    if not u or not verify_totp(u["totp_secret"], code):
        return None
    u["totp_activated"] = True
    plain = _gen_recovery_codes()
    u["recovery_hashes"] = [_hash_recovery(c) for c in plain]
    _save(users)
    return plain


def reset_totp(username):
    """Force a user back to 'needs setup' with a fresh secret (e.g. lost device)."""
    username = (username or "").strip()
    users = _load()
    if username not in users:
        return False
    users[username]["totp_secret"] = gen_totp_secret()
    users[username]["totp_activated"] = False
    _save(users)
    return True


def set_password(username, password):
    users = _load()
    if username not in users:
        raise ValueError(f"no such user: {username}")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt, ph, it = hash_password(password)
    users[username].update(pw_salt=salt, pw_hash=ph, pw_iter=it)
    _save(users)


def remove_user(username):
    users = _load()
    if username not in users:
        return False
    del users[username]
    _save(users)
    return True


def verify_login(username, password, code):
    """Verify all three factors for an ACTIVATED user. Returns True only if the
    user exists, has completed TOTP setup, and password + code both check out.
    A user that hasn't finished setup returns False here — route them to setup
    via check_password() + needs_totp_setup()."""
    u = _load().get((username or "").strip())
    if not u or not u.get("totp_activated"):
        return False
    if not verify_password(password, u["pw_salt"], u["pw_hash"], u.get("pw_iter", PBKDF2_ITERATIONS)):
        return False
    # Second factor: a live TOTP code, or a one-time recovery code (consumed on use).
    if verify_totp(u["totp_secret"], code):
        return True
    return use_recovery_code(username, code)
