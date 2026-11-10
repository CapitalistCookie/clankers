#!/usr/bin/env python3
"""Refresh the Fable weekly usage cache that the Claude Code statusline reads.

~/.claude/statusline-command.sh prints F5 from ~/.cache/clanker/usage.json.
When that cache is older than 300 s, the statusline starts this script,
detached. One run makes at most one GET to the plan-usage endpoint that
Claude Code's /usage command uses:

    https://api.anthropic.com/api/oauth/usage

Rules (operator, 2026-09-24):
  * Read the OAuth access token from <config dir>/.credentials.json on every
    run, because Claude Code rotates it. Never store it anywhere else, never
    print it, never call a token or refresh endpoint.
  * Write the cache only on HTTP 200 with a parseable Fable weekly entry.
  * On any failure keep the old cache and append one line to usage.err,
    rotated to usage.err.1 at 50 KB.
  * usage.next holds the earliest epoch at which the statusline may start
    another run. Success and failure both push it out by 300 s; a 429 with a
    longer Retry-After pushes it out by that instead.
  * An flock on usage.lock keeps concurrent runs to one.

The endpoint is internal to Claude Code and undocumented, so its shape can
change. When it does, the statusline drops F5 and usage.err says why.

Account slots (2026-10-29, clanker docs/SESSION-ACCOUNTS.md): every account
slot has its own credentials and its own Fable weekly figure. The config dir is
the one the calling session uses (CLAUDE_CONFIG_DIR, inherited from Claude Code
through the statusline; unset = ~/.claude). The home dir keeps the cache in the
cache directory itself; any other dir gets the subdirectory cfg-<sha256[:10] of
its real path>, so one slot's figure never shows in another slot's session.

Usage: statusline-usage-refresh.py [--force]
  --force  fetch even when the cache is fresh (manual runs).
Test hooks: CLANKER_USAGE_DIR overrides the cache directory, and
CLANKER_USAGE_PATH overrides the URL path. The host is fixed.
"""
import datetime
import fcntl
import hashlib
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "https://api.anthropic.com"
DEFAULT_PATH = "/api/oauth/usage"
TTL = 300             # s: a cache entry counts as fresh (same as the statusline)
FAIL_BACKOFF = 300    # s: earliest retry after a failure
ERR_MAX = 50 * 1024   # bytes: rotate usage.err at this size
TIMEOUT = 5           # s: socket timeout for the request
HARD_LIMIT = 20       # s: SIGALRM ends a run stuck in DNS or TLS


class HardLimit(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib forwards the Authorization header on redirects, even to another
    host. Refuse every redirect so the token only ever goes to HOST."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def config_dir():
    """The config dir of the session this refresh is for (unset = ~/.claude)."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def credentials_path():
    return os.path.join(config_dir(), ".credentials.json")


def cache_dir():
    """The cache directory for this session's config dir (see the docstring)."""
    base = os.environ.get("CLANKER_USAGE_DIR") or os.path.expanduser("~/.cache/clanker")
    real = os.path.realpath(config_dir())
    if real == os.path.realpath(os.path.expanduser("~/.claude")):
        return base
    return os.path.join(base, "cfg-" + hashlib.sha256(real.encode()).hexdigest()[:10])


def endpoint_url():
    path = os.environ.get("CLANKER_USAGE_PATH") or DEFAULT_PATH
    if not path.startswith("/"):
        raise ValueError("CLANKER_USAGE_PATH must start with /")
    url = HOST + path
    if urllib.parse.urlsplit(url).hostname != "api.anthropic.com":
        raise ValueError("refusing a URL whose host is not api.anthropic.com")
    return url


def utc(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_atomic(path, text):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def log_err(d, msg):
    path = os.path.join(d, "usage.err")
    try:
        if os.path.getsize(path) >= ERR_MAX:
            os.replace(path, path + ".1")
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write("%s %s\n" % (utc(time.time()), msg))


def read_cache(d):
    try:
        with open(os.path.join(d, "usage.json")) as f:
            c = json.load(f)
        return c if isinstance(c, dict) else None
    except (OSError, ValueError):
        return None


def cache_age(c, now):
    t = c.get("fetched_at") if c else None
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        return None
    return int(now - t)


def read_token():
    """The Claude Code OAuth access token, read fresh from disk. Returns
    (token, expires_at_seconds_or_None) or raises ValueError. Never logged."""
    with open(credentials_path()) as f:
        creds = json.load(f)
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("no claudeAiOauth access token in the credentials file")
    exp = oauth.get("expiresAt")
    exp = exp / 1000.0 if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None
    return token, exp


def fetch(token, url):
    """One GET with the headers Claude Code's /usage sends. Returns
    (status, retry_after_seconds_or_None, body_bytes)."""
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
    })
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            return r.status, None, r.read()
    except urllib.error.HTTPError as e:
        ra = None
        try:
            ra = int(float(e.headers.get("Retry-After", "")))
        except (TypeError, ValueError):
            pass
        return e.code, ra, b""


def parse_iso(s):
    dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(round(dt.timestamp()))


def fable_scoped(data):
    """True when limits[] holds any weekly_scoped entry for a Fable model, parseable
    or not. A well-formed limits[] without one means the account has no Fable weekly
    limit (2026-10-29: the home login answers only `session` and `weekly_all`)."""
    for item in data.get("limits") or [] if isinstance(data, dict) else []:
        if isinstance(item, dict) and item.get("kind") == "weekly_scoped":
            scope = item.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            name = model.get("display_name") if isinstance(model, dict) else None
            if isinstance(name, str) and name.lower().startswith("fable"):
                return True
    return False


def parse_fable(data):
    """The Fable weekly entry of the endpoint's limits[] list, or None."""
    if not isinstance(data, dict) or not isinstance(data.get("limits"), list):
        return None
    for item in data["limits"]:
        if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
            continue
        scope = item.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        if not isinstance(name, str) or not name.lower().startswith("fable"):
            continue
        pct = item.get("percent")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            continue
        try:
            reset = parse_iso(item["resets_at"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        return {"name": name, "percent": pct, "resets_at": reset}
    return None


def _alarm(signum, frame):
    raise HardLimit("run exceeded %d s" % HARD_LIMIT)


def main(argv):
    force = "--force" in argv[1:]
    os.umask(0o077)
    d = cache_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    lock = open(os.path.join(d, "usage.lock"), "a")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("skip: another refresh holds the lock")
            return 0
        return refresh(d, force)
    finally:
        lock.close()


def refresh(d, force):
    """One refresh under the lock. Returns the exit code."""
    now = time.time()
    old = read_cache(d)
    age = cache_age(old, now)
    if not force and age is not None and 0 <= age < TTL:
        print("skip: cache is fresh (age %d s)" % age)
        return 0
    kept = "cache kept (age %s)" % ("%d s" % age if age is not None else "none")

    def fail(msg, backoff=FAIL_BACKOFF):
        nxt = int(time.time()) + backoff
        log_err(d, "%s; %s; next try %s" % (msg, kept, utc(nxt)))
        write_atomic(os.path.join(d, "usage.next"), "%d\n" % nxt)
        print("failed: %s; %s" % (msg, kept))
        return 1

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(HARD_LIMIT)
    try:
        try:
            url = endpoint_url()
            token, exp = read_token()
        except (OSError, ValueError) as e:
            return fail("credentials or config: %s" % e)
        if exp is not None and exp <= time.time():
            return fail("access token expired at %s; Claude Code renews it on its next API call; not calling" % utc(exp))
        try:
            status, retry_after, body = fetch(token, url)
        except HardLimit as e:
            return fail("timeout: %s" % e)
        except Exception as e:  # URLError, TimeoutError, OSError, ssl and http.client errors
            text = "%s: %s" % (type(getattr(e, "reason", e)).__name__, getattr(e, "reason", e))
            if token and token in text:
                text = text.replace(token, "<redacted>")
            return fail("network: %s" % text[:160])
        finally:
            token = None
        if status != 200:
            backoff = FAIL_BACKOFF
            msg = "HTTP %d" % status
            if retry_after is not None:
                msg += "; retry-after %d s" % retry_after
                if status == 429:
                    backoff = max(FAIL_BACKOFF, retry_after)
            return fail(msg, backoff)
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        fable = parse_fable(data)
        if fable is None and (not isinstance(data, dict) or not isinstance(data.get("limits"), list)
                              or fable_scoped(data)):
            return fail("HTTP 200 without a parseable Fable weekly entry in limits[]")
        t = int(time.time())
        # fable None: this account has no Fable weekly limit — the statusline shows no F5
        entry = {"v": 1, "fetched_at": t, "source": urllib.parse.urlsplit(url).path, "fable": fable}
        write_atomic(os.path.join(d, "usage.json"), json.dumps(entry, separators=(",", ":")) + "\n")
        write_atomic(os.path.join(d, "usage.next"), "%d\n" % (t + TTL))
        print("ok: F5 %s%% resets %s" % (fable["percent"], utc(fable["resets_at"])) if fable
              else "ok: this account has no Fable weekly limit (limits[]: %s); F5 hidden"
              % ", ".join(str(i.get("kind")) for i in data["limits"] if isinstance(i, dict)))
        return 0
    except HardLimit as e:
        return fail("timeout: %s" % e)
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
