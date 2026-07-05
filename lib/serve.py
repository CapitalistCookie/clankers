"""Live dashboard server with tmux terminal bridge, Google SSO, and push notifications."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import pty
import fcntl
import re
import secrets
import struct
import subprocess
import sys
import termios
import time
import uuid
from collections import defaultdict
from urllib.parse import urlencode, quote

sys.path.insert(0, os.path.dirname(__file__))

from aiohttp import web, ClientSession

log = logging.getLogger("clanker.serve")

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")

# ─── Configuration ───
PORT = int(os.environ.get("CLANKER_SERVE_PORT", "8899"))
BASE_URL = os.environ.get("CLANKER_BASE_URL", "")  # e.g. https://dashboard.eigenstate.app
NTFY_TOPIC = os.environ.get("CLANKER_NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("CLANKER_NTFY_SERVER", "https://ntfy.sh")
# Access token for a self-hosted/authenticated ntfy server (Bearer). Empty = none.
NTFY_TOKEN = os.environ.get("CLANKER_NTFY_TOKEN", "")

# Auto-detect best bind address
def _detect_bind_address():
    """Find the best address to bind to: Tailscale > LAN > localhost."""
    import socket
    # Try Tailscale IP if it's a real local interface
    try:
        ts_ip = subprocess.check_output(
            ["tailscale", "ip", "-4"], stderr=subprocess.DEVNULL, text=True, timeout=3
        ).strip()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((ts_ip, 0))
            sock.close()
            return ts_ip, "tailscale"
        except OSError:
            sock.close()
    except Exception:
        pass
    # Fall back to LAN IP (reachable from phone/laptop)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        lan_ip = sock.getsockname()[0]
        sock.close()
        return lan_ip, "lan"
    except Exception:
        pass
    return "127.0.0.1", "localhost"

def _compute_build_id():
    """Short git SHA of the running code, computed ONCE at import (no per-request
    subprocess). Surfaced as the X-Clanker-Build header so a deploy is verifiable."""
    try:
        sha = subprocess.check_output(
            ["git", "-C", os.path.dirname(os.path.abspath(__file__)), "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, timeout=3,
        ).strip()
        return sha or "unknown"
    except Exception:
        return "unknown"


BUILD_ID = _compute_build_id()

_AUTO_HOST, _NET_TYPE = _detect_bind_address()
# Default to loopback. The cloudflared tunnel connects to the origin over
# localhost, so binding 127.0.0.1 keeps the public dashboard working while making
# the origin UNREACHABLE directly (no LAN/internet bypass of Cloudflare Access).
# Override with CLANKER_SERVE_HOST=0.0.0.0 only if you know what you're doing.
HOST = os.environ.get("CLANKER_SERVE_HOST", "127.0.0.1")

# Auth: username + password + TOTP (RFC 6238). Users enrolled via
# `clanker serve-user add <name>`. See lib/webauth.py. There is no token bypass,
# no OAuth, and no header trust — a valid signed session cookie (issued only after
# a successful 3-factor login) is the sole way past auth_middleware.
import webauth

# Cookie secret — auto-generate and persist if not provided
COOKIE_SECRET = os.environ.get("CLANKER_COOKIE_SECRET", "")
if not COOKIE_SECRET:
    _secret_path = os.path.join(DATA_DIR, ".cookie_secret")
    if os.path.exists(_secret_path):
        with open(_secret_path) as f:
            COOKIE_SECRET = f.read().strip()
    else:
        COOKIE_SECRET = secrets.token_hex(32)
        os.makedirs(os.path.dirname(_secret_path), exist_ok=True)
        with open(_secret_path, "w") as f:
            f.write(COOKIE_SECRET)

# Claude Code progress indicators (in capture-pane content)
PROGRESS_WORDS = [
    "Thinking", "Cooking", "Envisioning", "Planning", "Generating",
    "Reading", "Editing", "Writing", "Searching", "Running", "Analyzing",
    "Investigating", "Exploring", "Reasoning", "Reflecting", "Reviewing",
    "tokens)", "context)", "Cooked for",
]
# Spinner characters Claude Code uses in terminal titles and content
SPINNERS = set("⠐⠑⠒⠓⠔⠕⠖⠗⠘⠙⠚⠛⠜⠝⠞⠟⠠⠡⠢⠣⠤⠥⠦⠧⠨⠩⠪⠫⠬⠭⠮⠯"
               "⠰⠱⠲⠳⠴⠵⠶⠷⠸⠹⠺⠻⠼⠽⠾⠿✳✶✷✸✹✺✻✼⣾⣽⣻⢿⡿⣟⣯⣷")


# ══════════════════════════════════════════════════════════════════════════════
# Auth: HMAC-signed cookies (session + short-lived TOTP-setup token)
# ══════════════════════════════════════════════════════════════════════════════

def _sign_cookie(subject):
    """Create an HMAC-signed 7-day session cookie for an authenticated user."""
    expires = int(time.time()) + 86400 * 7  # 7 days
    payload = f"{subject}:{expires}"
    sig = hmac.new(COOKIE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()


def _sign_setup(username, ttl=600):
    """Short-lived (10 min) token authorising the TOTP-enrollment page. Issued
    only after a correct password on a not-yet-activated account."""
    expires = int(time.time()) + ttl
    payload = f"setup:{username}:{expires}"
    sig = hmac.new(COOKIE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()


def _verify_setup(cookie):
    """Return the username from a valid, unexpired setup token, else None."""
    try:
        decoded = base64.urlsafe_b64decode(cookie).decode()
        payload, sig = decoded.rsplit(":", 1)
        expected = hmac.new(COOKIE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        rest, expires_str = payload.rsplit(":", 1)
        if not rest.startswith("setup:") or int(expires_str) < time.time():
            return None
        return rest[len("setup:"):]
    except Exception:
        return None


def _verify_cookie(cookie):
    """Verify and decode a signed session cookie. Returns username or None."""
    try:
        decoded = base64.urlsafe_b64decode(cookie).decode()
        payload, sig = decoded.rsplit(":", 1)
        expected = hmac.new(COOKIE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        user, expires_str = payload.rsplit(":", 1)
        if int(expires_str) < time.time():
            return None
        return user
    except Exception:
        return None


def _is_https(request):
    """Best-effort: are we behind https? (CF/tunnel sets X-Forwarded-Proto)."""
    if request.headers.get("X-Forwarded-Proto", "").lower() == "https":
        return True
    return bool(BASE_URL) and BASE_URL.startswith("https")


async def handle_logout(request):
    response = web.HTTPFound("/auth/login")
    response.del_cookie("clanker_session")
    return response


# ══════════════════════════════════════════════════════════════════════════════
# Middleware
# ══════════════════════════════════════════════════════════════════════════════

@web.middleware
async def auth_middleware(request, handler):
    """Only a valid signed session cookie grants access. The cookie is issued
    solely by handle_login_submit after a successful username + password + TOTP
    login. No token bypass, no OAuth, no trusted header."""
    if request.path.startswith("/auth/") or request.path.startswith("/vendor/"):
        return await handler(request)

    cookie = request.cookies.get("clanker_session")
    if cookie:
        user = _verify_cookie(cookie)
        if user:
            request["user"] = user
            return await handler(request)

    if request.path.startswith("/ws/") or request.path.startswith("/api/"):
        raise web.HTTPUnauthorized(text="Not authenticated")
    raise web.HTTPFound("/auth/login")


# ── Login: username + password + TOTP (RFC 6238) ──
_login_fails = {}            # username -> [fail_count, locked_until_ts]
_LOCK_THRESHOLD = 5
_LOCK_SECONDS = 60


def _lock_remaining(username):
    rec = _login_fails.get(username)
    if rec and rec[1] > time.time():
        return int(rec[1] - time.time())
    return 0


def _record_fail(username):
    rec = _login_fails.get(username, [0, 0])
    rec[0] += 1
    if rec[0] >= _LOCK_THRESHOLD:
        rec[1] = time.time() + _LOCK_SECONDS
        rec[0] = 0
    _login_fails[username] = rec


def _login_html(error="", info=""):
    err = f'<div class="msg err">{error}</div>' if error else ""
    nfo = f'<div class="msg info">{info}</div>' if info else ""
    return f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Clanker — Login</title>
<style>
body {{ background: #0C0A09; color: #FAFAF9; font-family: 'JetBrains Mono', monospace; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
.login {{ background: #1C1917; padding: 40px; max-width: 380px; width: 90%; border-top: 3px solid #C2410C; }}
h1 {{ font-family: 'Instrument Serif', Georgia, serif; color: #FEF3C7; margin: 0 0 24px; font-weight: 400; letter-spacing: 0.05em; }}
label {{ display: block; font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; color: #78716C; margin-bottom: 6px; }}
input {{ width: 100%; padding: 12px; background: #292524; border: 1px solid #57534E; color: #FAFAF9; font-family: inherit; font-size: 16px; margin-bottom: 16px; box-sizing: border-box; }}
input:focus {{ border-color: #C2410C; outline: none; }}
input#code {{ letter-spacing: 0.4em; text-align: center; }}
button {{ width: 100%; padding: 12px; background: #C2410C; color: #FEF3C7; border: none; font-family: inherit; font-size: 13px; cursor: pointer; text-transform: uppercase; letter-spacing: 0.1em; }}
button:hover {{ background: #a33a0a; }}
.msg {{ font-size: 12px; padding: 8px 10px; margin-bottom: 16px; }}
.err {{ color: #FCA5A5; border-left: 2px solid #DC2626; background: rgba(220,38,38,0.08); }}
.info {{ color: #FCD34D; border-left: 2px solid #C2410C; background: rgba(194,65,12,0.08); }}
.hint {{ color: #57534E; font-size: 10px; margin-top: 14px; line-height: 1.5; }}
</style></head><body>
<div class="login">
  <h1>CL<span style="color:#C2410C">A</span>NKER</h1>
  {err}{nfo}
  <form method="POST" action="/auth/login" autocomplete="on">
    <label>Username</label>
    <input type="text" name="username" autocomplete="username" autofocus required>
    <label>Password</label>
    <input type="password" name="password" autocomplete="current-password" required>
    <label>Authenticator code <span style="text-transform:none;letter-spacing:0">or backup code</span></label>
    <input id="code" type="text" name="code" autocomplete="one-time-code"
           maxlength="12" placeholder="000000">
    <button type="submit">Sign in</button>
  </form>
  <div class="hint">Username + password + 6-digit code from Google Authenticator (or a one-time backup code).<br>
  First time? Enter username + password and leave the code blank — we'll set up your authenticator.</div>
</div></body></html>'''


async def handle_login(request):
    """GET /auth/login — show the login form."""
    if webauth.user_count() == 0:
        return web.Response(
            text=_login_html(info="No account yet. Open the one-time setup link printed when the "
                                  "server started (it contains a token), or run "
                                  "<code>clanker serve-user add &lt;name&gt;</code> on the host."),
            content_type="text/html")
    err = "Invalid username, password, or code." if request.query.get("error") else ""
    return web.Response(text=_login_html(error=err), content_type="text/html")


async def handle_login_submit(request):
    """POST /auth/login — verify all three factors, issue session cookie."""
    data = await request.post()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    code = (data.get("code") or "").strip().replace(" ", "")

    wait = _lock_remaining(username)
    if wait:
        return web.Response(
            text=_login_html(error=f"Too many attempts — wait {wait}s and retry."),
            content_type="text/html", status=429)

    # Activated user: full three-factor login.
    if webauth.verify_login(username, password, code):
        _login_fails.pop(username, None)
        log.info("Login OK: %s", username)
        resp = web.HTTPFound("/")
        resp.set_cookie(
            "clanker_session", _sign_cookie(username),
            max_age=86400 * 7, httponly=True, samesite="Lax",
            secure=_is_https(request))
        return resp

    # Password correct but authenticator not yet set up → first-time enrollment.
    if webauth.check_password(username, password) and webauth.needs_totp_setup(username):
        _login_fails.pop(username, None)
        log.info("TOTP setup started: %s", username)
        resp = web.HTTPFound("/auth/setup")
        resp.set_cookie(
            "clanker_setup", _sign_setup(username),
            max_age=600, httponly=True, samesite="Lax", secure=_is_https(request))
        return resp

    _record_fail(username)
    log.warning("Login FAIL: user=%r", username or "(blank)")
    raise web.HTTPFound("/auth/login?error=1")


# ── First-time TOTP enrollment (scan QR in the browser, confirm a code) ──
def _setup_html(username, secret, uri, error=""):
    import json as _json
    err = f'<div class="msg err">{error}</div>' if error else ""
    grouped = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
    return f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Clanker — Set up 2FA</title>
<script src="/vendor/qrcode.js"></script>
<style>
body {{ background: #0C0A09; color: #FAFAF9; font-family: 'JetBrains Mono', monospace; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
.box {{ background: #1C1917; padding: 36px; max-width: 420px; width: 92%; border-top: 3px solid #C2410C; }}
h1 {{ font-family: 'Instrument Serif', Georgia, serif; color: #FEF3C7; margin: 0 0 8px; font-weight: 400; }}
p {{ color: #A8A29E; font-size: 12px; line-height: 1.6; margin: 0 0 18px; }}
.qr {{ background: #FAFAF9; padding: 12px; width: fit-content; margin: 0 auto 16px; }}
.qr img {{ display: block; image-rendering: pixelated; }}
.key {{ font-size: 12px; color: #FCD34D; text-align: center; letter-spacing: 0.15em; word-break: break-all; background: #292524; padding: 8px; margin-bottom: 18px; }}
label {{ display: block; font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; color: #78716C; margin-bottom: 6px; }}
input {{ width: 100%; padding: 12px; background: #292524; border: 1px solid #57534E; color: #FAFAF9; font: inherit; font-size: 16px; letter-spacing: 0.4em; text-align: center; margin-bottom: 16px; box-sizing: border-box; }}
input:focus {{ border-color: #C2410C; outline: none; }}
button {{ width: 100%; padding: 12px; background: #C2410C; color: #FEF3C7; border: none; font: inherit; font-size: 13px; cursor: pointer; text-transform: uppercase; letter-spacing: 0.1em; }}
button:hover {{ background: #a33a0a; }}
.msg {{ font-size: 12px; padding: 8px 10px; margin-bottom: 16px; }}
.err {{ color: #FCA5A5; border-left: 2px solid #DC2626; background: rgba(220,38,38,0.08); }}
.step {{ color: #78716C; font-size: 11px; }}
</style></head><body>
<div class="box">
  <h1>Set up 2FA</h1>
  <p class="step">Signed in as <b style="color:#FEF3C7">{username}</b>. One-time setup for your account.</p>
  {err}
  <p>1. Open <b>Google Authenticator</b> → add account → scan this QR (or enter the key below).</p>
  <div class="qr" id="qr"></div>
  <div class="key">{grouped}</div>
  <p>2. Enter the 6-digit code it now shows to confirm:</p>
  <form method="POST" action="/auth/setup">
    <label>Authenticator code</label>
    <input type="text" name="code" inputmode="numeric" pattern="[0-9 ]*" autocomplete="one-time-code" maxlength="7" placeholder="000000" autofocus required>
    <button type="submit">Confirm &amp; finish</button>
  </form>
</div>
<script>
  var qr = qrcode(0, 'M');
  qr.addData({_json.dumps(uri)});
  qr.make();
  document.getElementById('qr').innerHTML = qr.createImgTag(5, 0);
</script>
</body></html>'''


async def handle_setup(request):
    """GET /auth/setup — show the enrollment QR (gated by the setup token)."""
    username = _verify_setup(request.cookies.get("clanker_setup", ""))
    if not username or not webauth.needs_totp_setup(username):
        raise web.HTTPFound("/auth/login")
    secret, uri = webauth.setup_info(username)
    return web.Response(text=_setup_html(username, secret, uri), content_type="text/html")


def _recovery_codes_html(codes):
    items = "".join(f"<code>{c}</code>" for c in codes)
    return f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Clanker — Backup codes</title>
<style>
body {{ background: #0C0A09; color: #FAFAF9; font-family: 'JetBrains Mono', monospace; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
.box {{ background: #1C1917; padding: 36px; max-width: 420px; width: 92%; border-top: 3px solid #65A30D; }}
h1 {{ font-family: 'Instrument Serif', Georgia, serif; color: #FEF3C7; margin: 0 0 8px; font-weight: 400; }}
p {{ color: #A8A29E; font-size: 12px; line-height: 1.6; margin: 0 0 16px; }}
.codes {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 18px; }}
.codes code {{ background: #292524; color: #FCD34D; padding: 8px; text-align: center; font-size: 14px; letter-spacing: 0.08em; }}
.warn {{ color: #FCA5A5; font-size: 11px; margin-bottom: 16px; }}
a.btn {{ display: block; text-align: center; padding: 12px; background: #C2410C; color: #FEF3C7; text-decoration: none; font-size: 13px; text-transform: uppercase; letter-spacing: 0.1em; }}
a.btn:hover {{ background: #a33a0a; }}
</style></head><body>
<div class="box">
  <h1>✓ 2FA enabled</h1>
  <p>Save these <b>backup codes</b> somewhere safe. Each works <b>once</b> in place of your authenticator code if you ever lose your phone.</p>
  <div class="codes">{items}</div>
  <p class="warn">⚠ This is the only time they're shown. Regenerate anytime with <code>clanker serve-user recovery {""}</code>.</p>
  <a class="btn" href="/">I've saved them — continue →</a>
</div></body></html>'''


async def handle_setup_submit(request):
    """POST /auth/setup — confirm the first code, activate, show backup codes, sign in."""
    username = _verify_setup(request.cookies.get("clanker_setup", ""))
    if not username or not webauth.needs_totp_setup(username):
        raise web.HTTPFound("/auth/login")
    data = await request.post()
    code = (data.get("code") or "").strip().replace(" ", "")
    recovery_codes = webauth.activate_totp(username, code)
    if recovery_codes:
        log.info("TOTP activated: %s", username)
        # Log them in now, but show the one-time backup codes before the dashboard.
        resp = web.Response(text=_recovery_codes_html(recovery_codes), content_type="text/html")
        resp.set_cookie(
            "clanker_session", _sign_cookie(username),
            max_age=86400 * 7, httponly=True, samesite="Lax", secure=_is_https(request))
        resp.del_cookie("clanker_setup")
        return resp
    secret, uri = webauth.setup_info(username)
    return web.Response(
        text=_setup_html(username, secret, uri,
                         error="That code didn't match. Make sure you scanned the QR, then enter the current 6-digit code."),
        content_type="text/html", status=400)


# ── First-run account creation (web UI bootstrap) ──
# When NO users exist, the server prints a one-time setup link containing this
# token. /auth/register accepts it to create the first account entirely in the
# browser, then hands off to the TOTP setup flow. Once any user exists, this
# closes automatically — so it can never be used to self-register on a running
# instance, even one exposed to the internet.
_bootstrap_token = None


def _bootstrap_open():
    return _bootstrap_token is not None and webauth.user_count() == 0


def _register_html(token, error=""):
    err = f'<div class="msg err">{error}</div>' if error else ""
    return f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Clanker — Create account</title>
<style>
body {{ background: #0C0A09; color: #FAFAF9; font-family: 'JetBrains Mono', monospace; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
.box {{ background: #1C1917; padding: 36px; max-width: 380px; width: 92%; border-top: 3px solid #C2410C; }}
h1 {{ font-family: 'Instrument Serif', Georgia, serif; color: #FEF3C7; margin: 0 0 6px; font-weight: 400; }}
.step {{ color: #C2410C; font-size: 10px; text-transform: uppercase; letter-spacing: 0.15em; margin-bottom: 20px; }}
label {{ display: block; font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; color: #78716C; margin-bottom: 6px; }}
input {{ width: 100%; padding: 12px; background: #292524; border: 1px solid #57534E; color: #FAFAF9; font: inherit; font-size: 16px; margin-bottom: 16px; box-sizing: border-box; }}
input:focus {{ border-color: #C2410C; outline: none; }}
button {{ width: 100%; padding: 12px; background: #C2410C; color: #FEF3C7; border: none; font: inherit; font-size: 13px; cursor: pointer; text-transform: uppercase; letter-spacing: 0.1em; }}
button:hover {{ background: #a33a0a; }}
.msg {{ font-size: 12px; padding: 8px 10px; margin-bottom: 16px; }}
.err {{ color: #FCA5A5; border-left: 2px solid #DC2626; background: rgba(220,38,38,0.08); }}
.hint {{ color: #57534E; font-size: 11px; margin-top: 14px; line-height: 1.5; }}
</style></head><body>
<div class="box">
  <h1>Create your account</h1>
  <div class="step">Step 1 of 2 — account &nbsp;›&nbsp; <span style="color:#57534E">Step 2 — 2FA</span></div>
  {err}
  <form method="POST" action="/auth/register" autocomplete="on">
    <input type="hidden" name="token" value="{token}">
    <label>Username</label>
    <input type="text" name="username" autocomplete="username" autofocus required>
    <label>Password (min 8 chars)</label>
    <input type="password" name="password" autocomplete="new-password" required>
    <label>Confirm password</label>
    <input type="password" name="confirm" autocomplete="new-password" required>
    <button type="submit">Create account →</button>
  </form>
  <div class="hint">Next you'll scan a QR into Google Authenticator to enable 2FA.</div>
</div></body></html>'''


def _bootstrap_closed_page():
    return ('<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Clanker</title>'
            '<style>body{background:#0C0A09;color:#A8A29E;font-family:monospace;'
            'display:flex;justify-content:center;align-items:center;min-height:100vh;text-align:center;padding:20px}'
            'a{color:#C2410C}</style></head><body><div>'
            'Account setup is closed (a user already exists, or no valid setup token).<br><br>'
            'To create the first account, open the setup link printed when the server started,<br>'
            'or run <code style="color:#FCD34D">clanker serve-user add &lt;name&gt;</code> on the host.<br><br>'
            '<a href="/auth/login">Go to login →</a></div></body></html>')


async def handle_register(request):
    """GET /auth/register?token=… — first-account creation form (0 users only)."""
    if webauth.user_count() > 0:
        raise web.HTTPFound("/auth/login")
    token = request.query.get("token", "")
    if not _bootstrap_open() or not hmac.compare_digest(token, _bootstrap_token or ""):
        return web.Response(text=_bootstrap_closed_page(), content_type="text/html", status=403)
    return web.Response(text=_register_html(token), content_type="text/html")


async def handle_register_submit(request):
    """POST /auth/register — create the first user, then go to TOTP setup."""
    if webauth.user_count() > 0:
        raise web.HTTPFound("/auth/login")
    data = await request.post()
    token = data.get("token", "")
    if not _bootstrap_open() or not hmac.compare_digest(token, _bootstrap_token or ""):
        return web.Response(text=_bootstrap_closed_page(), content_type="text/html", status=403)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    confirm = data.get("confirm") or ""
    if password != confirm:
        return web.Response(text=_register_html(token, "Passwords do not match."),
                            content_type="text/html", status=400)
    try:
        webauth.add_user(username, password)
    except ValueError as e:
        return web.Response(text=_register_html(token, str(e)),
                            content_type="text/html", status=400)
    log.info("First account created via web bootstrap: %s", username)
    resp = web.HTTPFound("/auth/setup")
    resp.set_cookie("clanker_setup", _sign_setup(username),
                    max_age=600, httponly=True, samesite="Lax", secure=_is_https(request))
    return resp


@web.middleware
async def security_middleware(request, handler):
    """Add security headers to all responses."""
    response = await handler(request)
    # WebSocket responses are already prepared by the time the handler returns;
    # setting headers on them is a no-op at best, so skip.
    if isinstance(response, web.WebSocketResponse):
        return response
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # The dashboard is a single live HTML doc with inline JS/CSS (no hashed asset
    # filenames), and the JSON APIs return fresh data every poll. Without this,
    # mobile browsers + the CDN cache the page heuristically and keep serving a
    # STALE build — which is why new terminal features didn't appear after deploy.
    # no-store forces a fresh fetch every load so a redeploy always takes effect.
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    # Build stamp so "did the deploy land?" is answerable with `curl -I`.
    response.headers["X-Clanker-Build"] = BUILD_ID
    if _is_https(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ══════════════════════════════════════════════════════════════════════════════
# Caching
# ══════════════════════════════════════════════════════════════════════════════

class Cache:
    """Simple TTL cache."""
    def __init__(self, ttl):
        self.ttl = ttl
        self._data = None
        self._time = 0

    def get(self):
        if time.time() - self._time < self.ttl:
            return self._data
        return None

    def set(self, data):
        self._data = data
        self._time = time.time()


_dashboard_cache = Cache(ttl=60)
_status_cache = Cache(ttl=2)


# ══════════════════════════════════════════════════════════════════════════════
# Tmux helpers
# ══════════════════════════════════════════════════════════════════════════════

def list_panes():
    """List all tmux panes with metadata."""
    try:
        fmt = "\t".join([
            "#{session_name}", "#{window_index}", "#{pane_index}",
            "#{pane_title}", "#{pane_current_command}",
            "#{pane_width}", "#{pane_height}", "#{session_attached}",
        ])
        out = subprocess.check_output(
            ["tmux", "list-panes", "-a", "-F", fmt],
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
        panes = []
        for line in out.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 8:
                continue
            session, win, pane, title, cmd, w, h, attached = parts
            if session.startswith("web-"):
                continue
            panes.append({
                "session": session,
                "window": int(win),
                "pane": int(pane),
                "target": f"{session}:{win}.{pane}",
                "title": title,
                "command": cmd,
                "width": int(w),
                "height": int(h),
                "attached": int(attached) > 0,
            })
        return panes
    except Exception as e:
        log.warning("Failed to list tmux panes: %s", e)
        return []


def capture_pane_tail(target, lines=5):
    """Capture last N lines of a tmux pane as plain text."""
    try:
        out = subprocess.check_output(
            ["tmux", "capture-pane", "-p", "-t", target, "-S", f"-{lines}"],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
        )
        return out.rstrip()
    except Exception:
        return ""


def _title_is_working(title):
    """True iff the pane title leads with Claude Code's animated working spinner.

    Claude renders an ANIMATED braille spinner (U+2800–U+28FF, frames like ⠐⠂⠄⡀)
    as the title's first glyph ONLY while actively generating; a finished session
    waiting for input shows a STATIC star (✳ U+2733) instead. Verified against the
    live fleet (2026-06-10): every working session cycled braille frames, every
    at-rest session showed ✳. The braille range is the whole signal — no body
    scraping (Claude uses ✳/✻ as static decoration in pane CONTENT, which is why
    the old content heuristic misclassified every session as 'working')."""
    t = (title or "").lstrip()
    return bool(t) and 0x2800 <= ord(t[0]) <= 0x28FF


def detect_session_state(pane):
    """Detect a session's state from its tmux pane.

    Returns 'working' | 'waiting' | 'idle':
      - non-claude pane (a bash shell, etc.)            -> idle
      - claude pane with the animated braille spinner   -> working
      - claude pane at rest (static star / no spinner)  -> waiting (ready for you)
    """
    if pane.get("command") != "claude":
        return "idle"
    return "working" if _title_is_working(pane.get("title", "")) else "waiting"


# Track active PTY bridges so the reaper doesn't kill them
_active_bridges = set()


def cleanup_web_sessions():
    """Kill orphaned web-* tmux sessions (not ones with active bridges)."""
    try:
        out = subprocess.check_output(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
        killed = 0
        for name in out.strip().split("\n"):
            if name.startswith("web-") and name not in _active_bridges:
                subprocess.run(
                    ["tmux", "kill-session", "-t", name],
                    capture_output=True, timeout=5,
                )
                killed += 1
        if killed:
            log.info("Cleaned up %d orphaned web sessions (skipped %d active)",
                     killed, len(_active_bridges))
    except Exception as e:
        log.debug("Cleanup error: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# PTY Bridge
# ══════════════════════════════════════════════════════════════════════════════

class PtyBridge:
    """Bridge between a WebSocket and a tmux session via a grouped session + PTY."""

    def __init__(self, target_session, cols=120, rows=40):
        self.target = target_session
        self.web_session = f"web-{uuid.uuid4().hex[:8]}"
        self.cols = cols
        self.rows = rows
        self.master_fd = None
        self.proc = None

    def start(self):
        """Create a grouped tmux session and attach via PTY."""
        # Set window-size to largest so the web client doesn't shrink the original terminal
        subprocess.run(
            ["tmux", "set-option", "-t", self.target, "window-size", "largest"],
            capture_output=True, timeout=3,
        )
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-t", self.target,
             "-s", self.web_session, "-x", str(self.cols), "-y", str(self.rows)],
            capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tmux new-session failed: {result.stderr.decode().strip()}")

        # Let tmux finish initializing the session
        time.sleep(0.15)

        # Verify session exists before attaching
        check = subprocess.run(
            ["tmux", "has-session", "-t", self.web_session],
            capture_output=True, timeout=3,
        )
        if check.returncode != 0:
            raise RuntimeError(f"Session {self.web_session} not found after creation")

        master_fd, slave_fd = pty.openpty()
        winsize = struct.pack("HHHH", self.rows, self.cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        self.proc = subprocess.Popen(
            ["tmux", "attach-session", "-t", self.web_session],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            preexec_fn=os.setsid,
            env=env,
        )
        os.close(slave_fd)

        self.master_fd = master_fd
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        _active_bridges.add(self.web_session)

    def read(self):
        try:
            return os.read(self.master_fd, 65536)
        except (OSError, BlockingIOError):
            return b""

    def write(self, data):
        if self.master_fd is not None:
            os.write(self.master_fd, data)

    def resize(self, cols, rows):
        self.cols = cols
        self.rows = rows
        if self.master_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    def close(self):
        _active_bridges.discard(self.web_session)
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                pass
            self.proc = None
        subprocess.run(
            ["tmux", "kill-session", "-t", self.web_session],
            capture_output=True, timeout=5,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

async def handle_index(request):
    """Serve the live dashboard with cached data."""
    from dashboard import _build_html
    from dashboard_data import generate_dashboard_data

    cached = _dashboard_cache.get()
    if cached:
        return web.Response(text=cached, content_type="text/html")

    data = generate_dashboard_data()
    data_json = json.dumps(data, indent=None)
    base_html = _build_html(data_json)
    html = base_html.replace("</body>", _live_features_html() + "\n</body>")
    _dashboard_cache.set(html)
    return web.Response(text=html, content_type="text/html")


async def handle_status(request):
    """Return current tmux session status (cached 2s)."""
    cached = _status_cache.get()
    if cached:
        return web.json_response(cached)

    panes = list_panes()
    sessions = []
    for p in panes:
        if p["command"] != "claude":
            continue
        state = detect_session_state(p)
        preview = capture_pane_tail(p["target"], lines=3)
        preview_line = ""
        for line in reversed(preview.split("\n")):
            s = line.strip()
            if s and "─" not in s and "❯" not in s and "═" not in s:
                preview_line = s[:120]
                break
        sessions.append({
            "session": p["session"],
            "target": p["target"],
            "title": p["title"],
            "command": p["command"],
            "state": state,
            "preview": preview_line,
            "size": f"{p['width']}x{p['height']}",
        })

    result = {"sessions": sessions, "ntfy_configured": bool(NTFY_TOPIC)}
    _status_cache.set(result)
    return web.json_response(result)


_panes_cache = Cache(ttl=2)

async def handle_panes(request):
    """Return captured content for all Claude panes (for tiled monitor view)."""
    cached = _panes_cache.get()
    if cached:
        return web.json_response(cached)

    panes = list_panes()
    result = []
    for p in panes:
        if p["session"].startswith("web-") or p["command"] != "claude":
            continue
        state = detect_session_state(p)
        content = capture_pane_tail(p["target"], lines=40)
        result.append({
            "session": p["session"],
            "target": p["target"],
            "state": state,
            "content": content,
            "width": p["width"],
            "height": p["height"],
        })

    _panes_cache.set(result)
    return web.json_response(result)


def capture_pane_ansi(target, scrollback=False, scrollback_lines=400):
    """Capture pane content with ANSI escape codes.

    If scrollback=True, captures the last `scrollback_lines` of history (not the
    whole buffer) so opening a long-lived session paints fast and the first
    WebSocket frame stays small on mobile. Otherwise captures only the visible screen.
    """
    try:
        cmd = ["tmux", "capture-pane", "-e", "-p", "-t", target]
        if scrollback:
            cmd.extend(["-S", f"-{int(scrollback_lines)}"])
        out = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, text=True, timeout=3,
        )
        return out
    except Exception:
        return ""


# Both WS endpoints reach a `claude --dangerously-skip-permissions` shell, so guard
# hard. These limits + allowlist + Origin check are the terminal-bridge hardening.
MAX_BRIDGES = 16         # concurrent PTY bridges (each = tmux session + PTY + child); enough for a tiled grid
MAX_VIEWS = 16           # concurrent capture-pane viewers
_view_count = 0
# Only these exact tmux key tokens may be sent on the non-literal "key" path. Anything
# else (especially a value starting with "-", e.g. "-X") would be argument-injected
# into `tmux send-keys` as a flag.
_ALLOWED_KEYS = frozenset({
    "Enter", "BSpace", "Tab", "Escape", "Space", "Up", "Down", "Left", "Right",
    "Home", "End", "PPage", "NPage", "IC", "DC",
    "C-c", "C-d", "C-z", "C-l", "C-a", "C-e", "C-u", "C-k", "C-w", "C-r", "C-p", "C-n",
})


def _ws_origin_ok(request):
    """Fail-closed CSWSH guard for WebSocket upgrades: the Origin host must match our
    own host (or the configured BASE_URL). A missing Origin is REJECTED — every
    browser sends Origin on a WS handshake, so absence means a non-browser/forged
    client. Without this, a logged-in operator visiting a malicious page could have
    it open a socket and drive the shell (cookie rides the cross-site handshake)."""
    from urllib.parse import urlparse
    origin = request.headers.get("Origin", "")
    if not origin:
        return False
    host = urlparse(origin).hostname
    allowed = {request.host.split(":")[0]}
    if BASE_URL:
        allowed.add(urlparse(BASE_URL).hostname)
    return host in allowed


# New-session names: a leading alnum then alnum/_/- , <=32 chars. Rejects anything
# that could flag-inject into `tmux new-session` (no leading '-', no spaces/specials).
_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


async def handle_session_new(request):
    """Create a fresh detached tmux session from the WebUI (claude or bare shell).

    Default: launch `claude --dangerously-skip-permissions` with
    CLAUDE_CODE_DISABLE_SANDBOX=1 so the new session is immediately usable and
    shows up in Live Sessions. `{"shell": true}` makes it a bare login shell
    instead. The session is anchored at $HOME. Behind the same 3-factor auth as
    the terminal bridge (this is no more privileged than the existing terminal
    access, which already reaches a skip-permissions claude)."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    name = (data.get("name") or "").strip()
    bare_shell = bool(data.get("shell"))
    if not name:
        name = ("sh-" if bare_shell else "cc-") + uuid.uuid4().hex[:6]
    if name.startswith("web-") or not _SESSION_NAME_RE.match(name):
        return web.json_response({"error": "invalid session name"}, status=400)

    loop = asyncio.get_event_loop()

    def _create():
        chk = subprocess.run(["tmux", "has-session", "-t", name],
                             capture_output=True, timeout=5)
        if chk.returncode == 0:
            return {"error": "session already exists"}
        home = os.path.expanduser("~")
        r = subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "-c", home, "-x", "120", "-y", "40"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return {"error": (r.stderr or "tmux new-session failed").strip()[:200]}
        if not bare_shell:
            # Launch claude with the sandbox disabled + permissions skipped. tmux
            # send-keys -l -- sends the line literally (no flag-injection); a
            # separate Enter submits it.
            launch = ("CLAUDE_CODE_DISABLE_SANDBOX=1 "
                      "claude --dangerously-skip-permissions")
            subprocess.run(["tmux", "send-keys", "-t", name, "-l", "--", launch],
                           capture_output=True, timeout=5)
            subprocess.run(["tmux", "send-keys", "-t", name, "Enter"],
                           capture_output=True, timeout=5)
        return {"ok": True, "session": name, "shell": bare_shell}

    result = await loop.run_in_executor(None, _create)
    if result.get("error"):
        status = 409 if "exists" in result["error"] else 400
        return web.json_response(result, status=status)
    log.info("New %s session %s created by %s",
             "shell" if bare_shell else "claude", name, request.get("user", "?"))
    return web.json_response(result)


# Native system monitor — the dashboard's floating compute-load view watches
# jangmojib (the Proxmox host this VM runs on). This is strictly READ-ONLY: the
# sampler (lib/sysmon_sampler.py) reads /proc + /sys + `df` over SSH, sleeps 1s for
# rate deltas, prints JSON, and exits. It sends NO signals, writes NO files, and
# kills NO processes. The SSH target, key, and script are fixed constants — no web
# input ever reaches the host (no injection surface).
SYSMON_SSH_HOST = os.environ.get("CLANKER_SYSMON_SSH", "")  # e.g. root@<hypervisor>; sysmon disabled when unset
SYSMON_SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sysmon_sampler.py")) as _f:
    SYSMON_SCRIPT = _f.read()
# Sample takes ~1.3s (1s delta + SSH); cache so rapid client polls don't stack
# multiple SSH samples on the host.
_sysmon_cache = Cache(ttl=2.5)


async def handle_sysmon(request):
    """Return a one-shot system snapshot of the Proxmox host as JSON (read-only).

    Runs lib/sysmon_sampler.py on the host via `ssh … python3 -` (script fed on
    stdin — a fixed constant, never anything from the request). Cached 2.5s."""
    cached = _sysmon_cache.get()
    if cached is not None:
        return web.json_response(cached)

    loop = asyncio.get_event_loop()

    def _sample():
        try:
            p = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ConnectTimeout=8", "-i", SYSMON_SSH_KEY, SYSMON_SSH_HOST,
                 "python3", "-"],
                input=SYSMON_SCRIPT, capture_output=True, text=True, timeout=12,
            )
        except subprocess.SubprocessError as e:
            return {"error": f"sample failed: {type(e).__name__}"}
        if p.returncode != 0:
            return {"error": (p.stderr or "ssh sample failed").strip()[:200]}
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError:
            return {"error": "sampler returned non-JSON"}

    data = await loop.run_in_executor(None, _sample)
    if not isinstance(data, dict) or data.get("error"):
        return web.json_response(
            data if isinstance(data, dict) else {"error": "no data"}, status=502)
    _sysmon_cache.set(data)
    return web.json_response(data)


MAX_SYSMON_STREAMS = 4   # concurrent real-time monitor streams (each = one SSH loop)
_sysmon_streams = 0


async def handle_ws_sysmon(request):
    """Stream real-time system snapshots over a WebSocket. Opens ONE persistent
    `ssh … python3 - loop` running the sampler in a loop on the host and forwards
    each JSON line. Still strictly read-only. The remote sampler self-exits the
    moment this socket closes (its next stdout write breaks) — verified no orphan."""
    global _sysmon_streams
    if not _ws_origin_ok(request):
        return web.Response(status=403, text="Origin not allowed")
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    if _sysmon_streams >= MAX_SYSMON_STREAMS:
        await ws.send_str(json.dumps({"error": "too many monitor streams"}))
        await ws.close()
        return ws
    _sysmon_streams += 1

    proc = None
    sent = 0
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
            "-i", SYSMON_SSH_KEY, SYSMON_SSH_HOST, "python3", "-", "loop", "1.0",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        proc.stdin.write(SYSMON_SCRIPT.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        while not ws.closed:
            line = await proc.stdout.readline()
            if not line:
                break  # ssh/sampler exited (host unreachable, etc.)
            line = line.strip()
            if line:
                await ws.send_str(line.decode("utf-8", "replace"))
                sent += 1
        if sent == 0 and not ws.closed:
            await ws.send_str(json.dumps({"error": "sampler unreachable"}))
    except Exception as e:
        log.debug("ws_sysmon error: %s", e)
    finally:
        _sysmon_streams -= 1
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError, Exception):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
    return ws


async def handle_view(request):
    """WebSocket handler for non-invasive terminal viewing via capture-pane + send-keys.

    No grouped sessions. No PTY bridge. Zero impact on original terminal sizing.
    Streams ANSI content every 200ms, accepts input as send-keys.
    """
    global _view_count
    if not _ws_origin_ok(request):
        return web.Response(status=403, text="Origin not allowed")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    session_name = request.match_info["session"]

    valid_sessions = {p["session"]: p["target"] for p in list_panes() if not p["session"].startswith("web-")}
    if session_name not in valid_sessions:
        await ws.close(message=b"Invalid session")
        return ws
    if _view_count >= MAX_VIEWS:
        await ws.close(message=b"Too many active viewers")
        return ws
    _view_count += 1

    target = valid_sessions[session_name]
    log.info("View opened: %s (capture-pane mode)", session_name)

    # Tell the client whether this pane is a full-screen TUI (on the alternate
    # screen — Claude, vim, btop…). Only those handle mouse-wheel scrolling, so the
    # client uses wheel events (smooth, line-granular) for them and plain native
    # scroll for a shell — a shell never receives stray mouse bytes.
    try:
        _alt = subprocess.run(["tmux", "display", "-p", "-t", target, "#{alternate_on}"],
                              capture_output=True, text=True, timeout=3)
        is_tui = _alt.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        is_tui = False
    try:
        await ws.send_str(json.dumps({"type": "meta", "tui": is_tui}))
    except Exception:
        pass

    last_content = ""

    async def capture_reader():
        # Live stream is the VISIBLE screen only (small + frequent). Adaptive cadence:
        # ~20 fps while the pane is actively changing (scrolling / generating) so
        # motion is smooth, backing off to ~5 fps once it's been still — light on
        # CPU + battery when idle.
        nonlocal last_content
        loop = asyncio.get_event_loop()
        idle = 0
        while not ws.closed:
            try:
                content = await loop.run_in_executor(None, capture_pane_ansi, target, False)
                if content != last_content:
                    last_content = content
                    await ws.send_str(json.dumps({"type": "content", "data": content}))
                    idle = 0
                else:
                    idle += 1
                await asyncio.sleep(0.05 if idle < 10 else 0.2)
            except Exception:
                break

    read_task = asyncio.create_task(capture_reader())

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get("type") == "keys":
                        # Literal text. -l makes it literal; -- ends option parsing so
                        # text starting with '-' can never be read as a flag.
                        keys = data.get("data", "")
                        if isinstance(keys, str) and keys:
                            subprocess.run(
                                ["tmux", "send-keys", "-t", target, "-l", "--", keys],
                                capture_output=True, timeout=2,
                            )
                    elif data.get("type") == "key":
                        # Named key — MUST be on the allowlist (else it's flag injection).
                        key = data.get("data", "")
                        if key in _ALLOWED_KEYS:
                            subprocess.run(
                                ["tmux", "send-keys", "-t", target, "--", key],
                                capture_output=True, timeout=2,
                            )
                except (json.JSONDecodeError, KeyError):
                    pass
    except Exception as e:
        log.warning("View WebSocket error for %s: %s", session_name, e)
    finally:
        _view_count -= 1
        read_task.cancel()
        log.info("View closed: %s", session_name)

    return ws


async def handle_terminal(request):
    """WebSocket handler for terminal interaction via PTY bridge."""
    if not _ws_origin_ok(request):       # fail-closed CSWSH guard (see _ws_origin_ok)
        return web.Response(status=403, text="Origin not allowed")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    session_name = request.match_info["session"]

    # Validate session exists and isn't a web-* session
    valid_sessions = {p["session"] for p in list_panes()}
    if session_name not in valid_sessions or session_name.startswith("web-"):
        await ws.close(message=b"Invalid session")
        return ws

    if len(_active_bridges) >= MAX_BRIDGES:
        log.warning("Bridge cap reached (%d) — refusing %s", MAX_BRIDGES, session_name)
        await ws.close(message=b"Too many active terminals")
        return ws

    log.info("Terminal opened: %s", session_name)
    bridge = PtyBridge(session_name, cols=120, rows=40)
    try:
        bridge.start()
    except Exception as e:
        log.error("PTY bridge failed: %s", e)
        bridge.close()                   # reap any partially-created tmux session / PTY
        await ws.close(message=str(e).encode())
        return ws

    async def pty_reader():
        loop = asyncio.get_event_loop()
        while not ws.closed:
            try:
                data = await loop.run_in_executor(None, bridge.read)
                if data:
                    await ws.send_bytes(data)
                else:
                    await asyncio.sleep(0.02)
            except Exception:
                break

    read_task = asyncio.create_task(pty_reader())

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                bridge.write(msg.data)
            elif msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get("type") == "resize":
                        cols = max(1, min(1000, int(data["cols"])))
                        rows = max(1, min(1000, int(data["rows"])))
                        bridge.resize(cols, rows)
                    elif data.get("type") == "input":
                        bridge.write(data["data"].encode("utf-8"))
                except (json.JSONDecodeError, KeyError, ValueError, struct.error):
                    bridge.write(msg.data.encode("utf-8"))
    except Exception as e:
        log.warning("WebSocket error for %s: %s", session_name, e)
    finally:
        read_task.cancel()
        bridge.close()
        log.info("Terminal closed: %s", session_name)

    return ws


# ══════════════════════════════════════════════════════════════════════════════
# Background tasks
# ══════════════════════════════════════════════════════════════════════════════

# A session must sit continuously 'waiting' at least this long before it earns a
# notification. This debounces the brief non-spinner frames a working session shows
# between tool calls / thinking steps (the source of the false-firing) — a real
# "waiting for you" persists; a momentary pause clears well under this window.
NOTIFY_STABLE_SECS = 12

# Optional throttle: minimum seconds between pings for the SAME session. Default
# OFF (0) — `monitor_sessions` already fires at most once per working→waiting
# episode, so a session is never pinged twice for one wait. Set this >0 only if
# you publish to a QUOTA'D backend (e.g. ntfy.sh's free tier, ~250/day per source
# IP): it bounds a chatty session's daily volume by collapsing rapid re-waits.
# A self-hosted server (and Android instant delivery) has no such cap, so leaving
# it at 0 maximizes how promptly you hear about every genuine wait.
NOTIFY_COOLDOWN_SECS = int(os.environ.get("CLANKER_NTFY_COOLDOWN", "0"))


async def monitor_sessions(app):
    """Poll Claude sessions and ntfy when one settles into 'waiting' for the user.

    Three gates kill the false-firing:
      1. TRANSITION — only an observed working→waiting edge arms a notification.
         Sessions already waiting when the monitor starts are seeded as handled, so
         a dashboard restart never bursts a ping for every idle session.
      2. DEBOUNCE — the session must stay waiting NOTIFY_STABLE_SECS before the ping,
         so a brief non-spinner frame between tool calls never fires.
      3. ONCE-PER-EPISODE — one ping per wait; it re-arms only after it works again."""
    prev_state = {}        # session -> last observed state
    waiting_since = {}     # session -> ts it entered the current waiting episode
    notified = set()       # sessions already pinged for their current episode
    last_ping = {}         # session -> ts of last DELIVERED ping (cooldown)
    muted_until = 0.0      # quota backoff: no publish attempts before this ts

    async with ClientSession() as http:
        while True:
            await asyncio.sleep(5)
            try:
                panes = [p for p in list_panes() if p["command"] == "claude"]
                live = {p["session"] for p in panes}
                now = time.time()

                # Drop bookkeeping for sessions that disappeared.
                for sid in [s for s in prev_state if s not in live]:
                    prev_state.pop(sid, None)
                    waiting_since.pop(sid, None)
                    last_ping.pop(sid, None)
                    notified.discard(sid)

                for p in panes:
                    sid = p["session"]
                    state = detect_session_state(p)
                    was = prev_state.get(sid)
                    prev_state[sid] = state

                    if state != "waiting":
                        # working/idle ends the episode: reset so the NEXT genuine
                        # wait can notify again.
                        waiting_since.pop(sid, None)
                        notified.discard(sid)
                        continue

                    if sid not in waiting_since:
                        waiting_since[sid] = now
                        # First time we ever see this session and it's ALREADY
                        # waiting (monitor just started / session just appeared):
                        # suppress — we never witnessed it working, so this isn't a
                        # fresh "done" event worth a ping.
                        if was is None:
                            notified.add(sid)

                    if sid in notified or now - waiting_since[sid] < NOTIFY_STABLE_SECS:
                        continue
                    notified.add(sid)

                    if not NTFY_TOPIC:
                        continue
                    # One ping per session per cooldown window — ntfy.sh's daily
                    # per-IP quota can't absorb a ping for every work→wait cycle.
                    if now - last_ping.get(sid, 0.0) < NOTIFY_COOLDOWN_SECS:
                        continue
                    if now < muted_until:
                        continue
                    preview = capture_pane_tail(p["target"], lines=4)
                    last_line = ""
                    for line in reversed(preview.split("\n")):
                        s = line.strip()
                        if s and "─" not in s and "❯" not in s and "│" not in s:
                            last_line = s[:100]
                            break
                    headers = {
                        "Title": f"{sid} needs input",
                        "Priority": "high",
                        "Tags": "robot",
                    }
                    if NTFY_TOKEN:
                        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
                    try:
                        async with http.post(
                            f"{NTFY_SERVER}/{NTFY_TOPIC}",
                            data=last_line or "Claude Code is waiting for input",
                            headers=headers,
                            timeout=10,
                        ) as resp:
                            # "Notified" only on evidence — a 429 (daily quota) or
                            # any other rejection must be loud, never logged as sent.
                            if resp.status == 200:
                                last_ping[sid] = now
                                log.info("Notified: %s", sid)
                            elif resp.status == 429:
                                muted_until = now + 1800
                                log.warning(
                                    "ntfy daily quota hit (429) — muting publishes 30 min")
                            else:
                                log.warning("ntfy rejected (%s): %s", resp.status,
                                            (await resp.text())[:200])
                    except Exception as e:
                        log.warning("ntfy failed: %s", e)
            except Exception as e:
                log.debug("Monitor error: %s", e)


async def session_reaper(app):
    """Periodically clean up orphaned web-* tmux sessions."""
    while True:
        await asyncio.sleep(120)
        try:
            cleanup_web_sessions()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Orchestration web controls (optional subsystem — lib/orch; OFF by default)
# ══════════════════════════════════════════════════════════════════════════════

def _orch():
    """Lazy import of the orchestration package; None if unavailable."""
    try:
        import orch
        return orch
    except Exception:
        return None


async def handle_orch_state(request):
    o = _orch()
    if not o:
        return web.json_response({"available": False})
    from orch import store, control
    return web.json_response({
        "available": True,
        "config": control.get_config(),
        "sessions": store.list_sessions(),
        "backlog": store.list_backlog(),
        "events": store.recent_events(20),
    })


async def handle_orch_config(request):
    if not _orch():
        return web.json_response({"error": "unavailable"}, status=400)
    from orch import control
    data = await request.json()
    updates = {k: v for k, v in (data or {}).items() if k in control.DEFAULTS}
    cfg = control.set_config(**updates)
    log.info("orch config set by %s: %s", request.get("user", "?"), updates)
    return web.json_response({"config": cfg})


async def handle_orch_spawn(request):
    if not _orch():
        return web.json_response({"error": "unavailable"}, status=400)
    from orch import control, spawn as orch_spawn
    if not control.enabled():
        return web.json_response({"error": "orchestration is off"}, status=400)
    data = await request.json()
    task = (data.get("task") or "").strip()
    if not task:
        return web.json_response({"error": "task required"}, status=400)
    loop = asyncio.get_event_loop()
    rec = await loop.run_in_executor(
        None, lambda: orch_spawn.spawn(task, project=data.get("project") or None,
                                       headless=bool(data.get("headless"))))
    log.info("orch spawn by %s: %s", request.get("user", "?"), task[:80])
    return web.json_response(rec)


async def handle_orch_session_action(request):
    if not _orch():
        return web.json_response({"error": "unavailable"}, status=400)
    from orch import spawn as orch_spawn, store
    sid, action = request.match_info["id"], request.match_info["action"]
    if not store.get_session(sid):
        return web.json_response({"error": "no such session"}, status=404)
    if action == "stop":
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(None, lambda: orch_spawn.stop(sid))
        log.info("orch stop %s by %s", sid[:10], request.get("user", "?"))
        return web.json_response({"ok": ok})
    return web.json_response({"error": f"unknown action {action}"}, status=400)


async def orch_supervise(app):
    """Background supervise loop — a no-op every tick unless orchestration is enabled.
    Toggling it on from the WebUI starts real passes within ~8s; off stops them."""
    while True:
        await asyncio.sleep(8)
        try:
            if not _orch():
                continue
            from orch import control, daemon as orch_daemon
            if not control.enabled():
                continue
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, orch_daemon.supervise_once)
        except Exception as e:
            log.debug("orch supervise error: %s", e)


async def start_background(app):
    cleanup_web_sessions()
    app["monitor_task"] = asyncio.create_task(monitor_sessions(app))
    app["reaper_task"] = asyncio.create_task(session_reaper(app))
    app["orch_task"] = asyncio.create_task(orch_supervise(app))


async def stop_background(app):
    for key in ("monitor_task", "reaper_task", "orch_task"):
        task = app.get(key)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    cleanup_web_sessions()


# ══════════════════════════════════════════════════════════════════════════════
# Live features HTML injection
# ══════════════════════════════════════════════════════════════════════════════

def _live_features_html():
    return '''
<!-- ─── Live Features: xterm.js + terminal + notifications ─── -->
<link rel="stylesheet" href="/vendor/xterm.min.css">
<script defer src="/vendor/xterm.min.js"></script>
<script defer src="/vendor/addon-fit.min.js"></script>
<script defer src="/vendor/addon-web-links.min.js"></script>
<!-- Pretext available at https://esm.sh/@chenglou/pretext@0.0.4 — will be used when we move to Canvas renderer -->

<style>
.session-card {
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 0; border-bottom: 1px solid rgba(87, 83, 78, 0.3);
  cursor: pointer; transition: background 0.15s;
}
.session-card:hover { background: rgba(255,255,255,0.02); padding-left: 8px; padding-right: 8px; margin: 0 -8px; }
.fav-star { cursor: pointer; color: var(--border); font-size: 16px; line-height: 1; margin-right: 10px; flex-shrink: 0; transition: color 0.15s; }
.fav-star.on { color: var(--accent-amber); }
.fav-star:hover { color: var(--accent-amber); }
.session-info { flex: 1; min-width: 0; }
.session-name { font-family: var(--font-mono); font-size: 13px; color: var(--accent-cream); }
.session-preview {
  font-family: var(--font-mono); font-size: 10px; color: var(--text-muted);
  margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.session-badge {
  font-family: var(--font-mono); font-size: 9px; text-transform: uppercase;
  letter-spacing: 0.1em; padding: 3px 8px; flex-shrink: 0; margin-left: 12px;
}
.session-badge.working { color: var(--accent-amber); border: 1px solid var(--accent-amber); }
.session-badge.waiting { color: var(--bg-void); background: var(--accent-olive); }
.session-badge.idle { color: var(--text-muted); border: 1px solid var(--border); }

.terminal-overlay {
  position: fixed; left: 0; top: 0; width: 100%; height: 100%;
  background: rgba(12, 10, 9, 0.95);
  z-index: 1000; display: none; flex-direction: column;
}
.terminal-overlay.open { display: flex; }
.terminal-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 4px 10px; background: var(--bg-deep);
  border-bottom: 1px solid var(--accent-terracotta); flex-shrink: 0;
}
.terminal-header h3 {
  font-family: var(--font-mono); font-size: 0.85rem;
  color: var(--accent-cream); font-weight: 500;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 50vw;
}
.terminal-header .session-state {
  font-family: var(--font-mono); font-size: 9px; color: var(--text-muted); margin-left: 8px;
}
.terminal-header .hdr-actions { display: flex; align-items: center; gap: 4px; }
.terminal-hdr-btn {
  cursor: pointer; color: var(--text-muted); font-size: 15px; line-height: 1;
  padding: 4px 7px; background: var(--bg-surface); border: 1px solid var(--border);
}
.terminal-hdr-btn:hover { color: var(--accent-cream); border-color: var(--accent-terracotta); }
.terminal-close {
  cursor: pointer; color: var(--text-muted); font-size: 22px;
  line-height: 1; padding: 2px 6px; transition: color 0.15s;
}
.terminal-close:hover { color: var(--accent-red); }
.terminal-body { flex: 1; overflow: hidden; background: #0C0A09; padding: 4px; }
.live-terminal-pre {
  font-family: var(--font-mono); font-size: 14px; line-height: 1.35;
  color: #FAFAF9; background: #0C0A09; padding: 8px 12px;
  white-space: normal; overflow-y: auto; overflow-x: hidden; height: 100%; margin: 0; box-sizing: border-box;
  outline: none; cursor: text; contain: content;
  scrollbar-width: thin; scrollbar-color: var(--border) transparent;
  -webkit-overflow-scrolling: touch; overscroll-behavior: contain;
}
.live-terminal-pre:focus { box-shadow: inset 0 0 0 1px var(--accent-terracotta); }
/* TUI panes: the frame can be TALLER than a phone viewport (a 50-row pane plus
   wrapped lines), so native pan stays enabled to reach the top of the CURRENT
   frame — a brand-new session has no transcript to page, which made its banner
   unreachable when native scroll was disabled. App-level paging (wheel SGR)
   engages only at the frame's edges (see the wheel/touch handlers);
   overscroll-behavior keeps momentum from rubber-banding the page. */
.live-terminal-pre.tui-view { overflow-y: auto; touch-action: pan-y; overscroll-behavior: contain; }
/* Per-line wrap policy (adaptive): prose lines wrap to the screen; decoration
   lines (long horizontal rules / box borders) clip to one row instead of
   wrapping into several repeated rows of the same character. */
.live-terminal-pre .tline { white-space: pre-wrap; overflow-wrap: anywhere; min-height: 1.15em; }
.live-terminal-pre .tline.rule { white-space: nowrap; overflow: hidden; }
.live-terminal-pre .tline.tfoot { white-space: pre; overflow: hidden; text-overflow: ellipsis; }
.live-terminal-pre .tline.todo-sum { opacity: 0.5; font-style: italic; }
@media (max-width: 768px) { .live-terminal-pre { font-size: 12px; } }
/* Jump-to-live pill — appears when you've scrolled up into history. */
.jump-live {
  position: absolute; right: 14px; bottom: 12px; z-index: 5;
  background: var(--accent-olive); color: var(--bg-void); border: none;
  font-family: var(--font-mono); font-size: 11px; font-weight: 700;
  padding: 7px 14px; border-radius: 16px; cursor: pointer; display: none;
  box-shadow: 0 2px 10px rgba(0,0,0,0.5); letter-spacing: 0.03em;
}
.jump-live.show { display: block; }
.terminal-body { position: relative; }
/* Hidden capture textarea: tap the terminal to type directly into the session
   (no reply bar). Invisible but IN-PLACE (bottom of the terminal body) so the
   browser never has anything off-canvas to scroll into view on focus, and
   font-size 16px so iOS doesn't auto-zoom the page when it gains focus. */
.terminal-input-capture {
  position: absolute; left: 0; bottom: 0; width: 1px; height: 1px;
  opacity: 0; border: 0; padding: 0; resize: none; font-size: 16px;
}
.live-terminal-pre::-webkit-scrollbar { width: 6px; }
.live-terminal-pre::-webkit-scrollbar-track { background: transparent; }
.live-terminal-pre::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.mobile-input {
  display: none; gap: 4px; padding: 4px 6px; flex-wrap: nowrap; align-items: center;
  background: var(--bg-deep); border-top: 1px solid var(--border); flex-shrink: 0;
  overflow-x: auto; -webkit-overflow-scrolling: touch;
}
@media (max-width: 768px) { .mobile-input { display: flex; } }
.mobile-input button.signal { flex: 1 0 auto; min-width: 34px; padding: 7px 6px; }
.mobile-input button {
  background: var(--bg-surface); color: var(--text-secondary); border: 1px solid var(--border);
  padding: 7px 10px; font-family: var(--font-mono); font-size: 12px; line-height: 1;
  letter-spacing: 0.03em; cursor: pointer; transition: background 0.15s; white-space: nowrap;
}
.mobile-input button:hover { background: #a33a0a; color: var(--accent-cream); }
.mobile-input button.signal { background: var(--bg-surface); color: var(--text-secondary); }
.mobile-input button.signal:hover { background: var(--accent-red); color: white; }

/* New-session modal */
.newsess-overlay {
  position: fixed; inset: 0; background: rgba(12,10,9,0.75); z-index: 1100;
  display: none; align-items: center; justify-content: center;
}
.newsess-overlay.open { display: flex; }
.newsess-box {
  background: var(--bg-deep); border: 1px solid var(--accent-terracotta);
  padding: 20px; width: min(420px, 92vw); font-family: var(--font-mono);
}
.newsess-box h3 {
  font-family: var(--font-display); font-size: 1.25rem; color: var(--accent-cream);
  font-weight: 400; margin: 0 0 14px;
}
.newsess-box label.row { display: block; font-size: 11px; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 6px; }
.newsess-box input[type=text] {
  width: 100%; box-sizing: border-box; background: var(--bg-panel);
  border: 1px solid var(--border); color: var(--text-primary);
  font-family: var(--font-mono); font-size: 16px; padding: 10px 12px;
  outline: none; margin-bottom: 14px;
}
.newsess-box input[type=text]:focus { border-color: var(--accent-terracotta); }
.newsess-box label.chk {
  display: flex; align-items: center; gap: 8px; font-size: 12px;
  color: var(--text-secondary); margin-bottom: 18px; cursor: pointer;
}
.newsess-box label.chk input { width: 16px; height: 16px; }
.newsess-box .hint { font-size: 10px; color: var(--text-muted); margin: -8px 0 16px; line-height: 1.4; }
.newsess-actions { display: flex; gap: 8px; justify-content: flex-end; }
.newsess-actions button {
  font-family: var(--font-mono); font-size: 11px; padding: 10px 18px;
  text-transform: uppercase; letter-spacing: 0.05em; cursor: pointer; border: none;
}
.newsess-actions .go { background: var(--accent-terracotta); color: var(--accent-cream); }
.newsess-actions .go:hover { background: #a33a0a; }
.newsess-actions .cancel { background: var(--bg-surface); color: var(--text-secondary); border: 1px solid var(--border); }

/* Floating system-monitor button — opens the native System Monitor overlay.
   Available from anywhere on the dashboard; sits under the overlays (z 1000). */
.fab-btop {
  position: fixed; right: 18px; bottom: 18px; z-index: 990;
  width: 54px; height: 54px; border-radius: 50%;
  background: var(--accent-terracotta); color: var(--accent-cream); border: none;
  font-family: var(--font-mono); font-size: 11px; font-weight: 700; letter-spacing: 0.04em;
  cursor: pointer; box-shadow: 0 4px 16px rgba(0,0,0,0.45);
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px;
  transition: background 0.15s, transform 0.1s;
}
.fab-btop:hover { background: #a33a0a; }
.fab-btop:active { transform: scale(0.94); }
.fab-btop .ic { font-size: 16px; line-height: 1; }
.fab-btop:disabled { opacity: 0.6; cursor: default; }
/* Hidden while any overlay is open — the terminal header carries its own sys
   button, so the monitor stays reachable from a terminal without the FAB
   floating over the control bar / live pill. */
body.overlay-open .fab-btop { display: none; }
@media (max-width: 768px) { .fab-btop { right: 14px; bottom: 14px; width: 48px; height: 48px; font-size: 9px; } }

/* ── Native System Monitor overlay (replaces btop-in-a-terminal on mobile) ── */
.sysmon-overlay {
  position: fixed; left: 0; top: 0; width: 100%; height: 100%;
  background: var(--bg-void); z-index: 1050; display: none; flex-direction: column;
}
.sysmon-overlay.open { display: flex; }
.sysmon-header {
  display: flex; align-items: center; gap: 10px; padding: 10px 16px;
  background: var(--bg-deep); border-bottom: 2px solid var(--accent-terracotta); flex-shrink: 0;
}
.sysmon-header h3 { font-family: var(--font-display); font-size: 1.25rem; color: var(--accent-cream); font-weight: 400; }
.sysmon-header .sm-host { font-family: var(--font-mono); font-size: 11px; color: var(--text-muted); }
.sysmon-header .sm-age { margin-left: auto; font-family: var(--font-mono); font-size: 10px; color: var(--text-muted); }
.sysmon-close { cursor: pointer; color: var(--text-muted); font-size: 26px; line-height: 1; padding: 2px 6px; }
.sysmon-close:hover { color: var(--accent-red); }
.sysmon-body { flex: 1; overflow-y: auto; padding: 12px; display: grid; gap: 12px;
  grid-template-columns: 1fr; -webkit-overflow-scrolling: touch; }
@media (min-width: 820px) { .sysmon-body { grid-template-columns: 1fr 1fr; } }
.sm-card { background: var(--bg-panel); border: 1px solid var(--border); padding: 12px 14px; }
.sm-card.wide { grid-column: 1 / -1; }
.sm-card h4 {
  font-family: var(--font-mono); font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em;
  color: var(--text-secondary); margin: 0 0 10px; display: flex; align-items: center; gap: 8px;
}
.sm-card h4 .sm-sub { margin-left: auto; color: var(--text-muted); letter-spacing: 0.04em; font-size: 10px; }
.sm-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.sm-chip {
  font-family: var(--font-mono); font-size: 11px; padding: 4px 9px;
  background: var(--bg-surface); color: var(--text-secondary); border: 1px solid var(--border);
}
.sm-chip b { color: var(--accent-cream); font-weight: 500; }
/* labelled progress bar */
.sm-bar-row { display: flex; align-items: center; gap: 8px; margin: 5px 0; font-family: var(--font-mono); font-size: 11px; }
.sm-bar-row .sm-lbl { flex: 0 0 auto; color: var(--text-muted); min-width: 38px; }
.sm-bar-row .sm-val { flex: 0 0 auto; color: var(--text-secondary); margin-left: auto; white-space: nowrap; }
.sm-bar { flex: 1 1 auto; height: 12px; background: var(--bg-void); border: 1px solid var(--border); overflow: hidden; position: relative; }
.sm-bar > span { display: block; height: 100%; background: var(--accent-olive); transition: width 0.4s ease; }
.sm-bar.warn > span { background: var(--accent-amber); }
.sm-bar.crit > span { background: var(--accent-red); }
/* per-core grid */
.sm-cores { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; }
@media (min-width: 520px) { .sm-cores { grid-template-columns: repeat(8, 1fr); } }
.sm-core { font-family: var(--font-mono); font-size: 9px; color: var(--text-muted); text-align: center; }
.sm-core .sm-cbar { height: 5px; background: var(--bg-void); border: 1px solid var(--border); margin-top: 2px; overflow: hidden; }
.sm-core .sm-cbar > span { display: block; height: 100%; background: var(--accent-olive); }
.sm-core .sm-cbar.warn > span { background: var(--accent-amber); }
.sm-core .sm-cbar.crit > span { background: var(--accent-red); }
/* process table */
.sm-proc-ctl { display: flex; gap: 6px; align-items: center; margin-bottom: 8px; }
.sm-proc-ctl input {
  flex: 1 1 auto; background: var(--bg-void); border: 1px solid var(--border); color: var(--text-primary);
  font-family: var(--font-mono); font-size: 13px; padding: 6px 8px; outline: none;
}
.sm-proc-ctl input:focus { border-color: var(--accent-terracotta); }
.sm-sort-btn {
  font-family: var(--font-mono); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em;
  padding: 6px 10px; background: var(--bg-surface); color: var(--text-muted); border: 1px solid var(--border); cursor: pointer;
}
.sm-sort-btn.active { background: var(--accent-terracotta); color: var(--bg-void); border-color: var(--accent-terracotta); }
.sm-proc { width: 100%; border-collapse: collapse; font-family: var(--font-mono); font-size: 11px; }
.sm-proc th { text-align: left; color: var(--text-muted); font-weight: 400; padding: 3px 6px; border-bottom: 1px solid var(--border); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; }
.sm-proc td { padding: 3px 6px; border-bottom: 1px solid rgba(87,83,78,0.18); color: var(--text-secondary); white-space: nowrap; }
.sm-proc td.cmd { color: var(--accent-cream); width: 99%; overflow: hidden; text-overflow: ellipsis; max-width: 0; }
.sm-proc td.num { text-align: right; }
.sm-proc td.hot { color: var(--accent-amber); }
.sm-err { font-family: var(--font-mono); font-size: 12px; color: var(--accent-red); padding: 10px; }

.notif-badge {
  position: fixed; top: 16px; right: 16px; background: var(--accent-olive);
  color: var(--bg-void); font-family: var(--font-mono); font-size: 11px;
  font-weight: 700; padding: 8px 14px; z-index: 999; cursor: pointer;
  display: none; letter-spacing: 0.05em;
}
.notif-badge.visible { display: block; animation: notifPulse 2s ease-in-out infinite; }
@keyframes notifPulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(101, 163, 13, 0.4); }
  50% { box-shadow: 0 0 0 8px rgba(101, 163, 13, 0); }
}
</style>

<div class="notif-badge" id="notif-badge" onclick="openWaitingSession()"></div>
<button class="fab-btop" id="fab-btop" onclick="openSysmon()" title="System monitor — hypervisor"><span class="ic">▤</span>sys</button>

<!-- Native System Monitor overlay -->
<div class="sysmon-overlay" id="sysmon-overlay">
  <div class="sysmon-header">
    <h3>System Monitor</h3>
    <span class="sm-host" id="sm-host">—</span>
    <span class="sm-age" id="sm-age"></span>
    <span class="sysmon-close" onclick="closeSysmon()">&times;</span>
  </div>
  <div class="sysmon-body" id="sysmon-body">
    <div class="sm-card wide"><div class="no-data">Loading…</div></div>
  </div>
</div>

<div class="terminal-overlay" id="terminal-overlay">
  <div class="terminal-header">
    <h3 id="terminal-title"></h3>
    <div class="hdr-actions">
      <span class="session-state" id="terminal-state"></span>
      <button class="terminal-hdr-btn" onclick="openSysmon()" title="System monitor">▤</button>
      <span class="terminal-close" onclick="closeTerminal()">&times;</span>
    </div>
  </div>
  <div class="terminal-body" id="terminal-body"></div>
  <div class="mobile-input">
    <button class="signal" id="key-esc" title="Escape · hold: hide/show todos">esc</button>
    <button class="signal" onclick="sendNamed('Tab')" title="Tab">tab</button>
    <button class="signal" onclick="viewScroll('up')" title="Scroll transcript up">⇞</button>
    <button class="signal" onclick="viewScroll('down')" title="Scroll transcript down">⇟</button>
    <button class="signal" onclick="sendNamed('Up')" title="Up">&uarr;</button>
    <button class="signal" onclick="sendNamed('Down')" title="Down">&darr;</button>
    <button class="signal" onclick="sendNamed('Left')" title="Left">&larr;</button>
    <button class="signal" onclick="sendNamed('Right')" title="Right">&rarr;</button>
    <button class="signal" onclick="sendNamed('Enter')" title="Enter">&crarr;</button>
    <button class="signal" onclick="sendNamed('C-c')" title="Ctrl+C">^C</button>
  </div>
</div>

<!-- New-session modal -->
<div class="newsess-overlay" id="newsess-overlay">
  <div class="newsess-box">
    <h3>New Session</h3>
    <label class="row" for="newsess-name">Name (optional)</label>
    <input type="text" id="newsess-name" placeholder="auto" autocomplete="off" autocapitalize="off" spellcheck="false">
    <label class="chk"><input type="checkbox" id="newsess-shell"> Bare shell (no Claude)</label>
    <div class="hint">Default launches <code>claude --dangerously-skip-permissions</code> with <code>CLAUDE_CODE_DISABLE_SANDBOX=1</code>, anchored at $HOME.</div>
    <div class="newsess-actions">
      <button class="cancel" onclick="closeNewSession()">Cancel</button>
      <button class="go" id="newsess-go" onclick="createNewSession()">Create &amp; Open</button>
    </div>
  </div>
</div>

<script>
let liveWs = null, liveSession = null;
let statusInterval = null, waitingSessions = [];

let livePanelEl = null;
// Place Live Sessions: on mobile, right above the Projects panel (operator wants
// it near the top); on desktop, in its original spot above the first 3-col grid.
function placeLivePanel() {
  if (!livePanelEl) return;
  const projectsPanel = document.querySelector('.panel[data-label="PROJECTS"]');
  const projectsGrid = projectsPanel ? projectsPanel.closest('.grid') : null;
  const grid3 = document.querySelectorAll('.grid-3')[0];
  if (isMobileView() && projectsGrid && projectsGrid.parentNode) {
    if (livePanelEl.nextElementSibling !== projectsGrid)
      projectsGrid.parentNode.insertBefore(livePanelEl, projectsGrid);
  } else if (grid3 && grid3.parentNode) {
    if (livePanelEl.nextElementSibling !== grid3)
      grid3.parentNode.insertBefore(livePanelEl, grid3);
  }
}

(function injectLivePanel() {
  const grid3 = document.querySelectorAll('.grid-3')[0];
  if (!grid3) return;
  const livePanel = document.createElement('div');
  livePanel.className = 'grid live-panel';
  livePanel.style.cssText = 'grid-template-columns:1fr; margin-bottom:2px;';
  livePanel.innerHTML = '<div class="panel" data-label="LIVE" style="animation-delay:0.5s"><h2>Live Sessions</h2><div id="live-sessions"></div></div>';
  livePanelEl = livePanel;
  grid3.parentNode.insertBefore(livePanel, grid3);
  placeLivePanel();
  // Re-place if the viewport crosses the mobile breakpoint (rotate / resize).
  if (window.matchMedia) {
    const mq = window.matchMedia('(max-width: 768px)');
    (mq.addEventListener ? mq.addEventListener.bind(mq, 'change') : mq.addListener.bind(mq))(placeLivePanel);
  }
  fetchStatus();
  statusInterval = setInterval(() => { if (!document.hidden) fetchStatus(); }, 5000);
  if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();
})();

async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    if (r.status === 401) { window.location = '/auth/login'; return; }
    const data = await r.json();
    renderLiveSessions(data.sessions);
    updateNotifications(data.sessions);
  } catch (e) {}
}

// ─── Favourites (per-browser, localStorage) — favourited sessions sort to the top ───
let favSessions = new Set(JSON.parse(localStorage.getItem('clanker_fav_sessions') || '[]'));
function saveFavs() { localStorage.setItem('clanker_fav_sessions', JSON.stringify([...favSessions])); }
function toggleFav(name, ev) {
  if (ev) ev.stopPropagation();
  favSessions.has(name) ? favSessions.delete(name) : favSessions.add(name);
  saveFavs();
  fetchStatus();   // re-render the list immediately
}
const STATE_RANK = { waiting: 0, working: 1, idle: 2 };
function favStateSort(a, b) {
  const fa = favSessions.has(a.session) ? 0 : 1, fb = favSessions.has(b.session) ? 0 : 1;
  if (fa !== fb) return fa - fb;                                  // favourites first
  return (STATE_RANK[a.state] ?? 3) - (STATE_RANK[b.state] ?? 3); // then by activity
}
function favStar(name) {
  const on = favSessions.has(name);
  return `<span class="fav-star ${on ? 'on' : ''}" title="Favourite" onclick="toggleFav('${name}', event)">${on ? '★' : '☆'}</span>`;
}

function renderLiveSessions(sessions) {
  const el = document.getElementById('live-sessions');
  if (!el) return;
  const sorted = sessions.filter(s => s.command === 'claude').sort(favStateSort);
  el.innerHTML = sorted.map(s => `
    <div class="session-card" onclick="openTerminal('${s.session}')">
      ${favStar(s.session)}
      <div class="session-info">
        <div class="session-name">${s.session}</div>
        <div class="session-preview">${escapeHtml(s.preview)}</div>
      </div>
      <span class="session-badge ${s.state}">${s.state}</span>
    </div>
  `).join('') || '<div class="no-data">No active Claude Code sessions</div>';
}

function updateNotifications(sessions) {
  const waiting = sessions.filter(s => s.state === 'waiting');
  const badge = document.getElementById('notif-badge');
  waitingSessions = waiting;
  if (waiting.length > 0) {
    badge.textContent = waiting.length + ' session' + (waiting.length > 1 ? 's' : '') + ' waiting';
    badge.classList.add('visible');
    if (!liveSession && 'Notification' in window && Notification.permission === 'granted') {
      new Notification('Clanker: Input needed', {
        body: waiting.map(s => s.session).join(', '), tag: 'clanker-input', renotify: true,
      });
    }
  } else { badge.classList.remove('visible'); }
}

function openWaitingSession() {
  if (waitingSessions.length > 0) openTerminal(waitingSessions[0].session);
}

let liveTerm = null, liveFit = null, liveResizeHandler = null;
let liveMode = null;   // 'pty' (xterm, desktop) | 'view' (capture, wraps, mobile)
let livePre = null;    // the <pre> element in view mode
let liveName = null;   // current session name (kept for reconnect)
let reconnectTimer = null, reconnectDelay = 600;

function setTermState(s) { const el = document.getElementById('terminal-state'); if (el) el.textContent = s; }

// Mobile browsers suspend a backgrounded tab and drop its WebSocket; without
// this the terminal looked dead until you exited and re-opened it. Reconnect
// whenever the socket closes while the terminal is still open, and immediately
// when the tab returns to the foreground.
function liveSocketDead() { return !liveWs || liveWs.readyState > 1; }  // 2=CLOSING 3=CLOSED
function reconnectLive() {
  if (!liveMode || !liveName) return;               // terminal was closed
  setTermState('reconnecting');
  (liveMode === 'view' ? connectView : connectPty)(liveName);
}
function scheduleReconnect() {
  if (!liveMode || !liveName || reconnectTimer) return;
  if (document.hidden) return;                       // reconnect on return instead
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (liveMode && liveName && liveSocketDead()) reconnectLive();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 5000);
}
function liveSendResize() {
  if (liveTerm && liveWs && liveWs.readyState === WebSocket.OPEN)
    liveWs.send(JSON.stringify({ type: 'resize', cols: liveTerm.cols, rows: liveTerm.rows }));
}

// Raw key→byte-sequence map for PTY mode (built via fromCharCode so no
// backslash-escapes live in the Python-embedded JS string).
const PTYSEQ = {
  'Enter':  String.fromCharCode(13),
  'Escape': String.fromCharCode(27),
  'Tab':    String.fromCharCode(9),
  'BSpace': String.fromCharCode(127),
  'Up':     String.fromCharCode(27) + '[A',
  'Down':   String.fromCharCode(27) + '[B',
  'Left':   String.fromCharCode(27) + '[D',
  'Right':  String.fromCharCode(27) + '[C',
  'PPage':  String.fromCharCode(27) + '[5~',
  'NPage':  String.fromCharCode(27) + '[6~',
  'C-c':    String.fromCharCode(3),
  'C-d':    String.fromCharCode(4),
};

function isMobileView() { return window.matchMedia('(max-width: 768px)').matches; }

function openTerminal(name) {
  liveSession = name; liveName = name;
  reconnectDelay = 600;
  document.getElementById('terminal-title').textContent = name;
  const overlay = document.getElementById('terminal-overlay');
  overlay.classList.add('open');
  updateFab();
  document.getElementById('terminal-body').innerHTML = '';
  applyTerminalViewport();
  // Mobile: capture-view (wraps in flight, never attaches/resizes the real
  // session). Desktop: xterm.js over the PTY bridge (true interactivity).
  if (isMobileView()) buildViewTerminal(name);
  else buildPtyTerminal(name);
}

// ── Desktop: xterm.js over the PTY bridge ──
function buildPtyTerminal(name) {
  liveMode = 'pty';
  const container = document.getElementById('terminal-body');
  const term = new Terminal({
    theme: XTERM_THEME, fontFamily: 'JetBrains Mono, ui-monospace, monospace',
    fontSize: 13, cursorBlink: true, scrollback: 5000, allowProposedApi: true,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  try { term.loadAddon(new WebLinksAddon.WebLinksAddon()); } catch (e) {}
  term.open(container);
  try { fit.fit(); } catch (e) {}
  liveTerm = term; liveFit = fit;
  // term/onData persist across reconnects; route input through the live socket.
  term.onData(d => { if (liveWs && liveWs.readyState === WebSocket.OPEN) liveWs.send(JSON.stringify({ type: 'input', data: d })); });
  term.onResize(() => liveSendResize());
  liveResizeHandler = () => { try { fit.fit(); liveSendResize(); } catch (e) {} };
  window.addEventListener('resize', liveResizeHandler);
  connectPty(name);
  setTimeout(() => { try { fit.fit(); liveSendResize(); } catch (e) {} }, 60);
}

function connectPty(name) {
  if (liveWs) { try { liveWs.close(); } catch (e) {} }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/terminal/${name}`);
  ws.binaryType = 'arraybuffer';
  liveWs = ws;
  ws.onopen = () => {
    reconnectDelay = 600; setTermState('connected');
    try { liveFit.fit(); } catch (e) {}
    liveSendResize();
    if (liveTerm) liveTerm.focus();
  };
  ws.onmessage = (e) => { if (liveTerm) liveTerm.write(typeof e.data === 'string' ? e.data : new Uint8Array(e.data)); };
  ws.onclose = () => { setTermState('disconnected'); scheduleReconnect(); };
  ws.onerror = () => { setTermState('error'); };
}

// ── Mobile: capture-pane view. Streams `capture-pane -e -p` (read-only) and
// renders ANSI per-line so prose wraps but decoration rules clip (adaptive wrap).
// Typing goes through a hidden textarea (tap the terminal) — no reply bar.
// SCROLLBACK: Claude Code (and other TUIs) run on the terminal's ALTERNATE screen,
// which has NO tmux scrollback — the transcript lives inside the app. So to scroll
// back we send the session its own PageUp/PagePown (Claude scrolls its transcript)
// and the next capture shows the earlier content. Works via the ⇞/⇟ keys, the
// mouse wheel, and a touch drag. ──
let viewPending = null, viewRaf = null, viewCapture = null, viewLastFrame = null;
let jumpLiveBtn = null, viewScrolledUp = false, viewIsTui = false;
let viewAppScrolled = false;   // the APP's transcript was paged up (vs native pan only)
let viewStickBottom = true;    // follow the live tail through size changes (keyboard)
let viewResizeObs = null;
let viewLastCH = 0;            // clientHeight at the last scroll event (resize detector)

function _viewRenderNow() {
  viewRaf = null;
  if (viewPending == null || !livePre) return;
  viewLastFrame = viewPending;   // kept so display toggles can re-render instantly
  // Stick-to-bottom: a new frame keeps the view anchored to the live tail, but
  // only if the user was already there — someone inspecting the top of the frame
  // (e.g. a fresh session's banner) must not be yanked back down every capture.
  const atBottom = livePre.scrollHeight - livePre.scrollTop - livePre.clientHeight < 24;
  livePre.innerHTML = renderAnsiToLines(viewPending);
  if (atBottom) livePre.scrollTop = livePre.scrollHeight;
  viewPending = null;
}
function _viewSchedule() {
  // Paint the latest frame on the next display refresh (rAF) — aligned to the
  // screen so motion is smooth, coalesced (only the newest frame is drawn), and
  // paused entirely while the tab is hidden (battery; repaints on return).
  if (document.hidden || viewRaf) return;
  viewRaf = requestAnimationFrame(_viewRenderNow);
}

function _setScrolled(v) {
  viewScrolledUp = v;
  if (jumpLiveBtn) jumpLiveBtn.classList.toggle('show', v);
}
// ⇞/⇟ buttons: pan the local frame first; once it's at its edge, page the app's
// own transcript (PageUp/PageDown). A fresh session with no transcript to page is
// still fully viewable via the local pan.
function viewScroll(dir) {
  if (livePre) {
    const room = dir === 'up'
      ? livePre.scrollTop > 4
      : livePre.scrollHeight - livePre.scrollTop - livePre.clientHeight > 4;
    if (room) {
      livePre.scrollTop += (dir === 'up' ? -0.9 : 0.9) * livePre.clientHeight;
      return;
    }
  }
  wsKey(dir === 'up' ? 'PPage' : 'NPage');
  if (dir === 'up') { viewAppScrolled = true; _setScrolled(true); }
}
// Return the VIEW to the live tail: anchor the local frame and re-arm
// stick-to-bottom so size changes (the soft keyboard opening/closing) keep the
// input line in sight. Used whenever the user acts on the session — typing is
// an implicit "show me where I'm typing".
function _viewSnapLive() {
  viewAppScrolled = false;
  viewStickBottom = true;
  if (livePre) livePre.scrollTop = livePre.scrollHeight;
  _setScrolled(false);
}
// Snap back to the live tail: page the app back down (only if its transcript was
// actually scrolled) and re-anchor the local frame.
function jumpToLive() {
  if (viewAppScrolled) for (let i = 0; i < 14; i++) wsKey('NPage');
  _viewSnapLive();
}

// ── line-smooth scroll: a wheel/drag sends the TUI mouse-wheel events (it scrolls
// its transcript line-by-line). `n` = notches, batched into one message. Gated to
// TUIs so a plain shell never receives stray mouse bytes. ──
function wsWheel(dir, n) {
  if (!viewIsTui || !liveWs || liveWs.readyState !== WebSocket.OPEN) return;
  const ESC = String.fromCharCode(27);
  const one = ESC + (dir === 'up' ? '[<64;1;1M' : '[<65;1;1M');
  liveWs.send(JSON.stringify({ type: 'keys', data: one.repeat(Math.max(1, Math.min(16, n))) }));
  if (dir === 'up') { viewAppScrolled = true; _setScrolled(true); }
}

// Coalesce wheel/drag input to ONE batched send per animation frame, so a fast
// flick can't flood the session with dozens of send-keys (which itself lags).
let wheelAccum = 0, wheelRaf = null;
function _flushWheel() {
  wheelRaf = null;
  if (!wheelAccum) return;
  const n = wheelAccum; wheelAccum = 0;
  wsWheel(n > 0 ? 'up' : 'down', Math.abs(n));   // +lines = scroll up (older)
}
function queueWheel(lines) {
  if (!viewIsTui || !lines) return;
  wheelAccum += lines;
  if (!wheelRaf) wheelRaf = requestAnimationFrame(_flushWheel);
}

function buildViewTerminal(name) {
  liveMode = 'view';
  viewPending = null; viewScrolledUp = false; viewAppScrolled = false; viewStickBottom = true;
  viewLastCH = 0;
  const container = document.getElementById('terminal-body');
  const pre = document.createElement('pre');
  pre.className = 'live-terminal-pre';
  pre.id = 'live-pre';
  container.appendChild(pre);
  livePre = pre;

  // "↓ live" pill — shown when scrolled up; tap to return to the live tail.
  const jl = document.createElement('button');
  jl.className = 'jump-live'; jl.textContent = '↓ live';
  jl.addEventListener('click', jumpToLive);
  container.appendChild(jl);
  jumpLiveBtn = jl;

  // Hidden textarea captures the soft keyboard; tapping the terminal focuses it.
  const ta = document.createElement('textarea');
  ta.className = 'terminal-input-capture';
  ta.setAttribute('autocomplete', 'off'); ta.setAttribute('autocapitalize', 'off');
  ta.setAttribute('autocorrect', 'off'); ta.setAttribute('spellcheck', 'false');
  container.appendChild(ta);
  viewCapture = ta;
  ta.addEventListener('input', () => { if (ta.value) { sendText(ta.value); ta.value = ''; } });
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendNamed('Enter'); ta.value = ''; }
    else if (e.key === 'Backspace' && !ta.value) { e.preventDefault(); sendNamed('BSpace'); }
  });

  // A tap (not a drag) focuses the input. `click` covers mouse/desktop; the
  // explicit tap detector in touchend covers mobile, where browsers' tap-to-click
  // heuristics get strict over a pan-y scroller (a few px of finger noise
  // suppresses the click). A tap = barely moved, quick, and didn't scroll.
  pre.addEventListener('click', () => {
    if (viewCapture) viewCapture.focus({ preventScroll: true });
  });
  let tapY = null, tapT = 0, tapST = 0;

  // The pill tracks the LOCAL scroll position too: visible whenever the view is
  // away from the live tail (natively panned up, or app transcript paged up).
  pre.addEventListener('scroll', () => {
    // A box resize (soft keyboard opening/closing) fires a scroll event with the
    // OLD scrollTop against the NEW geometry — that's the box moving under the
    // user, not the user scrolling. Detect it by the height change and don't let
    // it overwrite their intent; the ResizeObserver below re-anchors.
    if (pre.clientHeight !== viewLastCH) { viewLastCH = pre.clientHeight; return; }
    const nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
    viewStickBottom = nearBottom;
    if (!nearBottom) _setScrolled(true);
    else if (!viewAppScrolled) _setScrolled(false);
  }, { passive: true });

  // Soft keyboard (or rotation) resizes the terminal: if the user was following
  // the live tail, keep them there — the input box rides up above the keyboard
  // and drops back down when it dismisses. A deliberately scrolled-up reader is
  // left where they are.
  viewResizeObs = new ResizeObserver(() => {
    viewLastCH = pre.clientHeight;
    if (viewStickBottom) pre.scrollTop = pre.scrollHeight;
  });
  viewResizeObs.observe(pre);

  // Scrolling a TUI is two-layered: within the frame the browser pans natively
  // (the frame is often taller than a phone viewport — wrapped lines, 50-row
  // panes, a fresh session's banner). Only a pull BEYOND the frame's edge pages
  // the app's own transcript via line-granular mouse-wheel SGR. For a plain
  // shell (viewIsTui false) everything stays native.
  const atEdge = (up) => up
    ? pre.scrollTop <= 2
    : pre.scrollHeight - pre.scrollTop - pre.clientHeight <= 2;
  const frameScrolls = () => pre.scrollHeight - pre.clientHeight > 4;
  pre.addEventListener('wheel', (e) => {
    if (!viewIsTui) return;
    const up = e.deltaY < 0;
    if (frameScrolls() && !atEdge(up)) return;   // native wheel pans the frame
    e.preventDefault();
    const notches = Math.max(1, Math.min(8, Math.round(Math.abs(e.deltaY) / 32)));
    queueWheel(up ? notches : -notches);   // +up = older
  }, { passive: false });
  let tY = null, tAcc = 0;
  const WHEEL_PX = 14;   // px of drag per line — fine-grained, smooth
  pre.addEventListener('touchstart', (e) => {
    tY = e.touches[0].clientY; tAcc = 0;
    tapY = tY; tapT = Date.now(); tapST = pre.scrollTop;
  }, { passive: true });
  pre.addEventListener('touchmove', (e) => {
    if (!viewIsTui || tY == null) return;
    const y = e.touches[0].clientY;
    const dy = y - tY;
    // Within the frame the browser pans natively (touch-action: pan-y); only a
    // pull at the edge pages the app. (Mid-gesture the browser may ignore a late
    // preventDefault once a native pan owns the gesture — lifting the finger and
    // dragging again at the edge always engages the app paging.)
    if (frameScrolls() && !atEdge(dy > 0)) { tY = y; tAcc = 0; return; }
    tAcc += dy; tY = y;
    const lines = Math.trunc(tAcc / WHEEL_PX);
    if (lines !== 0) {
      queueWheel(lines);   // drag DOWN (positive) reveals EARLIER content
      tAcc -= lines * WHEEL_PX;
      e.preventDefault();
    }
  }, { passive: false });
  pre.addEventListener('touchend', (e) => {
    const t = e.changedTouches && e.changedTouches[0];
    if (t && tapY != null && Math.abs(t.clientY - tapY) < 8 &&
        Date.now() - tapT < 350 && Math.abs(pre.scrollTop - tapST) < 4 &&
        viewCapture) viewCapture.focus({ preventScroll: true });
    tY = null; tapY = null;
  });

  connectView(name);
  // No auto-focus: opening a session must NOT raise the soft keyboard. The
  // keyboard appears only when the user taps the terminal (the `pre` click
  // handler above focuses the capture textarea on demand).
}

function connectView(name) {
  if (liveWs) { try { liveWs.close(); } catch (e) {} }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/view/${name}`);
  liveWs = ws;
  ws.onopen = () => { reconnectDelay = 600; setTermState('connected'); };
  ws.onmessage = (e) => {
    let msg; try { msg = JSON.parse(e.data); } catch (err) { return; }
    if (msg.type === 'content') { viewPending = msg.data || ''; _viewSchedule(); }
    else if (msg.type === 'meta') {
      viewIsTui = !!msg.tui;
      // TUIs scroll via wheel events → disable native scroll (no bounce). A shell
      // keeps native scroll. Toggled here because meta arrives after the pre exists.
      if (livePre) livePre.classList.toggle('tui-view', viewIsTui);
    }
  };
  ws.onclose = () => { setTermState('disconnected'); scheduleReconnect(); };
  ws.onerror = () => { setTermState('error'); };
}

function closeTerminal() {
  // If we scrolled the shared session's transcript up, return it to the live tail
  // BEFORE disconnecting — otherwise the desktop Claude session (same tmux pane)
  // is left scrolled up. A native-only pan never touched the pane, so it needs no
  // reset. Send the page-downs while the socket is still open.
  if (viewAppScrolled && liveWs && liveWs.readyState === WebSocket.OPEN) {
    for (let i = 0; i < 16; i++) wsKey('NPage');
  }
  document.getElementById('terminal-overlay').classList.remove('open');
  updateFab();
  if (liveResizeHandler) { window.removeEventListener('resize', liveResizeHandler); liveResizeHandler = null; }
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  // Null these BEFORE closing the socket so onclose's scheduleReconnect bails.
  liveMode = null; liveName = null;
  if (liveWs) { try { liveWs.close(); } catch (e) {} liveWs = null; }
  if (liveTerm) { try { liveTerm.dispose(); } catch (e) {} liveTerm = null; }
  if (viewRaf) { cancelAnimationFrame(viewRaf); viewRaf = null; }
  if (wheelRaf) { cancelAnimationFrame(wheelRaf); wheelRaf = null; }
  wheelAccum = 0;
  viewPending = null; viewCapture = null; jumpLiveBtn = null; viewScrolledUp = false; viewIsTui = false;
  viewAppScrolled = false; viewStickBottom = true;
  if (viewResizeObs) { viewResizeObs.disconnect(); viewResizeObs = null; }
  liveFit = null; livePre = null;
  liveSession = null;
  resetTerminalViewport();
}

// ── Unified input: routes to the PTY byte-stream or the capture-view send-keys
// protocol depending on which renderer is active. ──
function sendText(text) {
  if (!text || !liveWs || liveWs.readyState !== WebSocket.OPEN) return;
  if (liveMode === 'view') liveWs.send(JSON.stringify({ type: 'keys', data: text }));
  else { liveWs.send(JSON.stringify({ type: 'input', data: text })); if (liveTerm) liveTerm.focus(); }
  _viewSnapLive();   // typing snaps the view back to the live tail (input line)
}

// Raw named-key send (no scroll-state side effect) — used by viewScroll.
function wsKey(key) {
  if (!liveWs || liveWs.readyState !== WebSocket.OPEN) return;
  if (liveMode === 'view') {
    liveWs.send(JSON.stringify({ type: 'key', data: key }));
  } else {
    const seq = PTYSEQ[key];
    if (seq != null) { liveWs.send(JSON.stringify({ type: 'input', data: seq })); if (liveTerm) liveTerm.focus(); }
  }
}

function sendNamed(key) {   // key: Enter|Escape|Tab|Up|Down|Left|Right|C-c|...
  wsKey(key);
  // Any non-scroll key returns the TUI to the live tail (the input sits there).
  if (key !== 'PPage' && key !== 'NPage') _viewSnapLive();
}

// ── Todo-list visibility (display-only). Hidden by default on mobile, where the
// checklist eats most of the screen; the pane itself is never sent any keys. ──
let todosHidden = localStorage.getItem('clanker_todos_hidden') === null
  ? isMobileView() : localStorage.getItem('clanker_todos_hidden') === '1';
function toggleTodos() {
  todosHidden = !todosHidden;
  localStorage.setItem('clanker_todos_hidden', todosHidden ? '1' : '0');
  if (navigator.vibrate) navigator.vibrate(15);
  if (viewLastFrame != null) { viewPending = viewLastFrame; _viewSchedule(); }
}
// esc: tap = send Escape; hold 550ms = hide/show the todo list.
(() => {
  const b = document.getElementById('key-esc');
  if (!b) return;
  let t = null, held = false;
  b.addEventListener('pointerdown', () => {
    held = false; clearTimeout(t);
    t = setTimeout(() => { held = true; toggleTodos(); }, 550);
  });
  b.addEventListener('pointerup', () => { clearTimeout(t); if (!held) sendNamed('Escape'); held = false; });
  b.addEventListener('pointerleave', () => clearTimeout(t));
  b.addEventListener('pointercancel', () => clearTimeout(t));
  b.addEventListener('contextmenu', e => e.preventDefault());
})();

// When the tab returns to the foreground: if the socket died while we were
// backgrounded (mobile suspends the tab + drops the WS), reconnect right away so
// the terminal is live again WITHOUT the operator having to exit and re-open it.
// Otherwise just paint the latest buffered frame (rendering is skipped while hidden).
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  if (liveMode && liveName && liveSocketDead()) {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    reconnectDelay = 600;
    reconnectLive();
  } else if (liveMode === 'view' && viewPending != null) {
    _viewSchedule();
  }
});
// Some mobile browsers fire pageshow (bfcache restore) but not visibilitychange.
window.addEventListener('pageshow', () => {
  if (liveMode && liveName && liveSocketDead()) reconnectLive();
});

// ── Keep the terminal (and its input bar) above the on-screen keyboard.
// The soft keyboard shrinks the VISUAL viewport but not the layout viewport, so
// a position:fixed overlay would otherwise sit behind the keyboard. Pin the
// overlay box to the visual viewport. ──
function applyTerminalViewport() {
  const vv = window.visualViewport;
  const ov = document.getElementById('terminal-overlay');
  if (!vv || !ov.classList.contains('open')) return;
  ov.style.top = vv.offsetTop + 'px';
  ov.style.left = vv.offsetLeft + 'px';
  ov.style.height = vv.height + 'px';
  ov.style.width = vv.width + 'px';
}
function resetTerminalViewport() {
  const ov = document.getElementById('terminal-overlay');
  ov.style.top = ov.style.left = ov.style.height = ov.style.width = '';
}
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', applyTerminalViewport);
  window.visualViewport.addEventListener('scroll', applyTerminalViewport);
}

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (document.getElementById('newsess-overlay').classList.contains('open')) { closeNewSession(); return; }
  if (document.getElementById('sysmon-overlay').classList.contains('open')) { closeSysmon(); return; }
  if (document.getElementById('terminal-overlay').classList.contains('open')) closeTerminal();
});
// ─── New Session (launch a fresh tmux instance from the WebUI) ───
function openNewSession() {
  document.getElementById('newsess-name').value = '';
  document.getElementById('newsess-shell').checked = false;
  document.getElementById('newsess-overlay').classList.add('open');
  updateFab();
  setTimeout(() => document.getElementById('newsess-name').focus(), 60);
}
function closeNewSession() { document.getElementById('newsess-overlay').classList.remove('open'); updateFab(); }

// ─── Native System Monitor (floating button → overlay, real-time WS stream) ───
let sysmonWs = null, sysmonSort = 'cpu', sysmonFilter = '', sysmonReconnect = null;

// Floating sys FAB hides whenever an overlay is open (the terminal header carries
// its own sys button, so the monitor stays reachable while viewing a terminal).
function updateFab() {
  const any = ['terminal-overlay', 'sysmon-overlay', 'newsess-overlay']
    .some(id => document.getElementById(id).classList.contains('open'));
  document.body.classList.toggle('overlay-open', any);
}
function openSysmon() {
  document.getElementById('sysmon-overlay').classList.add('open');
  updateFab();
  connectSysmon();
}
function closeSysmon() {
  document.getElementById('sysmon-overlay').classList.remove('open');
  updateFab();
  if (sysmonReconnect) { clearTimeout(sysmonReconnect); sysmonReconnect = null; }
  if (sysmonWs) { try { sysmonWs.close(); } catch (e) {} sysmonWs = null; }
}
// Real-time: one WebSocket streaming a JSON snapshot ~every second (one persistent
// SSH loop on the host), instead of polling. Reconnects if the socket drops.
function connectSysmon() {
  if (sysmonWs) { try { sysmonWs.close(); } catch (e) {} }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/sysmon`);
  sysmonWs = ws;
  ws.onmessage = (e) => {
    let d; try { d = JSON.parse(e.data); } catch (err) { return; }
    if (d.error) { renderSysmonError(d.error); return; }
    renderSysmon(d);
  };
  ws.onclose = () => {
    if (!document.getElementById('sysmon-overlay').classList.contains('open')) return;
    if (sysmonReconnect) clearTimeout(sysmonReconnect);
    sysmonReconnect = setTimeout(() => { if (!document.hidden) connectSysmon(); }, 1500);
  };
  ws.onerror = () => {};
}

// ── formatters ──
function smBytes(n) {
  if (n == null) return '—';
  const u = ['B','K','M','G','T','P']; let i = 0; n = Math.abs(n);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)) + u[i];
}
function smRate(n) { return smBytes(n) + '/s'; }
function smCls(pct) { return pct >= 85 ? 'crit' : pct >= 60 ? 'warn' : ''; }
function smDur(s) {
  s = Math.floor(s || 0); const d = Math.floor(s/86400), h = Math.floor(s%86400/3600), m = Math.floor(s%3600/60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

function bar(label, pct, valText, extraCls) {
  const c = smCls(pct);
  return `<div class="sm-bar-row"><span class="sm-lbl">${esc(label)}</span>`
    + `<span class="sm-bar ${c} ${extraCls||''}"><span style="width:${Math.max(0,Math.min(100,pct)).toFixed(1)}%"></span></span>`
    + `<span class="sm-val">${valText}</span></div>`;
}

let _sysmonLast = null;
function renderSysmon(d) {
  _sysmonLast = d;
  document.getElementById('sm-host').textContent = (d.host || '') + (d.model ? ' · ' + d.model : '');
  document.getElementById('sm-age').textContent = '● live';
  // Don't rebuild the body while the user is typing in the process filter — a full
  // innerHTML swap would steal focus. The snapshot is stored; the next poll after
  // they blur repaints. (The header age above still updates.)
  const af = document.activeElement;
  if (af && af.id === 'sm-filter') return;

  const memPct = d.mem.total ? 100 * d.mem.used / d.mem.total : 0;
  const swapPct = d.swap.total ? 100 * d.swap.used / d.swap.total : 0;
  const loadPctOfCores = d.ncpu ? 100 * d.load[0] / d.ncpu : 0;

  // ── CPU card ──
  let cpu = `<div class="sm-card"><h4>CPU <span class="sm-sub">${d.ncpu} threads</span></h4>`;
  cpu += `<div class="sm-chips">`
    + `<span class="sm-chip"><b>${d.cpu}%</b> total</span>`
    + (d.temp != null ? `<span class="sm-chip" style="${d.temp>=85?'border-color:var(--accent-red)':''}"><b>${d.temp}°C</b></span>` : '')
    + (d.freq != null ? `<span class="sm-chip"><b>${(d.freq/1000).toFixed(2)}</b>GHz</span>` : '')
    + `<span class="sm-chip">load <b>${d.load[0].toFixed(2)}</b> ${d.load[1].toFixed(2)} ${d.load[2].toFixed(2)}</span>`
    + `<span class="sm-chip">tasks <b>${esc(d.tasks)}</b></span>`
    + `<span class="sm-chip">up <b>${smDur(d.uptime)}</b></span>`
    + `</div>`;
  cpu += bar('all', d.cpu, d.cpu + '%');
  cpu += `<div class="sm-cores">` + d.cores.map((c, i) =>
    `<div class="sm-core">${i}<div class="sm-cbar ${smCls(c)}"><span style="width:${c}%"></span></div></div>`).join('') + `</div>`;
  cpu += `</div>`;

  // ── Memory card ──
  let mem = `<div class="sm-card"><h4>Memory</h4>`;
  mem += bar('RAM', memPct, `${smBytes(d.mem.used)} / ${smBytes(d.mem.total)}`);
  mem += `<div class="sm-chips" style="margin-top:6px">`
    + `<span class="sm-chip">avail <b>${smBytes(d.mem.avail)}</b></span>`
    + `<span class="sm-chip">cached <b>${smBytes(d.mem.cached)}</b></span>`
    + `<span class="sm-chip">buffers <b>${smBytes(d.mem.buffers)}</b></span></div>`;
  mem += d.swap.total ? bar('swap', swapPct, `${smBytes(d.swap.used)} / ${smBytes(d.swap.total)}`)
                      : `<div class="sm-bar-row"><span class="sm-lbl">swap</span><span class="sm-val">none</span></div>`;
  mem += `</div>`;

  // ── Network card ──
  let net = `<div class="sm-card"><h4>Network</h4>`;
  net += (d.net.length ? d.net.map(n =>
    `<div class="sm-bar-row"><span class="sm-lbl" style="min-width:74px">${esc(n.iface)}</span>`
    + `<span class="sm-val" style="margin-left:0">↓ ${smRate(n.rx)}</span>`
    + `<span class="sm-val">↑ ${smRate(n.tx)}</span></div>`).join('')
    : `<div class="no-data">no active interfaces</div>`);
  net += `</div>`;

  // ── Disk card (I/O + filesystems) ──
  let disk = `<div class="sm-card"><h4>Disk</h4>`;
  if (d.disk_io.length) {
    disk += `<div style="font-size:10px;color:var(--text-muted);font-family:var(--font-mono);margin-bottom:4px">I/O</div>`;
    disk += d.disk_io.map(x =>
      `<div class="sm-bar-row"><span class="sm-lbl" style="min-width:64px">${esc(x.dev)}</span>`
      + `<span class="sm-val" style="margin-left:0">r ${smRate(x.read)}</span>`
      + `<span class="sm-val">w ${smRate(x.write)}</span></div>`).join('');
  }
  if (d.fs.length) {
    disk += `<div style="font-size:10px;color:var(--text-muted);font-family:var(--font-mono);margin:8px 0 4px">Filesystems</div>`;
    disk += d.fs.map(f => bar(f.mount.length > 18 ? '…' + f.mount.slice(-17) : f.mount, f.pct,
      `${smBytes(f.used)}/${smBytes(f.total)} (${f.pct}%)`)).join('');
  }
  disk += `</div>`;

  // ── Processes card ──
  let proc = `<div class="sm-card wide"><h4>Processes <span class="sm-sub">${d.top.length} shown</span></h4>`;
  proc += `<div class="sm-proc-ctl">`
    + `<input id="sm-filter" placeholder="filter…" value="${esc(sysmonFilter)}" oninput="sysmonOnFilter(this.value)" autocomplete="off" autocapitalize="off" spellcheck="false">`
    + `<button class="sm-sort-btn ${sysmonSort==='cpu'?'active':''}" onclick="sysmonSetSort('cpu')">CPU</button>`
    + `<button class="sm-sort-btn ${sysmonSort==='mem'?'active':''}" onclick="sysmonSetSort('mem')">MEM</button></div>`;
  proc += renderProcTable(d.top);
  proc += `</div>`;

  document.getElementById('sysmon-body').innerHTML = cpu + mem + net + disk + proc;
}

function renderProcTable(rows) {
  let list = rows.slice();
  const f = sysmonFilter.trim().toLowerCase();
  if (f) list = list.filter(p => (p.cmd || '').toLowerCase().includes(f) || String(p.pid).includes(f) || (p.user||'').toLowerCase().includes(f));
  list.sort((a, b) => (sysmonSort === 'mem' ? b.mem - a.mem : b.cpu - a.cpu));
  let t = `<table class="sm-proc"><thead><tr><th>PID</th><th>USER</th><th class="num">CPU%</th><th class="num">MEM%</th><th class="num">RSS</th><th>COMMAND</th></tr></thead><tbody>`;
  t += list.map(p =>
    `<tr><td>${p.pid}</td><td>${esc(p.user)}</td>`
    + `<td class="num ${p.cpu>=50?'hot':''}">${p.cpu.toFixed(1)}</td>`
    + `<td class="num ${p.mem>=20?'hot':''}">${p.mem.toFixed(1)}</td>`
    + `<td class="num">${smBytes(p.rss)}</td><td class="cmd">${esc(p.cmd)}</td></tr>`).join('');
  t += `</tbody></table>`;
  return t;
}

// Sort/filter re-render the process table from the last snapshot (no refetch).
function sysmonSetSort(s) {
  sysmonSort = s;
  document.querySelectorAll('.sm-sort-btn').forEach(b => b.classList.toggle('active', b.textContent.toLowerCase() === s));
  if (_sysmonLast) document.querySelector('.sm-proc').outerHTML = renderProcTable(_sysmonLast.top);
}
function sysmonOnFilter(v) {
  sysmonFilter = v;
  if (_sysmonLast) document.querySelector('.sm-proc').outerHTML = renderProcTable(_sysmonLast.top);
}
function renderSysmonError(msg) {
  document.getElementById('sysmon-body').innerHTML = `<div class="sm-card wide"><div class="sm-err">monitor unavailable: ${esc(msg)}</div></div>`;
}

async function createNewSession() {
  const go = document.getElementById('newsess-go');
  const name = document.getElementById('newsess-name').value.trim();
  const shell = document.getElementById('newsess-shell').checked;
  go.disabled = true; go.textContent = 'Creating…';
  try {
    const r = await fetch('/api/session/new', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, shell }),
    });
    const d = await r.json();
    if (!r.ok || d.error) { alert('Could not create session: ' + (d.error || r.status)); return; }
    closeNewSession();
    fetchStatus();
    // A bare shell isn't in the claude-filtered list; open it directly either way.
    openTerminal(d.session);
  } catch (e) { alert('create error'); }
  finally { go.disabled = false; go.textContent = 'Create & Open'; }
}

function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// ─── Tiled View ───
let tiledActive = false;
let tiledTerminals = {};
let tiledInterval = null;
let prevTiledStates = {};
let tiledSelectedSessions = new Set();
let tiledLayout = 'grid';
let allSessions = []; // cached for session switcher

const XTERM_THEME = {
  background: '#0C0A09', foreground: '#FAFAF9', cursor: '#C2410C',
  black: '#1C1917', red: '#DC2626', green: '#65A30D', yellow: '#D97706',
  blue: '#2563EB', magenta: '#9333EA', cyan: '#0891B2', white: '#A8A29E',
  brightBlack: '#57534E', brightRed: '#EF4444', brightGreen: '#84CC16',
  brightYellow: '#F59E0B', brightBlue: '#3B82F6', brightMagenta: '#A855F7',
  brightCyan: '#06B6D4', brightWhite: '#FAFAF9',
};

// ─── ANSI Color Map ───
const ANSI_COLORS = {
  '30': '#1C1917', '31': '#DC2626', '32': '#65A30D', '33': '#D97706',
  '34': '#2563EB', '35': '#9333EA', '36': '#0891B2', '37': '#A8A29E',
  '90': '#57534E', '91': '#EF4444', '92': '#84CC16', '93': '#F59E0B',
  '94': '#3B82F6', '95': '#A855F7', '96': '#06B6D4', '97': '#FAFAF9',
};
const ANSI_BG_COLORS = {
  '40': '#1C1917', '41': '#DC2626', '42': '#65A30D', '43': '#D97706',
  '44': '#2563EB', '45': '#9333EA', '46': '#0891B2', '47': '#A8A29E',
  '100': '#57534E', '101': '#EF4444', '102': '#84CC16', '103': '#F59E0B',
  '104': '#3B82F6', '105': '#A855F7', '106': '#06B6D4', '107': '#FAFAF9',
};

// 256-color lookup (16-231: 6x6x6 cube, 232-255: grayscale)
function color256(n) {
  if (n < 16) {
    const basic = ['#1C1917','#DC2626','#65A30D','#D97706','#2563EB','#9333EA','#0891B2','#A8A29E',
                   '#57534E','#EF4444','#84CC16','#F59E0B','#3B82F6','#A855F7','#06B6D4','#FAFAF9'];
    return basic[n];
  }
  if (n < 232) {
    const i = n - 16;
    const r = Math.floor(i / 36) * 51;
    const g = Math.floor((i % 36) / 6) * 51;
    const b = (i % 6) * 51;
    return `rgb(${r},${g},${b})`;
  }
  const v = (n - 232) * 10 + 8;
  return `rgb(${v},${v},${v})`;
}

function parseAnsiToSegments(text) {
  const segments = [];
  let fg = null, bg = null, bold = false, italic = false, underline = false, dim = false;
  // Split on ESC[ (real escape char is \\x1b = \\u001b)
  const parts = text.split('\\u001b[');

  // First part has no escape prefix
  if (parts[0]) segments.push({ text: parts[0], fg: null, bg: null, bold: false, italic: false, underline: false, dim: false });

  for (let i = 1; i < parts.length; i++) {
    // Find the CSI terminator (any letter). Only process SGR codes (ending in 'm').
    const termMatch = parts[i].match(/^([0-9;]*)([A-Za-z])/);
    if (!termMatch) { segments.push({ text: parts[i], fg, bg, bold, italic, underline, dim }); continue; }
    if (termMatch[2] !== 'm') {
      // Non-SGR sequence (cursor move, erase, etc.) — skip the code, keep any trailing text
      const rest = parts[i].substring(termMatch[0].length);
      if (rest) segments.push({ text: rest, fg, bg, bold, italic, underline, dim });
      continue;
    }
    const mIdx = termMatch[0].length - 1;
    // mIdx now points to 'm'

    const codes = parts[i].substring(0, mIdx).split(';');
    const rest = parts[i].substring(mIdx + 1);

    for (let j = 0; j < codes.length; j++) {
      const c = codes[j];
      if (c === '0' || c === '') { fg = null; bg = null; bold = false; italic = false; underline = false; dim = false; }
      else if (c === '1') bold = true;
      else if (c === '2') dim = true;
      else if (c === '3') italic = true;
      else if (c === '4') underline = true;
      else if (c === '22') { bold = false; dim = false; }
      else if (c === '23') italic = false;
      else if (c === '24') underline = false;
      else if (c === '39') fg = null;
      else if (c === '49') bg = null;
      else if (ANSI_COLORS[c]) fg = ANSI_COLORS[c];
      else if (ANSI_BG_COLORS[c]) bg = ANSI_BG_COLORS[c];
      else if (c === '38' && codes[j+1] === '5') { fg = color256(parseInt(codes[j+2])); j += 2; }
      else if (c === '48' && codes[j+1] === '5') { bg = color256(parseInt(codes[j+2])); j += 2; }
      else if (c === '38' && codes[j+1] === '2') { fg = `rgb(${codes[j+2]},${codes[j+3]},${codes[j+4]})`; j += 4; }
      else if (c === '48' && codes[j+1] === '2') { bg = `rgb(${codes[j+2]},${codes[j+3]},${codes[j+4]})`; j += 4; }
    }

    if (rest) segments.push({ text: rest, fg, bg, bold, italic, underline, dim });
  }
  return segments;
}

function renderAnsiToHTML(ansiText) {
  const segments = parseAnsiToSegments(ansiText);
  let html = '';
  for (const seg of segments) {
    if (!seg.text) continue;
    let style = '';
    if (seg.fg) style += `color:${seg.fg};`;
    if (seg.bg) style += `background:${seg.bg};`;
    if (seg.bold) style += 'font-weight:700;';
    if (seg.dim) style += 'opacity:0.6;';
    if (seg.italic) style += 'font-style:italic;';
    if (seg.underline) style += 'text-decoration:underline;';
    const escaped = seg.text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    html += style ? `<span style="${style}">${escaped}</span>` : escaped;
  }
  return html;
}

// Characters that make up horizontal rules / box borders. A line that is almost
// entirely these is decoration — clip it to one row rather than wrap it into
// several identical-looking rows (the mobile clutter the operator flagged).
const RULE_CHARS = new Set([
  '─','━','│','┃','┄','┅','┆','┇',
  '┈','┉','┊','┋','═','║','╌','╍',
  '╭','╮','╯','╰','╱','╴','╶','╸','╺',
  '├','┤','┬','┴','┼','▀','▁','▔','▔',
  '-','—','–','_','=','·','⎯','•',
]);
function isRuleLine(visible) {
  const t = (visible || '').trim();
  if (t.length < 10) return false;
  let rule = 0, other = 0;
  for (const ch of t) {
    if (ch === ' ') continue;
    if (RULE_CHARS.has(ch)) rule++; else other++;
  }
  return rule >= 8 && other <= Math.max(2, rule * 0.12);
}

// Per-line renderer: splits the styled segments into lines, decides wrap vs
// clip per line, and emits one <div> each. Reuses parseAnsiToSegments so the
// (correct) ANSI decoding isn't duplicated.
// A TUI todo-checklist row (the task list Claude renders above its input box).
const TODO_ROW_RE = /^\\s*(?:⎿\\s*)?[☐☒☑◻◼✓✔]/;
// The panel's trailer row, e.g. "… +6 completed".
const TODO_TRAILER_RE = /^\\s*(?:…|\\.{3})?\\s*\\+\\d+\\s+completed\\b/;
function renderAnsiToLines(ansiText) {
  const segments = parseAnsiToSegments(ansiText);
  const lines = [[]];
  for (const seg of segments) {
    const parts = (seg.text || '').split('\\n');
    for (let k = 0; k < parts.length; k++) {
      if (k > 0) lines.push([]);
      if (parts[k]) lines[lines.length - 1].push({ ...seg, text: parts[k] });
    }
  }
  // Materialize rows first so the TUI's bottom chrome (input-box rules, status
  // zone, todo list) can be located before any HTML is emitted.
  const rows = lines.map(line => {
    let visible = '', inner = '';
    for (const seg of line) {
      visible += seg.text;
      let style = '';
      if (seg.fg) style += `color:${seg.fg};`;
      if (seg.bg) style += `background:${seg.bg};`;
      if (seg.bold) style += 'font-weight:700;';
      if (seg.dim) style += 'opacity:0.6;';
      if (seg.italic) style += 'font-style:italic;';
      if (seg.underline) style += 'text-decoration:underline;';
      const esc = seg.text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      inner += style ? `<span style="${style}">${esc}</span>` : esc;
    }
    return { visible, inner, cls: isRuleLine(visible) ? 'tline rule' : 'tline' };
  });

  // Locate the input box: the last two rule lines near the bottom of the frame.
  let ruleBot = -1, ruleTop = -1;
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i].cls.indexOf('rule') < 0) continue;
    if (ruleBot < 0) { ruleBot = i; } else { ruleTop = i; break; }
  }
  const tuiBottom = ruleBot >= 0 && ruleTop >= 0 && (ruleBot - ruleTop) <= 8 &&
                    (rows.length - 1 - ruleBot) <= 16;

  const skip = new Set();
  let todoSummaryAt = -1, todoCount = 0;
  if (tuiBottom) {
    // 1. STATUS ZONE below the input box (statusline + mode hints): on a phone
    //    these wrap to 5-6 lines. Render the first 3 as single ellipsis-clamped
    //    lines and drop the rest. >5 non-blank rows means a menu/dialog is open
    //    down there — leave that alone.
    const foot = [];
    for (let i = ruleBot + 1; i < rows.length; i++) if (rows[i].visible.trim()) foot.push(i);
    if (foot.length > 0 && foot.length <= 5) {
      for (let i = ruleBot + 1; i < rows.length; i++) {
        if (!rows[i].visible.trim()) { skip.add(i); continue; }
        if (foot.indexOf(i) < 3) rows[i].cls += ' tfoot'; else skip.add(i);
      }
    }
    // 2. TODO LIST: consecutive checkbox rows sitting right above the input box.
    //    Display-side collapse only — the session itself is never touched.
    if (todosHidden) {
      let i = ruleTop - 1;
      while (i >= 0 && !rows[i].visible.trim()) i--;
      const end = i;
      // Optional trailer ("… +6 completed"), then the checkbox rows above it.
      let t = 0;
      while (i >= 0 && t < 2 && TODO_TRAILER_RE.test(rows[i].visible)) { i--; t++; }
      while (i >= 0 && TODO_ROW_RE.test(rows[i].visible)) { todoCount++; i--; }
      if (todoCount > 0) {
        for (let k = i + 1; k <= end; k++) skip.add(k);
        todoSummaryAt = end;
      }
    }
  }

  let html = '';
  for (let i = 0; i < rows.length; i++) {
    if (skip.has(i)) {
      if (i === todoSummaryAt)
        html += `<div class="tline todo-sum">☐ ${todoCount} todo${todoCount > 1 ? 's' : ''} hidden · hold esc</div>`;
      continue;
    }
    html += `<div class="${rows[i].cls}">${rows[i].inner || '&nbsp;'}</div>`;
  }
  return html;
}

function openTiledView() {
  document.getElementById('tile-picker').classList.add('open');
  fetch('/api/status').then(r => r.json()).then(data => {
    allSessions = data.sessions.filter(s => s.command === 'claude').sort(favStateSort);
    const saved = JSON.parse(localStorage.getItem('clanker_tiled_sessions') || '[]');
    const list = document.getElementById('picker-list');
    list.innerHTML = allSessions.map(s => {
      const checked = saved.length === 0 || saved.includes(s.session) ? 'checked' : '';
      return `<label class="picker-item"><input type="checkbox" value="${s.session}" ${checked}>
        ${favStar(s.session)}
        <span class="picker-name">${s.session}</span>
        <span class="picker-badge ${s.state}">${s.state}</span></label>`;
    }).join('');
  });
}

function launchTiledView() {
  const selected = Array.from(document.querySelectorAll('#picker-list input:checked')).map(c => c.value);
  localStorage.setItem('clanker_tiled_sessions', JSON.stringify(selected));
  document.getElementById('tile-picker').classList.remove('open');
  tiledActive = true;
  tiledSelectedSessions = new Set(selected);
  document.getElementById('tiled-overlay').classList.add('open');
  document.getElementById('tiled-grid').innerHTML = '';
  tiledLayout = localStorage.getItem('clanker_tile_layout_mode') || 'grid';
  updateLayoutButtons();
  fetchTiledPanes();
  tiledInterval = setInterval(() => { if (!document.hidden) fetchTiledPanes(); }, 2000);
}

function closeTiledView() {
  tiledActive = false;
  document.getElementById('tiled-overlay').classList.remove('open');
  if (tiledInterval) { clearInterval(tiledInterval); tiledInterval = null; }
  Object.keys(tiledTerminals).forEach(s => disconnectTile(s));
  tiledTerminals = {};
  prevTiledStates = {};
}

async function fetchTiledPanes() {
  try {
    const [panesR, statusR] = await Promise.all([fetch('/api/panes'), fetch('/api/status')]);
    if (!panesR.ok) return;
    const panes = await panesR.json();
    const status = await statusR.json();
    allSessions = status.sessions.filter(s => s.command === 'claude');
    renderTiles(panes);
  } catch(e) {}
}

function renderTiles(panes) {
  const grid = document.getElementById('tiled-grid');
  const filtered = panes.filter(p => tiledSelectedSessions.has(p.session));

  filtered.forEach(p => {
    let tile = document.getElementById('tile-' + p.session);
    if (!tile) {
      tile = document.createElement('div');
      tile.id = 'tile-' + p.session;
      tile.className = 'tile';
      tile.dataset.session = p.session;
      tile.innerHTML = buildTileHTML(p);
      grid.appendChild(tile);
    }
    // Update badge
    const badge = tile.querySelector('.tile-badge');
    if (badge) { badge.className = 'tile-badge ' + p.state; badge.textContent = p.state; }
    // Flash on waiting transition
    const prev = prevTiledStates[p.session];
    if (p.state === 'waiting' && prev === 'working') {
      tile.classList.add('flash');
      playChime();
      if ('Notification' in window && Notification.permission === 'granted')
        new Notification(p.session + ' needs input', { tag: 'tile-' + p.session, renotify: true });
    } else if (p.state !== 'waiting') { tile.classList.remove('flash'); }
    prevTiledStates[p.session] = p.state;
    // Update monitor
    if (!tiledTerminals[p.session]) {
      const pre = tile.querySelector('.tile-monitor');
      if (pre) pre.textContent = p.content;
    }
    const btn = tile.querySelector('.tile-connect');
    if (btn) btn.textContent = tiledTerminals[p.session] ? 'disconnect' : 'connect';
    // Update select value
    const sel = tile.querySelector('.tile-select');
    if (sel && sel.value !== p.session) sel.value = p.session;
  });

  // Apply layout after first render
  if (grid.children.length > 0) applyLayout(tiledLayout);
  // Keep connected terminals fitted as the grid updates (fit() is a no-op if unchanged).
  Object.values(tiledTerminals).forEach(t => { try { if (t.fitAddon) t.fitAddon.fit(); } catch (e) {} });
}

function buildTileHTML(p) {
  const opts = allSessions.map(s =>
    `<option value="${s.session}" ${s.session === p.session ? 'selected' : ''}>${s.session} [${s.state}]</option>`
  ).join('');
  return `<div class="tile-header">
      <select class="tile-select" onchange="switchTileSession('${p.session}', this.value, this)" onmousedown="event.stopPropagation()">${opts}</select>
      <span class="tile-badge ${p.state}">${p.state}</span>
      <button class="tile-connect" onclick="toggleTileTerminal('${p.session}')">connect</button>
    </div>
    <div class="tile-body" id="tile-body-${p.session}">
      <pre class="tile-monitor">${escapeHtml(p.content)}</pre>
    </div>`;
}

// ─── Layouts (tiling WM style) ───
function setLayout(mode) {
  tiledLayout = mode;
  localStorage.setItem('clanker_tile_layout_mode', mode);
  updateLayoutButtons();
  applyLayout(mode);
  // Refit terminals
  setTimeout(() => Object.values(tiledTerminals).forEach(t => { if (t.fitAddon) t.fitAddon.fit(); }), 100);
}

function updateLayoutButtons() {
  document.querySelectorAll('.layout-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.layout === tiledLayout);
  });
}

function applyLayout(mode) {
  const grid = document.getElementById('tiled-grid');
  const tiles = Array.from(grid.querySelectorAll('.tile'));
  if (!tiles.length) return;
  const n = tiles.length;
  const cw = grid.clientWidth;
  const ch = grid.clientHeight;
  const gap = 2;

  // Reset all tile styles
  tiles.forEach(t => { t.style.cssText = ''; });

  switch(mode) {
    case 'grid': {
      const cols = Math.ceil(Math.sqrt(n * (cw / ch)));
      const rows = Math.ceil(n / cols);
      grid.style.display = 'grid';
      grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
      grid.style.gridTemplateRows = `repeat(${rows}, 1fr)`;
      grid.style.gap = gap + 'px';
      tiles.forEach(t => { t.style.minHeight = '0'; t.style.minWidth = '0'; });
      break;
    }
    case 'master': {
      grid.style.display = 'grid';
      grid.style.gap = gap + 'px';
      if (n === 1) {
        grid.style.gridTemplateColumns = '1fr';
        grid.style.gridTemplateRows = '1fr';
      } else {
        grid.style.gridTemplateColumns = '3fr 2fr';
        grid.style.gridTemplateRows = `repeat(${n - 1}, 1fr)`;
        tiles[0].style.gridRow = `1 / ${n}`;
      }
      tiles.forEach(t => { t.style.minHeight = '0'; t.style.minWidth = '0'; });
      break;
    }
    case 'columns': {
      grid.style.display = 'grid';
      grid.style.gridTemplateColumns = `repeat(${n}, 1fr)`;
      grid.style.gridTemplateRows = '1fr';
      grid.style.gap = gap + 'px';
      tiles.forEach(t => { t.style.minHeight = '0'; t.style.minWidth = '0'; });
      break;
    }
    case 'rows': {
      grid.style.display = 'grid';
      grid.style.gridTemplateColumns = '1fr';
      grid.style.gridTemplateRows = `repeat(${n}, 1fr)`;
      grid.style.gap = gap + 'px';
      tiles.forEach(t => { t.style.minHeight = '0'; t.style.minWidth = '0'; });
      break;
    }
    case 'focus': {
      grid.style.display = 'grid';
      grid.style.gap = gap + 'px';
      if (n === 1) {
        grid.style.gridTemplateColumns = '1fr';
        grid.style.gridTemplateRows = '1fr';
      } else {
        grid.style.gridTemplateColumns = '1fr';
        grid.style.gridTemplateRows = `1fr 120px`;
        tiles[0].style.gridColumn = '1';
        tiles[0].style.gridRow = '1';
        // Remaining tiles in a horizontal strip at the bottom
        const sub = document.createElement('div');
        sub.className = 'focus-strip';
        sub.style.cssText = `display:flex;gap:${gap}px;grid-column:1;grid-row:2;overflow-x:auto;`;
        tiles.slice(1).forEach(t => {
          t.style.minWidth = '250px';
          t.style.minHeight = '0';
          t.style.flex = '1';
          sub.appendChild(t);
        });
        grid.appendChild(sub);
      }
      break;
    }
  }
}

function resetTileLayout() {
  localStorage.removeItem('clanker_tile_layout_mode');
  tiledLayout = 'grid';
  updateLayoutButtons();
  applyLayout('grid');
  setTimeout(() => Object.values(tiledTerminals).forEach(t => { if (t.fitAddon) t.fitAddon.fit(); }), 100);
}

// ─── Session Switcher (select dropdown) ───
function switchTileSession(oldSession, newSession, selectEl) {
  if (oldSession === newSession) return;
  disconnectTile(oldSession);
  const tile = document.getElementById('tile-' + oldSession);
  if (!tile) return;

  tile.id = 'tile-' + newSession;
  tile.dataset.session = newSession;
  const body = tile.querySelector('.tile-body');
  if (body) { body.id = 'tile-body-' + newSession; body.innerHTML = '<pre class="tile-monitor">Loading...</pre>'; }
  // Update connect button onclick
  const btn = tile.querySelector('.tile-connect');
  if (btn) btn.setAttribute('onclick', "toggleTileTerminal('" + newSession + "')");
  // Update select onchange
  if (selectEl) selectEl.setAttribute('onchange', "switchTileSession('" + newSession + "', this.value, this)");
  tiledSelectedSessions.delete(oldSession);
  tiledSelectedSessions.add(newSession);
}

// ─── Terminal Connect/Disconnect ───
function toggleTileTerminal(session) {
  tiledTerminals[session] ? disconnectTile(session) : connectTile(session);
}

function sendKey(ws, key) { ws.send(JSON.stringify({ type: 'key', data: key })); }
function sendKeys(ws, text) { ws.send(JSON.stringify({ type: 'keys', data: text })); }

function setupKeyboardInput(el, ws, session) {
  // Hidden textarea for mobile keyboard capture
  const ta = document.createElement('textarea');
  ta.className = 'terminal-input-capture';
  ta.setAttribute('autocomplete', 'off');
  ta.setAttribute('autocapitalize', 'off');
  ta.setAttribute('autocorrect', 'off');
  ta.setAttribute('spellcheck', 'false');
  el.parentNode.appendChild(ta);

  // Desktop: keydown on the pre element
  const keyHandler = (e) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    e.preventDefault();
    if (e.key === 'Enter') sendKey(ws, 'Enter');
    else if (e.key === 'Backspace') sendKey(ws, 'BSpace');
    else if (e.key === 'Tab') sendKey(ws, 'Tab');
    else if (e.key === 'Escape') {
      if (e.shiftKey && session) { closeTiledView(); return; }
      sendKey(ws, 'Escape');
    }
    else if (e.key === 'ArrowUp') sendKey(ws, 'Up');
    else if (e.key === 'ArrowDown') sendKey(ws, 'Down');
    else if (e.key === 'ArrowLeft') sendKey(ws, 'Left');
    else if (e.key === 'ArrowRight') sendKey(ws, 'Right');
    else if (e.key === 'Home') sendKey(ws, 'Home');
    else if (e.key === 'End') sendKey(ws, 'End');
    else if (e.key === 'PageUp') sendKey(ws, 'PPage');
    else if (e.key === 'PageDown') sendKey(ws, 'NPage');
    else if (e.ctrlKey && e.key === 'c') sendKey(ws, 'C-c');
    else if (e.ctrlKey && e.key === 'd') sendKey(ws, 'C-d');
    else if (e.ctrlKey && e.key === 'z') sendKey(ws, 'C-z');
    else if (e.ctrlKey && e.key === 'l') sendKey(ws, 'C-l');
    else if (e.ctrlKey && e.key === 'a') sendKey(ws, 'C-a');
    else if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
      sendKeys(ws, e.key);
    }
  };
  el.addEventListener('keydown', keyHandler);

  // Mobile: textarea input event (captures on-screen keyboard)
  ta.addEventListener('input', () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const text = ta.value;
    if (text) { sendKeys(ws, text); ta.value = ''; }
  });
  ta.addEventListener('keydown', (e) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (e.key === 'Enter') { e.preventDefault(); sendKey(ws, 'Enter'); ta.value = ''; }
    else if (e.key === 'Backspace' && !ta.value) { e.preventDefault(); sendKey(ws, 'BSpace'); }
  });

  // Tap on terminal → focus hidden textarea (shows mobile keyboard)
  el.addEventListener('click', () => {
    if ('ontouchstart' in window) ta.focus();
    else el.focus();
  });

  return { keyHandler, textarea: ta };
}

function connectTile(session) {
  const body = document.getElementById('tile-body-' + session);
  if (!body) return;
  body.innerHTML = '';

  // Real xterm.js terminal per tile, over the PTY bridge (same as the full-screen one).
  const term = new Terminal({
    theme: XTERM_THEME, fontFamily: 'JetBrains Mono, ui-monospace, monospace',
    fontSize: 11, cursorBlink: true, scrollback: 2000, allowProposedApi: true,
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(body);
  try { fitAddon.fit(); } catch (e) {}

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/terminal/${session}`);
  ws.binaryType = 'arraybuffer';

  function sendResize() {
    if (ws.readyState === WebSocket.OPEN)
      ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
  }

  ws.onopen = () => {
    const btn = document.querySelector('#tile-' + session + ' .tile-connect');
    if (btn) btn.textContent = 'disconnect';
    try { fitAddon.fit(); } catch (e) {}
    sendResize();
  };
  ws.onmessage = (e) => { term.write(typeof e.data === 'string' ? e.data : new Uint8Array(e.data)); };
  ws.onclose = () => {
    const btn = document.querySelector('#tile-' + session + ' .tile-connect');
    if (btn) btn.textContent = 'connect';
  };
  term.onData(d => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'input', data: d })); });
  term.onResize(() => sendResize());

  tiledTerminals[session] = { ws, term, fitAddon };
}

function disconnectTile(session) {
  const t = tiledTerminals[session];
  if (!t) return;
  if (t.ws && t.ws.readyState <= 1) { try { t.ws.close(); } catch (e) {} }
  if (t.term) { try { t.term.dispose(); } catch (e) {} }
  delete tiledTerminals[session];
  const body = document.getElementById('tile-body-' + session);
  if (body) body.innerHTML = '<pre class="tile-monitor">Disconnected</pre>';
  const btn = document.querySelector('#tile-' + session + ' .tile-connect');
  if (btn) btn.textContent = 'connect';
}

async function connectAllTiles() {
  for (const tile of document.querySelectorAll('.tile')) {
    const session = tile.dataset.session;
    if (session && !tiledTerminals[session]) {
      connectTile(session);
      await new Promise(r => setTimeout(r, 600));
    }
  }
}

function disconnectAllTiles() {
  Object.keys(tiledTerminals).forEach(s => disconnectTile(s));
}

function playChime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.setValueAtTime(1100, ctx.currentTime + 0.1);
    gain.gain.setValueAtTime(0.3, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
    osc.start(ctx.currentTime); osc.stop(ctx.currentTime + 0.3);
  } catch(e) {}
}

// Inject button
(function() {
  const h2 = document.querySelector('[data-label="LIVE"] h2');
  const btn = 'float:right;font-family:var(--font-mono);font-size:10px;padding:4px 12px;background:var(--bg-surface);color:var(--text-secondary);border:1px solid var(--border);cursor:pointer;text-transform:uppercase;letter-spacing:0.1em';
  const btnNew = btn + ';background:var(--accent-terracotta);color:var(--accent-cream);border-color:var(--accent-terracotta)';
  if (h2) h2.innerHTML += ` <button onclick="openNewSession()" style="${btnNew}">+ New Session</button> <button onclick="openTiledView()" style="${btn}">Tiled View</button> <button onclick="openOrch()" style="${btn}">Orchestration</button>`;
})();
</script>

<!-- Tiled View Overlay -->
<style>
.tiled-overlay { position: fixed; inset: 0; background: var(--bg-void); z-index: 1000; display: none; flex-direction: column; }
.tiled-overlay.open { display: flex; }
.tiled-bar {
  display: flex; align-items: center; gap: 12px;
  padding: 6px 16px; background: var(--bg-deep);
  border-bottom: 2px solid var(--accent-terracotta); flex-shrink: 0;
}
.tiled-bar h3 { font-family: var(--font-display); font-size: 1.1rem; color: var(--accent-cream); font-weight: 400; }
.layout-group { display: flex; gap: 2px; margin-left: 12px; }
.layout-btn {
  font-family: var(--font-mono); font-size: 9px; padding: 4px 10px;
  background: var(--bg-panel); color: var(--text-muted);
  border: 1px solid transparent; cursor: pointer; text-transform: uppercase; letter-spacing: 0.08em;
}
.layout-btn:hover { color: var(--text-secondary); border-color: var(--border); }
.layout-btn.active { background: var(--accent-terracotta); color: var(--bg-void); border-color: var(--accent-terracotta); }
.tiled-bar-actions { display: flex; gap: 6px; align-items: center; margin-left: auto; }
.tiled-bar-actions button {
  font-family: var(--font-mono); font-size: 9px; padding: 4px 10px;
  background: var(--bg-surface); color: var(--text-secondary);
  border: 1px solid var(--border); cursor: pointer; text-transform: uppercase; letter-spacing: 0.05em;
}
.tiled-bar-actions button:hover { border-color: var(--accent-terracotta); color: var(--accent-cream); }
.tiled-bar-actions .close-btn { color: var(--text-muted); font-size: 22px; cursor: pointer; margin-left: 8px; border: none; background: none; line-height: 1; }
.tiled-bar-actions .close-btn:hover { color: var(--accent-red); }

.tiled-grid { flex: 1; display: grid; gap: 2px; overflow: hidden; }

.tile { background: var(--bg-deep); display: flex; flex-direction: column; border: 1px solid var(--border); overflow: hidden; }
.tile.flash { border-color: var(--accent-olive); animation: tileFlash 1.5s ease-in-out infinite; }
@keyframes tileFlash {
  0%, 100% { box-shadow: inset 0 0 0 0 rgba(101, 163, 13, 0); }
  50% { box-shadow: inset 0 0 30px 0 rgba(101, 163, 13, 0.15); }
}

.tile-header {
  display: flex; align-items: center; gap: 6px;
  padding: 4px 8px; background: var(--bg-panel);
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.tile-select {
  font-family: var(--font-mono); font-size: 11px; font-weight: 500;
  color: var(--accent-cream); background: var(--bg-surface);
  border: 1px solid var(--border); padding: 3px 6px; cursor: pointer;
  max-width: 180px;
}
.tile-select:hover { border-color: var(--accent-terracotta); }
.tile-select:focus { outline: none; border-color: var(--accent-terracotta); }
.tile-select option { background: var(--bg-panel); color: var(--text-secondary); }

.tile-badge {
  font-family: var(--font-mono); font-size: 8px; text-transform: uppercase;
  letter-spacing: 0.1em; padding: 2px 6px; margin-left: auto;
}
.tile-badge.working { color: var(--accent-amber); border: 1px solid var(--accent-amber); }
.tile-badge.waiting { color: var(--bg-void); background: var(--accent-olive); }
.tile-badge.idle { color: var(--text-muted); border: 1px solid var(--border); }
.tile-connect {
  font-family: var(--font-mono); font-size: 8px; padding: 2px 8px;
  background: var(--bg-surface); color: var(--text-muted);
  border: 1px solid var(--border); cursor: pointer; text-transform: uppercase; letter-spacing: 0.05em;
}
.tile-connect:hover { color: var(--accent-cream); border-color: var(--accent-terracotta); }

.tile-body { flex: 1; overflow: hidden; background: #0C0A09; min-height: 0; position: relative; }
.tile-terminal {
  font-family: var(--font-mono); font-size: 11px; line-height: 1.35;
  color: #FAFAF9; background: #0C0A09; padding: 6px 8px;
  white-space: pre; overflow-x: auto;
  overflow-y: auto; height: 100%; margin: 0;
  outline: none; cursor: text;
  contain: content; content-visibility: auto;
  scrollbar-width: thin; scrollbar-color: var(--border) transparent;
}
.tile-terminal:focus { box-shadow: inset 0 0 0 1px var(--accent-terracotta); }
.terminal-input-capture {
  position: absolute; left: -9999px; top: 0; width: 1px; height: 1px;
  opacity: 0; font-size: 16px; /* prevent iOS zoom */
}
@media (max-width: 768px) {
  .tile-terminal { white-space: pre-wrap; overflow-wrap: break-word; font-size: 10px; }
}
.tile-terminal::-webkit-scrollbar { width: 4px; }
.tile-terminal::-webkit-scrollbar-track { background: transparent; }
.tile-terminal::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
.tile-monitor {
  font-family: var(--font-mono); font-size: 10px; line-height: 1.3;
  color: var(--text-secondary); padding: 6px 8px;
  white-space: pre; overflow: auto; height: 100%; margin: 0;
  scrollbar-width: thin; scrollbar-color: var(--border) transparent;
}
.tile-monitor::-webkit-scrollbar { width: 4px; height: 4px; }
.tile-monitor::-webkit-scrollbar-track { background: transparent; }
.tile-monitor::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
.tile-body::-webkit-scrollbar { width: 4px; }
.tile-body::-webkit-scrollbar-track { background: transparent; }
.tile-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
.tile-body { scrollbar-width: thin; scrollbar-color: var(--border) transparent; }
.focus-strip { scrollbar-width: thin; scrollbar-color: var(--border) transparent; }
.focus-strip::-webkit-scrollbar { height: 4px; }
.focus-strip::-webkit-scrollbar-thumb { background: var(--border); }

.tile-picker { position: fixed; inset: 0; background: rgba(12, 10, 9, 0.9); z-index: 1001; display: none; justify-content: center; align-items: center; }
.tile-picker.open { display: flex; }
.picker-panel { background: var(--bg-deep); border-top: 3px solid var(--accent-terracotta); padding: 24px; width: 90%; max-width: 400px; }
.picker-panel h3 { font-family: var(--font-display); color: var(--accent-cream); font-weight: 400; font-size: 1.3rem; margin-bottom: 16px; }
.picker-item { display: flex; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid rgba(87, 83, 78, 0.3); cursor: pointer; }
.picker-item input[type="checkbox"] { accent-color: var(--accent-terracotta); width: 16px; height: 16px; }
.picker-name { font-family: var(--font-mono); font-size: 13px; color: var(--text-primary); flex: 1; }
.picker-badge { font-family: var(--font-mono); font-size: 8px; text-transform: uppercase; letter-spacing: 0.1em; padding: 2px 6px; }
.picker-badge.working { color: var(--accent-amber); }
.picker-badge.waiting { color: var(--accent-olive); }
.picker-badge.idle { color: var(--text-muted); }
.picker-actions { display: flex; gap: 8px; margin-top: 16px; justify-content: flex-end; }
.picker-actions button { font-family: var(--font-mono); font-size: 11px; padding: 8px 16px; border: none; cursor: pointer; text-transform: uppercase; letter-spacing: 0.05em; }
.picker-actions .btn-primary { background: var(--accent-terracotta); color: var(--accent-cream); }
.picker-actions .btn-secondary { background: var(--bg-surface); color: var(--text-secondary); border: 1px solid var(--border); }
</style>

<div class="tile-picker" id="tile-picker">
  <div class="picker-panel">
    <h3>Select Sessions</h3>
    <div id="picker-list"></div>
    <div class="picker-actions">
      <button class="btn-secondary" onclick="document.getElementById('tile-picker').classList.remove('open')">Cancel</button>
      <button class="btn-primary" onclick="launchTiledView()">Open</button>
    </div>
  </div>
</div>

<div class="tiled-overlay" id="tiled-overlay">
  <div class="tiled-bar">
    <h3>Tiled</h3>
    <div class="layout-group">
      <button class="layout-btn" data-layout="grid" onclick="setLayout('grid')">Grid</button>
      <button class="layout-btn" data-layout="master" onclick="setLayout('master')">Master</button>
      <button class="layout-btn" data-layout="columns" onclick="setLayout('columns')">Cols</button>
      <button class="layout-btn" data-layout="rows" onclick="setLayout('rows')">Rows</button>
      <button class="layout-btn" data-layout="focus" onclick="setLayout('focus')">Focus</button>
    </div>
    <div class="tiled-bar-actions">
      <button onclick="connectAllTiles()">Connect All</button>
      <button onclick="disconnectAllTiles()">Disconnect All</button>
      <span class="close-btn" onclick="closeTiledView()">&times;</span>
    </div>
  </div>
  <div class="tiled-grid" id="tiled-grid"></div>
</div>

<style>
.orch-overlay { position: fixed; inset: 0; background: rgba(12,10,9,0.97); z-index: 1100; display: none; flex-direction: column; overflow: auto; }
.orch-overlay.open { display: flex; }
.orch-bar { display: flex; justify-content: space-between; align-items: center; padding: 12px 18px; background: var(--bg-deep); border-bottom: 2px solid var(--accent-terracotta); position: sticky; top: 0; }
.orch-bar h3 { font-family: var(--font-display); color: var(--accent-cream); font-weight: 400; font-size: 1.4rem; margin: 0; }
.orch-body { padding: 18px; max-width: 900px; margin: 0 auto; width: 100%; box-sizing: border-box; }
.orch-section { margin-bottom: 22px; }
.orch-section h4 { color: var(--accent-cream); font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; margin: 0 0 10px; }
.orch-toggles { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.orch-toggle { font-family: var(--font-mono); font-size: 11px; padding: 8px 12px; border: 1px solid var(--border); background: var(--bg-surface); color: var(--text-secondary); cursor: pointer; }
.orch-toggle.on { background: var(--accent-terracotta); color: var(--accent-cream); border-color: var(--accent-terracotta); }
.orch-toggle.master.on { background: #65A30D; border-color: #65A30D; color: #0C0A09; }
.orch-form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.orch-form input[type=text] { padding: 9px; background: var(--bg-panel); border: 1px solid var(--border); color: var(--text-primary); font: inherit; font-size: 14px; }
.orch-form button { padding: 9px 16px; background: var(--accent-terracotta); color: var(--accent-cream); border: none; cursor: pointer; font-family: var(--font-mono); font-size: 11px; text-transform: uppercase; }
.orch-num { width: 60px; padding: 6px; background: var(--bg-panel); border: 1px solid var(--border); color: var(--text-primary); font: inherit; }
.orch-row { display: flex; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid rgba(87,83,78,0.3); font-family: var(--font-mono); font-size: 12px; }
.orch-hint { color: var(--text-muted); font-size: 11px; margin-top: 8px; line-height: 1.5; }
</style>
<div class="orch-overlay" id="orch-overlay">
  <div class="orch-bar">
    <h3>Orchestration</h3>
    <span onclick="closeOrch()" style="cursor:pointer;font-size:26px;color:var(--text-muted)">&times;</span>
  </div>
  <div class="orch-body">
    <div class="orch-section">
      <h4>Controls — off by default</h4>
      <div class="orch-toggles" id="orch-toggles"></div>
      <div class="orch-hint" id="orch-hint"></div>
    </div>
    <div class="orch-section">
      <h4>Spawn a session</h4>
      <div class="orch-form">
        <input type="text" id="orch-task" placeholder="Task for the agent…" style="flex:1 1 280px">
        <input type="text" id="orch-project" placeholder="project" style="flex:0 0 130px">
        <label style="font-size:11px;color:var(--text-secondary)"><input type="checkbox" id="orch-headless"> headless</label>
        <button onclick="orchSpawn()">Spawn</button>
      </div>
    </div>
    <div class="orch-section"><h4>Fleet</h4><div id="orch-fleet"></div></div>
    <div class="orch-section"><h4>Backlog</h4><div id="orch-backlog"></div></div>
  </div>
</div>
<script>
let orchInterval = null;
const ORCH_TOGGLES = [['enabled','Orchestration',true],['auto_nudge','Auto-nudge',false],['auto_spawn','Auto-spawn',false],['auto_merge','Auto-merge',false]];
function openOrch() { document.getElementById('orch-overlay').classList.add('open'); orchRefresh(); orchInterval = setInterval(() => { if (!document.hidden) orchRefresh(); }, 4000); }
function closeOrch() { document.getElementById('orch-overlay').classList.remove('open'); if (orchInterval) { clearInterval(orchInterval); orchInterval = null; } }
async function orchRefresh() {
  try { const r = await fetch('/api/orch'); if (!r.ok) return; const d = await r.json();
    if (!d.available) { document.getElementById('orch-toggles').innerHTML = '<span class="orch-hint">orchestration package unavailable</span>'; return; }
    renderOrch(d); } catch(e) {}
}
function renderOrch(d) {
  const cfg = d.config || {};
  document.getElementById('orch-toggles').innerHTML = ORCH_TOGGLES.map(([k,label,master]) => {
    const on = !!cfg[k];
    return `<span class="orch-toggle ${master?'master':''} ${on?'on':''}" onclick="orchSet('${k}', ${!on})">${label}: ${on?'ON':'off'}</span>`;
  }).join('')
    + ` <span class="orch-toggle">max <input class="orch-num" type="number" min="1" max="32" value="${cfg.max_parallel||4}" onchange="orchSet('max_parallel', parseInt(this.value)||4)"></span>`
    + ` <span class="orch-toggle">nudge risk≤ <select class="orch-num" style="width:auto" onchange="orchSet('nudge_risk_max', this.value)">${['allow','review','confirm','block'].map(o=>`<option ${cfg.nudge_risk_max===o?'selected':''}>${o}</option>`).join('')}</select></span>`;
  document.getElementById('orch-hint').textContent = cfg.enabled
    ? (cfg.auto_nudge ? 'Auto-nudge ON: routine waiting sessions are auto-continued (risk-gated; never on confirm/block actions).' : 'Read-only supervision. Turn on auto-nudge / auto-spawn / auto-merge to let it act.')
    : 'Orchestration is OFF. Turn it on for read-only supervision; the act-on-sessions toggles stay off until you enable them.';
  const sessions = (d.sessions||[]).filter(s => ['pending','running','waiting','idle','stale'].includes(s.state));
  document.getElementById('orch-fleet').innerHTML = sessions.length ? sessions.map(s => `
    <div class="orch-row"><span class="session-badge ${s.state}" style="font-size:9px;padding:2px 7px">${s.state}</span>
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.id.slice(0,8)} · ${s.project||'-'} · ${(s.task||'').slice(0,60)}</span>
      <button class="orch-toggle" onclick="orchStop('${s.id}')">stop</button></div>`).join('') : '<div class="orch-hint">no active sessions</div>';
  const bl = d.backlog||[];
  document.getElementById('orch-backlog').innerHTML = bl.length ? bl.map(b => `<div class="orch-row"><span style="flex:1">${(b.task||'').slice(0,70)}</span><span class="orch-hint">${b.project||''}</span></div>`).join('') : '<div class="orch-hint">empty</div>';
}
async function orchSet(key, val) { try { const r = await fetch('/api/orch/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({[key]: val})}); if (r.ok) orchRefresh(); } catch(e) {} }
async function orchSpawn() {
  const task = document.getElementById('orch-task').value.trim(); if (!task) return;
  const project = document.getElementById('orch-project').value.trim();
  const headless = document.getElementById('orch-headless').checked;
  try { const r = await fetch('/api/orch/spawn', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({task, project, headless})});
    const d = await r.json(); if (d.error) alert('Spawn failed: ' + d.error); else { document.getElementById('orch-task').value=''; orchRefresh(); }
  } catch(e) { alert('spawn error'); }
}
async function orchStop(id) { try { await fetch('/api/orch/session/'+id+'/stop', {method:'POST'}); orchRefresh(); } catch(e) {} }
</script>
'''


# ══════════════════════════════════════════════════════════════════════════════
# App setup
# ══════════════════════════════════════════════════════════════════════════════

def create_app():
    middlewares = [security_middleware, auth_middleware]

    app = web.Application(middlewares=middlewares)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/panes", handle_panes)
    app.router.add_get("/api/orch", handle_orch_state)
    app.router.add_post("/api/orch/config", handle_orch_config)
    app.router.add_post("/api/orch/spawn", handle_orch_spawn)
    app.router.add_post("/api/orch/session/{id}/{action}", handle_orch_session_action)
    app.router.add_post("/api/session/new", handle_session_new)
    app.router.add_get("/api/sysmon", handle_sysmon)
    app.router.add_get("/ws/sysmon", handle_ws_sysmon)
    app.router.add_get("/ws/view/{session}", handle_view)
    app.router.add_get("/ws/terminal/{session}", handle_terminal)

    # Vendored JS/CSS (xterm, qrcode) served locally — no external CDN dependency,
    # so the page never blocks on a slow/unreachable jsdelivr. Public (auth-exempt).
    app.router.add_static("/vendor/", os.path.join(os.path.dirname(__file__), "vendor"))

    app.router.add_get("/auth/login", handle_login)
    app.router.add_post("/auth/login", handle_login_submit)
    app.router.add_get("/auth/register", handle_register)
    app.router.add_post("/auth/register", handle_register_submit)
    app.router.add_get("/auth/setup", handle_setup)
    app.router.add_post("/auth/setup", handle_setup_submit)
    app.router.add_get("/auth/logout", handle_logout)

    # M2 (watchyourclankers merge): mount the read-only IDE-spectator at /wyc/.
    # Graceful no-op if wyc isn't installed (see lib/wyc_mount.py); clanker's
    # auth_middleware covers the sub-app, so /wyc/* needs the same login.
    from wyc_mount import mount_wyc
    mount_wyc(app)

    app.on_startup.append(start_background)
    app.on_cleanup.append(stop_background)
    return app


def run_server(host=None, port=None, ntfy_topic=None):
    global HOST, PORT, NTFY_TOPIC, _bootstrap_token
    if host:
        HOST = host
    if port:
        PORT = port
    if ntfy_topic:
        NTFY_TOPIC = ntfy_topic

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    app = create_app()
    url = BASE_URL.rstrip("/") if BASE_URL else f"http://{HOST}:{PORT}"
    n_users = webauth.user_count()
    print(f"\n  CLANKER DASHBOARD")
    print(f"  {'─' * 50}")
    print(f"  URL:   {url}")
    print(f"  Users: {n_users} enrolled")
    if n_users == 0:
        # First run: open a one-time, token-gated web setup link. Closes the
        # moment the first account is created.
        _bootstrap_token = secrets.token_urlsafe(24)
        print(f"  ┌─ FIRST-RUN SETUP ─────────────────────────────")
        print(f"  │ No account yet. Create one in your browser:")
        print(f"  │   {url}/auth/register?token={_bootstrap_token}")
        print(f"  │ (one-time link; or run: clanker serve-user add <name>)")
        print(f"  └────────────────────────────────────────────────")
    else:
        print(f"  Login: {url}/auth/login   (username + password + TOTP)")
    print(f"  Net:   {_NET_TYPE} ({HOST})")
    if HOST not in ("127.0.0.1", "::1", "localhost"):
        print(f"         WARNING — bound to {HOST}, reachable beyond loopback. Prefer 127.0.0.1.")
    if NTFY_TOPIC:
        print(f"  Ntfy:  {NTFY_SERVER}/{NTFY_TOPIC}")
    print(f"  {'─' * 50}\n")
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    run_server()
