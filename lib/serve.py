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

# SGR escape sequences — stripped before parsing the statusline off a pane tail
# (capture-pane -p is already plain, but keep this defensive for -e captures).
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


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
    # EXCEPT /vendor/: those libs change only with a repo update, and no-store
    # made every phone page-load re-pull ~350KB of xterm.js through the tunnel
    # (the single biggest "dashboard takes forever" cost, 2026-07-19).
    if request.path.startswith("/vendor/"):
        response.headers["Cache-Control"] = "public, max-age=604800"
    elif request.path.startswith("/app/"):
        # SPA assets change with every deploy (stable filenames), so no week-long
        # /vendor/ cache — but no-store is wasteful too. no-cache lets the browser
        # keep a copy and revalidate; the static handler answers with a cheap 304
        # off Last-Modified/ETag when unchanged.
        response.headers["Cache-Control"] = "no-cache"
    else:
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
    """Simple TTL cache. Unkeyed by default (one slot); pass a key to get()/set()
    for an endpoint whose payload varies by a request param (e.g. /api/panes?sessions=)
    — each key gets its own TTL slot so distinct selections never clobber each other."""
    def __init__(self, ttl):
        self.ttl = ttl
        self._data = None
        self._time = 0
        self._keyed = {}

    def get(self, key=None):
        now = time.time()
        if key is not None:
            hit = self._keyed.get(key)
            if hit is not None and now - hit[1] < self.ttl:
                return hit[0]
            return None
        if now - self._time < self.ttl:
            return self._data
        return None

    def set(self, data, key=None):
        now = time.time()
        if key is not None:
            self._keyed[key] = (data, now)
            # Bound the keyed map — selections churn as the operator changes tiles;
            # drop entries past their TTL so it can't grow without limit.
            if len(self._keyed) > 16:
                self._keyed = {k: v for k, v in self._keyed.items()
                               if now - v[1] < self.ttl}
            return
        self._data = data
        self._time = now


# TTL 300 > the refresher's 120s period: '/' serves warm cache always; the TTL
# only matters as a staleness ceiling if the refresher task dies.
_dashboard_cache = Cache(ttl=300)
_status_cache = Cache(ttl=4)


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


def classify_rest_state(tail):
    """Classify WHY an at-rest Claude pane is at rest, from its captured tail.

    Returns (substate, detail): substate in {'decision','limit','error','waiting'};
    detail is the most relevant matched line (box-drawing stripped), '' if none.
    The notifier previously treated every non-spinner pane as one generic
    'needs input' — a permission dialog, a usage-limit banner, an API-error
    banner and a plain finished prompt all pinged identically (operator:
    'quite naive', 2026-07-19). Pure function so the patterns are testable.
    """
    lines = [l.strip(" │╭╮╰╯─┃┏┓┗┛┌┐└┘\t") for l in (tail or "").split("\n")]
    text = "\n".join(lines)
    low = text.lower()

    def _detail(rx):
        for l in reversed(lines):
            if l and re.search(rx, l, re.I):
                return l[:140]
        return ""

    dec_rx = r"do you want|always allow|allow once|don'?t ask again"
    has_options = (re.search(r"(?m)^\s*❯?\s*1\.\s", text)
                   and re.search(r"(?m)^\s*2\.\s", text))
    if re.search(dec_rx, low) or has_options:
        q = _detail(r"do you want") or _detail(r"\?\s*$") or _detail(r"^\s*❯?\s*1\.\s")
        return "decision", q

    lim_rx = r"usage limit|rate limit|limit reached|weekly limit|5-hour limit|resets? (at|in)\b"
    if re.search(lim_rx, low):
        return "limit", _detail(lim_rx)

    err_rx = (r"api error|overloaded|connection (error|refused|failed)"
              r"|request timed out|failed to connect|offline")
    if re.search(err_rx, low):
        return "error", _detail(err_rx)

    return "waiting", ""


def classify_working_state(tail):
    """Classify WHAT a working (spinner) pane is doing, from its captured tail.

    Returns (wsub, agents, detail): wsub in {'subagents','compacting','working'};
    agents = best-effort count of live subagents (0 when none detected); detail
    = the most informative activity line. Exhaustive-states request, 2026-07-19:
    'working' alone hid fan-outs and compaction stalls from the fleet view."""
    lines = [l.rstrip() for l in (tail or "").split("\n")]
    text = "\n".join(lines)
    low = text.lower()

    if re.search(r"compacting (the )?conversation|context left until auto-compact: 0", low):
        return "compacting", 0, _last_activity_line(tail)

    agents = 0
    m = re.search(r"(\d+)\s+agents?\s+(running|working)", low)
    if m:
        agents = int(m.group(1))
    task_lines = [l.strip() for l in lines if re.search(r"⏺?\s*Task\(", l)]
    if not agents and task_lines:
        agents = len(task_lines)
    if re.search(r"\bsubagents?\b", low) and not agents:
        agents = 1
    if agents:
        detail = task_lines[-1][:140] if task_lines else _last_activity_line(tail)
        return "subagents", agents, detail

    return "working", 0, _last_activity_line(tail)


def _last_activity_line(tail):
    """The most recent line that says WHAT is happening: prefer Claude's ⏺
    action/summary lines, fall back to the last non-chrome content line."""
    lines = (tail or "").split("\n")
    for line in reversed(lines):
        s = line.strip()
        if s.startswith("⏺"):
            return s[:140]
    for line in reversed(lines):
        s = line.strip()
        if s and "─" not in s and "❯" not in s and "│" not in s:
            return s[:140]
    return ""


def _craft_notification(sid, tail):
    """Build (title, priority, tags, body) for a stable at-rest session's ping,
    by rest-substate — a blocking dialog outranks a finished turn outranks a
    limit banner (nothing to do until reset)."""
    sub, detail = classify_rest_state(tail)
    if sub == "decision":
        return (f"{sid}: decision needed", "max", "warning",
                detail or "Claude is waiting on an approval/choice dialog")
    if sub == "limit":
        return (f"{sid}: usage limit hit", "default", "hourglass_flowing_sand",
                detail or "Usage-limit banner showing")
    if sub == "error":
        return (f"{sid}: erroring", "high", "rotating_light",
                detail or "Error banner showing")
    body = _last_activity_line(tail)
    return (f"{sid} finished — your turn", "high", "robot",
            body or "Claude Code is waiting for input")


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
        # Web client must not fight the desktop over window size. 'largest'
        # while the bridge lives, and the PREVIOUS value is restored on close —
        # the old code set it permanently, so one tiled-view visit left every
        # target session resized against its desktop clients (the operator's
        # 3-monitor mess, 2026-07-19).
        self._prev_window_size = None
        try:
            out = subprocess.run(
                ["tmux", "show-options", "-t", self.target, "window-size"],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            if out.startswith("window-size "):
                self._prev_window_size = out.split(None, 1)[1]
        except Exception:
            pass
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
        # Restore the target's pre-bridge window-size (see start()). Restores
        # only when NO other bridge remains anywhere (conservative: a tiled
        # view holds many bridges; restoring under a live one would flip its
        # target mid-use — worst case here is a restore deferred to last-close).
        if getattr(self, "_prev_window_size", None) and not any(
            b for b in _active_bridges if b != self.web_session
        ):
            subprocess.run(
                ["tmux", "set-option", "-t", self.target,
                 "window-size", self._prev_window_size],
                capture_output=True, timeout=3,
            )


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

def _stamp_assets(html):
    """Version-stamp the /app/ asset URLs with the process BUILD_ID.

    The page itself is no-store, so every load carries current URLs; a build
    change forces every intermediary (browser heuristics, Cloudflare edge,
    bfcache-restored documents doing a late fetch) to refetch the assets.
    Without this, 'did the deploy land on the phone' was unanswerable
    (operator hit exactly that, 2026-07-19)."""
    for asset in ("app/app.css", "app/app.js", "app/live.js", "app/reader.js"):
        html = html.replace(f"/{asset}", f"/{asset}?v={BUILD_ID}")
    return html


async def handle_index(request):
    """Serve the live dashboard from the warm cache (see dashboard_refresher).

    The build costs ~0.9s (registry git subprocesses + session-store scans);
    paying it inline on the first request after any idle >TTL stacked a full
    second on top of tunnel RTT — the refresher pays it off-request instead."""
    cached = _dashboard_cache.get()
    if cached:
        resp = web.Response(text=cached, content_type="text/html")
        resp.enable_compression()
        return resp

    # Cold start only (first request racing the refresher's first build).
    from dashboard import _build_html
    from dashboard_data import generate_dashboard_data
    data = await asyncio.to_thread(generate_dashboard_data)
    data_json = json.dumps(data, indent=None)
    base_html = _build_html(data_json)
    html = _stamp_assets(
        base_html.replace("</body>", _live_features_html() + "\n</body>"))
    _dashboard_cache.set(html)
    resp = web.Response(text=html, content_type="text/html")
    resp.enable_compression()
    return resp


async def handle_status(request):
    """Return current tmux session status (cached 2s)."""
    cached = _status_cache.get()
    if cached:
        resp = web.json_response(cached)
        resp.enable_compression()
        return resp

    panes = list_panes()
    sessions = []
    for p in panes:
        if p["command"] != "claude":
            continue
        state = detect_session_state(p)
        # 12 lines: enough to see a dialog/banner/agent-tree for substates,
        # not just the 3-line preview.
        preview = capture_pane_tail(p["target"], lines=12)
        substate = ""
        agents = 0
        if state == "waiting":
            substate, _ = classify_rest_state(preview)
        elif state == "working":
            wsub, agents, _ = classify_working_state(preview)
            substate = "" if wsub == "working" else wsub
        preview_line = ""
        for line in reversed(preview.split("\n")):
            s = line.strip()
            if s and "─" not in s and "❯" not in s and "═" not in s:
                preview_line = s[:120]
                break
        # Statusline chips (items 2+4): the pane bottom carries Claude Code's
        # statusline — "<model> | ctx:NN%/1M | <branch> | … | 5h:NN% ↻ | 7d:NN% …".
        # Parse it off the preview we already captured (no extra capture-pane).
        model = ctx = b5h = b7d = None
        # bottom-up: the statusline is the pane's last line — prefer it over any
        # coincidental "ctx:" text higher up in the captured conversation.
        for line in reversed(_SGR_RE.sub("", preview).split("\n")):
            if "ctx:" not in line:
                continue
            m = re.search(r"ctx:(\d+)%", line)
            if not m:
                continue
            ctx = int(m.group(1))
            model = line.split(" | ", 1)[0].strip() or None
            m = re.search(r"5h:(\d+)%", line)
            if m:
                b5h = int(m.group(1))
            m = re.search(r"7d:(\d+)%", line)
            if m:
                b7d = int(m.group(1))
            break
        sessions.append({
            "session": p["session"],
            "target": p["target"],
            "title": p["title"],
            "command": p["command"],
            "state": state,
            "substate": substate,
            "agents": agents,
            "preview": preview_line,
            "model": model,
            "ctx": ctx,
            "b5h": b5h,
            "b7d": b7d,
            "size": f"{p['width']}x{p['height']}",
        })

    result = {"sessions": sessions, "ntfy_configured": bool(NTFY_TOPIC)}
    _status_cache.set(result)
    resp = web.json_response(result)
    resp.enable_compression()
    return resp


async def handle_reader(request):
    """Reader mode (mobile flagship, 2026-07-19): the session transcript's tail
    as message units — see lib/reader.py + docs/MOBILE-READER.md. Incremental
    via ?offset=<byte>; resolution + parsing run off-loop (subprocess + file IO)."""
    name = request.match_info["session"]
    if name.startswith("web-") or not _SESSION_NAME_RE.match(name):
        return web.json_response({"error": "invalid session name"}, status=400)
    off_raw = request.query.get("offset", "")
    offset = int(off_raw) if off_raw.isdigit() else None
    before_raw = request.query.get("before", "")
    before = int(before_raw) if before_raw.isdigit() else None
    import reader as _reader
    path = await asyncio.to_thread(_reader.resolve_transcript, name)
    if not path:
        return web.json_response(
            {"units": [], "offset": 0, "reset": True, "error": "no-transcript"})
    if before is not None:
        out = await asyncio.to_thread(_reader.parse_window, path, before)
    else:
        out = await asyncio.to_thread(_reader.parse_tail, path, offset)
    out["session"] = name
    resp = web.json_response(out)
    resp.enable_compression()
    return resp


_panes_cache = Cache(ttl=4)

async def handle_panes(request):
    """Return per-Claude-pane state + captured content for the tiled monitor view.

    ?sessions=a,b,c (the tiles the client is actually showing) restricts the
    EXPENSIVE capture_pane_tail(40 lines) to those sessions — every claude pane is
    still returned with its cheap state, so the tile switcher's session list stays
    complete, but off-screen panes carry empty content instead of a 40-line grab.
    The cache is keyed by the (normalized) selection so distinct views don't clobber."""
    sel_raw = request.query.get("sessions", "")
    sel = {s for s in sel_raw.split(",") if s} if sel_raw else None
    cache_key = ",".join(sorted(sel)) if sel else ""
    cached = _panes_cache.get(cache_key)
    if cached:
        resp = web.json_response(cached)
        resp.enable_compression()
        return resp

    panes = list_panes()
    result = []
    for p in panes:
        if p["session"].startswith("web-") or p["command"] != "claude":
            continue
        state = detect_session_state(p)
        # Only pay the 40-line capture for panes the client is actually showing.
        content = (capture_pane_tail(p["target"], lines=40)
                   if sel is None or p["session"] in sel else "")
        result.append({
            "session": p["session"],
            "target": p["target"],
            "state": state,
            "content": content,
            "width": p["width"],
            "height": p["height"],
        })

    _panes_cache.set(result, cache_key)
    resp = web.json_response(result)
    resp.enable_compression()
    return resp


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


# Native system monitor — the dashboard's floating compute-load view watches the
# SSH target named by CLANKER_SYSMON_SSH (set in the systemd unit; typically the
# hypervisor this VM runs on). This is strictly READ-ONLY: the
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
# Warm caches: a background warmer samples every ~20s so opening the monitor
# paints INSTANTLY from cache instead of paying SSH handshake + the sampler's
# 1s rate-delta sleep on-request (~2-3s of 'sluggish', operator 2026-07-19).
# ControlMaster reuses one SSH connection across all samples.
_sysmon_cache = Cache(ttl=300)   # S7: lazy-warm — the warmer keeps this fresh; a
_gpu_cache = Cache(ttl=45)       # cold open is served from here (no on-request SSH).
_sysmon_last_touch = 0.0         # time.time() of the last monitor open (handle_sysmon / ws accept)
SYSMON_GPU_SSH = os.environ.get("CLANKER_SYSMON_GPU_SSH", "")  # e.g. root@<gpu-ct>

_SSH_MUX = ["-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/clanker-ssh-%r@%h",
            "-o", "ControlPersist=120"]


def _sysmon_sample_host():
    try:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=8", *_SSH_MUX,
             "-i", SYSMON_SSH_KEY, SYSMON_SSH_HOST, "python3", "-"],
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


def _sysmon_sample_gpus():
    """nvidia-smi on the GPU host (CT with the passed-through card). Read-only."""
    if not SYSMON_GPU_SSH:
        return []
    try:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=6", *_SSH_MUX,
             "-i", SYSMON_SSH_KEY, SYSMON_GPU_SSH,
             "nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,"
             "memory.total,temperature.gpu,power.draw,power.limit",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if p.returncode != 0:
            return []
        gpus = []
        for row in p.stdout.strip().splitlines():
            parts = [c.strip() for c in row.split(",")]
            if len(parts) < 7:
                continue
            def _num(v):
                try:
                    return float(v)
                except ValueError:
                    return None
            gpus.append({"name": parts[0], "util": _num(parts[1]) or 0,
                         "mem_used": _num(parts[2]) or 0, "mem_total": _num(parts[3]) or 0,
                         "temp": _num(parts[4]), "power": _num(parts[5]),
                         "plimit": _num(parts[6])})
        return gpus
    except subprocess.SubprocessError:
        return []


async def sysmon_warmer(app):
    """Keep host+GPU snapshots warm off-request. Lazy cadence (S7): 20s while the
    monitor was opened in the last 10min, 120s when nobody's looking — the 300s
    cache TTL still covers the slow cadence so a cold open is never stale."""
    if not SYSMON_SSH_HOST:
        return
    while True:
        try:
            data = await asyncio.to_thread(_sysmon_sample_host)
            gpus = await asyncio.to_thread(_sysmon_sample_gpus)
            if gpus:
                _gpu_cache.set(gpus)
            if isinstance(data, dict) and not data.get("error"):
                data["gpus"] = _gpu_cache.get() or []
                _sysmon_cache.set(data)
        except Exception as e:
            log.debug("sysmon warmer error: %s", e)
        await asyncio.sleep(20 if time.time() - _sysmon_last_touch < 600 else 120)


async def handle_sysmon(request):
    """One-shot system snapshot (host + gpus) as JSON — served from the warm
    cache; a cold miss (just after boot) samples inline."""
    global _sysmon_last_touch
    _sysmon_last_touch = time.time()   # S7: a look re-arms the fast warm cadence
    cached = _sysmon_cache.get()
    if cached is not None:
        return web.json_response(cached)
    data = await asyncio.to_thread(_sysmon_sample_host)
    if not isinstance(data, dict) or data.get("error"):
        return web.json_response(
            data if isinstance(data, dict) else {"error": "no data"}, status=502)
    data["gpus"] = _gpu_cache.get() or []
    _sysmon_cache.set(data)
    return web.json_response(data)


MAX_SYSMON_STREAMS = 4   # concurrent real-time monitor streams (each = one SSH loop)
_sysmon_streams = 0


async def handle_ws_sysmon(request):
    """Stream real-time system snapshots over a WebSocket. Opens ONE persistent
    `ssh … python3 - loop` running the sampler in a loop on the host and forwards
    each JSON line. Still strictly read-only. The remote sampler self-exits the
    moment this socket closes (its next stdout write breaks) — verified no orphan."""
    global _sysmon_streams, _sysmon_last_touch
    if not _ws_origin_ok(request):
        return web.Response(status=403, text="Origin not allowed")
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _sysmon_last_touch = time.time()   # S7: an open stream re-arms the fast warm cadence
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
                # attach the latest GPU snapshot to each streamed host frame
                out = line.decode("utf-8", "replace")
                gpus = _gpu_cache.get()
                if gpus:
                    try:
                        frame = json.loads(out)
                        frame["gpus"] = gpus
                        out = json.dumps(frame)
                    except (json.JSONDecodeError, TypeError):
                        pass
                await ws.send_str(out)
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


async def _post_ntfy(http, title, priority, tags, body, now, muted_until):
    """POST one ntfy message. Returns (delivered, muted_until) — 'delivered'
    only on a 200 (evidence, never assumption); a 429 arms the 30-min mute."""
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    try:
        async with http.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body, headers=headers, timeout=10,
        ) as resp:
            if resp.status == 200:
                log.info("Notified: %s", title)
                return True, muted_until
            if resp.status == 429:
                log.warning("ntfy daily quota hit (429) — muting publishes 30 min")
                return False, now + 1800
            log.warning("ntfy rejected (%s): %s", resp.status,
                        (await resp.text())[:200])
    except Exception as e:
        log.warning("ntfy failed: %s", e)
    return False, muted_until


async def monitor_sessions(app):
    """Poll Claude sessions and ntfy on every state worth the operator's attention.

    Signals (exhaustive-states pass, 2026-07-19):
      - working→waiting settle, classified: decision (max prio, the question) /
        limit (the reset line) / error / finished (last ⏺ activity line)
      - subagent fan-out: first sight of agent activity per working episode
        (low prio, informational, says what fanned out)
      - vanished-mid-work: pane gone while last seen working (crash/kill)
      - subagent limit-kills: new pending entries in agent_resume_queue.jsonl

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
    agents_pinged = set()  # sessions already pinged for their current fan-out
    fanout_check = {}      # sid -> ts of last fan-out capture_pane_tail (S2: throttle to 1/30s)
    # Subagent limit-kill watcher: the SubagentStop hook appends to this queue;
    # start at the current size so historical entries never burst on restart.
    queue_path = os.path.expanduser("~/.claude/agent_resume_queue.jsonl")
    queue_offset = os.path.getsize(queue_path) if os.path.exists(queue_path) else 0

    async with ClientSession() as http:
        while True:
            await asyncio.sleep(5)
            try:
                panes = [p for p in list_panes() if p["command"] == "claude"]
                live = {p["session"] for p in panes}
                now = time.time()

                # Drop bookkeeping for sessions that disappeared. A session that
                # vanishes while last observed WORKING likely crashed / was
                # OOM-killed / hit a hard limit — that used to become silence
                # (state naivety fix, 2026-07-19). One aggregate ping when
                # several vanish together (tmux server restart) instead of a burst.
                gone = [s for s in prev_state if s not in live]
                crashed = [s for s in gone if prev_state.get(s) == "working"]
                for sid in gone:
                    prev_state.pop(sid, None)
                    waiting_since.pop(sid, None)
                    last_ping.pop(sid, None)
                    notified.discard(sid)
                    fanout_check.pop(sid, None)
                if crashed and NTFY_TOPIC and now >= muted_until:
                    names = ", ".join(sorted(crashed)[:6])
                    title = (f"{crashed[0]} vanished mid-work" if len(crashed) == 1
                             else f"{len(crashed)} sessions vanished mid-work")
                    _, muted_until = await _post_ntfy(
                        http, title, "high", "skull",
                        f"tmux pane(s) disappeared while working: {names}",
                        now, muted_until)
                for sid in gone:
                    agents_pinged.discard(sid)

                # Subagent limit-kills: the SubagentStop hook queues them in
                # agent_resume_queue.jsonl — new pending entries earn a ping so
                # a dead lane isn't discovered hours later at the desk. Offset
                # advances only once processed (or when ntfy is unconfigured),
                # so a quota-mute delays the ping instead of dropping it.
                try:
                    qsize = (os.path.getsize(queue_path)
                             if os.path.exists(queue_path) else 0)
                    if qsize < queue_offset:
                        queue_offset = 0  # queue truncated/cleared
                    if qsize > queue_offset:
                        if not NTFY_TOPIC:
                            queue_offset = qsize
                        elif now >= muted_until:
                            with open(queue_path) as qf:
                                qf.seek(queue_offset)
                                fresh = qf.read()
                            queue_offset = qsize
                            for qline in fresh.strip().split("\n"):
                                if not qline.strip():
                                    continue
                                try:
                                    e = json.loads(qline)
                                except json.JSONDecodeError:
                                    continue
                                if e.get("status") != "pending":
                                    continue
                                proj = os.path.basename(e.get("cwd", "")) or "?"
                                body = (e.get("prompt", "") or "").strip()[:120] \
                                    or "queued for resume"
                                reset = e.get("reset_hint", "")
                                if reset and reset != "unknown":
                                    body = f"resets {reset} — {body}"
                                _, muted_until = await _post_ntfy(
                                    http, f"subagent limit-killed: {proj}",
                                    "high", "zzz", body, now, muted_until)
                except Exception as e:
                    log.debug("resume-queue watch error: %s", e)

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
                        # Fan-out visibility: first sight of subagent activity in
                        # this working episode -> one informational ping saying
                        # what fanned out (exhaustive-states request, 2026-07-19).
                        # Throttle the fan-out capture to at most once per 30s per
                        # session (S2): a pane with no subagents never sets
                        # agents_pinged, so without this the capture would run every
                        # 5s loop for the whole working episode.
                        if (state == "working" and sid not in agents_pinged
                                and NTFY_TOPIC and now >= muted_until
                                and now - fanout_check.get(sid, 0.0) >= 30):
                            fanout_check[sid] = now
                            wtail = capture_pane_tail(p["target"], lines=12)
                            wsub, agents, detail = classify_working_state(wtail)
                            if wsub == "subagents":
                                agents_pinged.add(sid)
                                _, muted_until = await _post_ntfy(
                                    http, f"{sid}: {agents} agent(s) fanned out",
                                    "low", "gear",
                                    detail or "subagents working", now, muted_until)
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
                    # a stable wait closes the working episode: next fan-out pings again
                    agents_pinged.discard(sid)

                    if not NTFY_TOPIC:
                        continue
                    # One ping per session per cooldown window — ntfy.sh's daily
                    # per-IP quota can't absorb a ping for every work→wait cycle.
                    if now - last_ping.get(sid, 0.0) < NOTIFY_COOLDOWN_SECS:
                        continue
                    if now < muted_until:
                        continue
                    # 15 lines so a permission dialog / limit banner / error
                    # banner above the prompt is visible to the classifier —
                    # the ping now says WHAT the session is waiting on.
                    tail = capture_pane_tail(p["target"], lines=15)
                    title, priority, tags, body = _craft_notification(sid, tail)
                    delivered, muted_until = await _post_ntfy(
                        http, title, priority, tags, body, now, muted_until)
                    if delivered:
                        last_ping[sid] = now
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


async def dashboard_refresher(app):
    """Keep the dashboard HTML cache warm OFF-request.

    generate_dashboard_data costs ~0.9s (registry git subprocesses + session
    store scans). Built inline, the first request after any idle > cache TTL
    stacked that on top of tunnel RTT — the recurring 'dashboard takes forever'
    (2026-07-19). Refresh every 120s < the 300s TTL, so '/' always serves warm."""
    from dashboard import _build_html
    from dashboard_data import generate_dashboard_data
    first = True
    while True:
        try:
            t0 = time.time()
            data = await asyncio.to_thread(generate_dashboard_data)
            html = _stamp_assets(
                _build_html(json.dumps(data, indent=None)).replace(
                    "</body>", _live_features_html() + "\n</body>"))
            _dashboard_cache.set(html)
            (log.info if first else log.debug)(
                "dashboard cache warmed in %.2fs", time.time() - t0)
            first = False
        except Exception as e:
            log.warning("dashboard refresh failed: %s", e)
        await asyncio.sleep(120)


async def start_background(app):
    cleanup_web_sessions()
    app["monitor_task"] = asyncio.create_task(monitor_sessions(app))
    app["reaper_task"] = asyncio.create_task(session_reaper(app))
    app["orch_task"] = asyncio.create_task(orch_supervise(app))
    app["dashboard_task"] = asyncio.create_task(dashboard_refresher(app))
    app["sysmon_task"] = asyncio.create_task(sysmon_warmer(app))


async def stop_background(app):
    for key in ("monitor_task", "reaper_task", "orch_task", "dashboard_task",
                "sysmon_task"):
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
    """Markup appended before </body> for the live features (terminal, sysmon,
    tiled view, orchestration). The CSS/JS now live in lib/web/app.css +
    lib/web/live.js (served at /app/); this returns only the vendor <script>
    tags, the static overlay markup, and the deferred /app/live.js include —
    which runs after app.js (defer preserves document order)."""
    return '''
<!-- ─── Live Features: xterm.js + terminal + notifications ─── -->
<link rel="stylesheet" href="/vendor/xterm.min.css">
<!-- A8: xterm.min.js + addon-webgl/fit/web-links are loaded lazily by live.js's
     ensureXterm() on the first terminal open (desktop terminal / tiled connect);
     the mobile reader/view paths never pull them. -->
<script defer src="/vendor/marked.min.js"></script>
<script defer src="/vendor/purify.min.js"></script>
<script defer src="/app/reader.js"></script>
<!-- Pretext available at https://esm.sh/@chenglou/pretext@0.0.4 — will be used when we move to Canvas renderer -->

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
      <button class="terminal-hdr-btn" id="terminal-mode-btn" onclick="toggleMobileMode()" title="Reader ⇄ Terminal (mobile)">📖</button>
      <button class="terminal-hdr-btn" onclick="toggleComposeMode()" title="Compose bar (native autocorrect/glide typing; Enter sends the whole line)">⌨</button>
      <button class="terminal-hdr-btn" onclick="openSysmon()" title="System monitor">▤</button>
      <span class="terminal-close" onclick="closeTerminal()">&times;</span>
    </div>
  </div>
  <div class="terminal-body" id="terminal-body"></div>
  <div class="compose-bar" id="compose-bar">
    <textarea id="compose-input" rows="1" placeholder="Message — Enter sends, Shift+Enter = newline" enterkeyhint="send" autocomplete="on" autocapitalize="sentences" spellcheck="true"></textarea>
    <button class="compose-send" id="compose-send" title="Send">➤</button>
  </div>
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

<!-- Tiled View Overlay -->

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

<script defer src="/app/live.js"></script>
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
    app.router.add_get("/api/reader/{session}", handle_reader)
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
    # The dashboard SPA's own CSS/JS (extracted from the Python HTML strings).
    # Behind auth like the page itself (not exempted below); the static handler's
    # Last-Modified/ETag pairs with the /app/ no-cache header so reloads 304 cheaply.
    app.router.add_static("/app/", os.path.join(os.path.dirname(__file__), "web"))

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
