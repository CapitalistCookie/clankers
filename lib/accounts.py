"""Account slots for Claude sessions — the box account layer, seen from clanker.

A slot is one Claude account: its own CLAUDE_CONFIG_DIR (credentials, transcripts,
session registry, file history). Slots, usage figures and leases come from an
accounts command: `claude-acct` on PATH by default (the box layer), or
CLANKER_ACCOUNTS_CMD (`none` turns it off). Without one, clanker runs in
single-slot mode: one slot, `default`, the environment Claude would use anyway.

Rules (docs/SESSION-ACCOUNTS.md):
- The user always selects the slot of a new session. Nothing here picks one.
- The slot of a running session is read from its process: CLAUDE_CONFIG_DIR in
  /proc/<pid>/environ, unset = the home dir. Never from a state file.
- The home slot (the one that resolves to ~/.claude) launches with
  CLAUDE_CONFIG_DIR UNSET: with it set, Claude keeps its state file inside the
  dir (~/.claude/.claude.json) instead of ~/.claude.json, a different file.
- A user session never fails on policy (caps, fences, fair share): the lease
  records the reasons as warnings. Only a slot that cannot run is refused.
- Every environment variable is read at call time (repo law 9).
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import time

SINGLE = "default"            # the one slot of single-slot mode
LIST_TTL = 10.0               # s: how long a slot list is reused within one process
LEASE_TTL = 86400             # s: a user lease lives 24 h unless renewed; its pid ends it sooner
HOLDER_PREFIX = "clanker:"
_cache = {}


class AccountsError(Exception):
    """The accounts command is missing, failed, or answered something unreadable."""


# ─── the accounts command ──────────────────────────────────────────────────

def accounts_cmd():
    """argv of the accounts command, or None in single-slot mode. Looked up on PATH,
    then in the user's own bin dirs: a systemd service or a cron job runs with a PATH
    that lacks ~/bin (2026-10-29 deploy: the dashboard fell back to single-slot mode
    and read the home slot's registry only)."""
    v = os.environ.get("CLANKER_ACCOUNTS_CMD")
    if v is not None:
        v = v.strip()
        return None if not v or v.lower() == "none" else shlex.split(v)
    p = shutil.which("claude-acct")
    if not p:
        for d in ("~/bin", "~/.local/bin"):
            c = os.path.join(os.path.expanduser(d), "claude-acct")
            if os.path.isfile(c) and os.access(c, os.X_OK):
                p = c
                break
    return [p] if p else None


def enabled():
    return accounts_cmd() is not None


def _run(args, timeout=30):
    cmd = accounts_cmd()
    if not cmd:
        raise AccountsError("no accounts command (single-slot mode)")
    try:
        return subprocess.run(cmd + args, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AccountsError(f"{os.path.basename(cmd[0])} {args[0]}: {type(e).__name__}: {e}")


def _json(p, what):
    try:
        return json.loads(p.stdout)
    except ValueError:
        err = (p.stderr or p.stdout or "").strip()[:300]
        raise AccountsError(f"{what}: unreadable answer (rc {p.returncode}): {err}")


# ─── slots ─────────────────────────────────────────────────────────────────

def home_dir():
    """The config dir Claude uses when CLAUDE_CONFIG_DIR is unset."""
    return os.environ.get("CLANKER_CLAUDE_DIR") or os.path.expanduser("~/.claude")


def _real(p):
    return os.path.realpath(p) if p else None


def _shape(row, home_real):
    """One `claude-acct list --json` row → a clanker slot dict."""
    real = row.get("resolves_to") or (_real(row.get("config_dir"))
                                      if row.get("config_dir") and os.path.exists(row["config_dir"]) else None)
    status, declared = row.get("status") or "?", row.get("declared") or row.get("status")
    why, warn = "", []
    if declared == "disabled":
        why = "retired (disabled in the slot registry)"
    elif not real or not os.path.isdir(real):
        why = "no config dir" if status == "empty" else f"config dir {row.get('config_dir')} missing"
    elif status == "needs-login":
        why = f"not logged in (owner: claude-acct login {row.get('slot')})"
    if row.get("fence"):
        warn.append(f"fenced: {row['fence']}")
    if status == "live?":
        warn.append("login state not checked")
    if row.get("stale"):
        warn.append("usage figure stale")
    caps = row.get("caps") or {}
    for key, cap, label in (("five_hour", "five_hour_stand_down", "5h"), ("seven_day", "seven_day_stand_down", "7d")):
        v, c = row.get(key), caps.get(cap)
        if isinstance(v, (int, float)) and isinstance(c, (int, float)) and v >= c:
            warn.append(f"{label} {round(v * 100)}% >= stand-down {round(c * 100)}%")
    return {"id": row.get("slot"), "status": status, "plan": row.get("plan"),
            "config_dir": row.get("config_dir"), "real": real,
            "home": bool(real) and real == home_real, "usable": not why, "why": why, "warnings": warn,
            "five_hour": row.get("five_hour"), "seven_day": row.get("seven_day"),
            "resets_at": row.get("resets_at") or {}, "leases": row.get("leases") or 0,
            "figure_at": row.get("figure_at"), "figure_source": row.get("figure_source"),
            "stale": bool(row.get("stale")), "fence": row.get("fence"), "owner_note": row.get("owner_note")}


def _single():
    return [{"id": SINGLE, "status": "live", "plan": None, "config_dir": None,
             "real": _real(os.environ.get("CLAUDE_CONFIG_DIR") or home_dir()),
             "home": True, "single": True, "usable": True, "why": "", "warnings": [],
             "five_hour": None, "seven_day": None, "resets_at": {}, "leases": 0,
             "fence": None, "owner_note": None}]


def slots(online=False, max_age=LIST_TTL):
    """Every slot the accounts command knows, shaped by _shape. online=True asks
    the CLI for each slot's login state (~0.6 s, for pickers); the default reads
    the cached state (~0.1 s, for display). Raises AccountsError on failure."""
    cmd = accounts_cmd()
    if not cmd:
        return _single()
    key = (tuple(cmd), online, home_dir())
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    p = _run(["list", "--json"] + ([] if online else ["--offline"]))
    rows = _json(p, "list")
    if p.returncode != 0 or not isinstance(rows, list):
        raise AccountsError(f"list: rc {p.returncode}: {(p.stderr or '').strip()[:300]}")
    home_real = _real(home_dir())
    out = [_shape(r, home_real) for r in rows if isinstance(r, dict) and r.get("slot")]
    _cache[key] = (time.time(), out)
    return out


def slots_or_home(online=False):
    """slots(), or — when the accounts command fails — the home slot alone, so a
    reader (dashboard, registry) still sees the home fleet. Returns (slots, error)."""
    try:
        return slots(online=online), None
    except AccountsError as e:
        home = dict(_single()[0], id="?home", status="?", why="")
        return [home], str(e)


LOGIN_TTL = 300.0           # s: login state and plan (claude auth status, ~0.6 s) are re-checked this often


def slots_display():
    """(slots, error) for a view that refreshes every few seconds (the dashboard):
    figures from the fast list, login state and plan from the checked list,
    cached LOGIN_TTL — so a never-checked `live?` slot is not shown as usable, and
    the dashboard and `clanker ls` say the same thing."""
    sl, err = slots_or_home()
    if err or (len(sl) == 1 and sl[0].get("single")):
        return sl, err
    try:
        on = checked_logins(LOGIN_TTL)
    except AccountsError:
        return sl, err
    return [dict(s, **on[s["id"]]) if s["id"] in on else s for s in sl], err


_CHECKED = ("status", "plan", "usable", "why", "warnings")


def _login_cache_path():
    return os.path.join(os.environ.get("CLANKER_DATA", "/data/clanker"), "cache", "accounts-login.json")


def checked_logins(max_age=LOGIN_TTL):
    """{slot id: checked login fields}: a file cache that every process shares (the
    dashboard refreshes it every few seconds), so `clanker ls` does not pay the
    ~0.5 s online check on each run. Asks the accounts command when the cache is
    older than max_age or was written for another command or home."""
    key = [list(accounts_cmd() or []), home_dir()]
    path = _login_cache_path()
    try:
        with open(path) as f:
            d = json.load(f)
        if d.get("key") == key and 0 <= time.time() - d["at"] < max_age:
            return d["slots"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    on = {s["id"]: {k: s.get(k) for k in _CHECKED} for s in slots(online=True, max_age=max_age)}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"key": key, "at": time.time(), "slots": on}, f)
        os.replace(tmp, path)
    except OSError:
        pass
    return on


def get(slot_id, sl):
    return next((s for s in sl if s["id"] == slot_id), None)


def slot_of_dir(cfg, sl):
    """The slot whose dir `cfg` resolves to (None/'' = the home dir), else None."""
    single = [s for s in sl if s.get("single")]
    if single:
        return single[0]
    r = _real(cfg) if cfg else _real(home_dir())
    return next((s for s in sl if s["real"] and s["real"] == r), None)


def proc_config_dir(pid):
    """(readable, value): CLAUDE_CONFIG_DIR of a process; value None when unset."""
    try:
        with open(f"/proc/{int(pid)}/environ", "rb") as f:
            env = f.read().split(b"\0")
    except (OSError, ValueError, TypeError):
        return False, None
    for e in env:
        if e.startswith(b"CLAUDE_CONFIG_DIR="):
            return True, e.split(b"=", 1)[1].decode(errors="replace") or None
    return True, None


def proc_env_value(pid, name):
    """One variable from a process's environment, or None."""
    try:
        with open(f"/proc/{int(pid)}/environ", "rb") as f:
            env = f.read().split(b"\0")
    except (OSError, ValueError, TypeError):
        return None
    pre = name.encode() + b"="
    return next((e[len(pre):].decode(errors="replace") for e in env if e.startswith(pre)), None)


def slot_of_pid(pid, sl):
    """The slot a running process uses, None when its environment is unreadable,
    or {"id": "?", ...} when its config dir matches no slot."""
    ok, cfg = proc_config_dir(pid)
    if not ok:
        return None
    s = slot_of_dir(cfg, sl)
    return s or {"id": "?", "real": _real(cfg), "plan": None, "home": False, "usable": False,
                 "why": f"config dir {cfg} is not a known slot", "warnings": []}


def sessions_dirs(sl):
    """[(slot id, sessions dir)] — each slot's live Claude registry, deduplicated.
    CLANKER_CLAUDE_SESSIONS_DIR replaces the home slot's dir (the test pin)."""
    out, seen = [], set()
    for s in sl:
        if s.get("single") or s["home"]:
            d = os.environ.get("CLANKER_CLAUDE_SESSIONS_DIR") or os.path.join(
                s["real"] or home_dir(), "sessions")
        elif s["real"]:
            d = os.path.join(s["real"], "sessions")
        else:
            continue
        r = _real(d)
        if r in seen:
            continue
        seen.add(r)
        out.append((s["id"], d))
    return out


def projects_dirs(sl):
    """[(slot id, projects dir)] — where each slot keeps its transcripts."""
    out, seen = [], set()
    for s in sl:
        base = s["real"] or (home_dir() if s.get("single") or s["home"] else None)
        if not base:
            continue
        d = os.path.join(base, "projects")
        r = _real(d)
        if r in seen:
            continue
        seen.add(r)
        out.append((s["id"], d))
    return out


def launch_env(slot):
    """The env(1) prefix that pins a launch to `slot`: unset for the home slot,
    the slot's stable dir otherwise. Single-slot mode inherits the environment."""
    if slot.get("single"):
        return ""
    if slot["home"]:
        return "env -u CLAUDE_CONFIG_DIR"
    return "env CLAUDE_CONFIG_DIR=" + shlex.quote(slot["config_dir"])


def label(s):
    """`a3 max 5h 4% 7d 79%` — a slot in one line."""
    def pct(v):
        return f"{round(v * 100)}%" if isinstance(v, (int, float)) else "?"
    bits = [s["id"]] + ([s["plan"]] if s.get("plan") else [])
    if not s.get("single"):
        bits += [f"5h {pct(s.get('five_hour'))}", f"7d {pct(s.get('seven_day'))}"]
    return " ".join(bits)


# ─── selection ─────────────────────────────────────────────────────────────

SLOT_RX = re.compile(r"^a[1-9][0-9]?$")


def take_slot(words):
    """(slot word or None, other words, error): the account a command line names.
    Any word shaped like a slot ID (a1, a3, a12) IS an account word, wherever it
    stands — `clanker new a3 foo`, `clanker open foo a3` — so a typo like a9
    is an error, never a session called a9. Two account words are an error."""
    found = [w for w in words if SLOT_RX.match(w)]
    rest = [w for w in words if not SLOT_RX.match(w)]
    if len(found) > 1:
        return None, rest, f"two accounts named: {' and '.join(found)} — name one"
    return (found[0] if found else None), rest, None


def usable_summary(sl):
    good = [label(s) for s in sl if s["usable"]]
    bad = [f"{s['id']} ({s['why']})" for s in sl if not s["usable"] and s["status"] != "empty"]
    return ("usable: " + ("; ".join(good) or "none")) + (("\nnot usable: " + "; ".join(bad)) if bad else "")


def resolve(account):
    """The slot for a NEW session: (slot, None) or (None, error message).
    `account` is what the user selected. None is an error unless only one slot
    exists (single-slot mode): the user always selects."""
    try:
        sl = slots(online=True)
    except AccountsError as e:
        return None, f"account slots unavailable: {e}"
    if len(sl) == 1 and sl[0].get("single"):
        if account not in (None, SINGLE):
            return None, (f"unknown account '{account}': no accounts command, the only slot is "
                          f"'{SINGLE}' (set CLANKER_ACCOUNTS_CMD to use slots)")
        return sl[0], None
    if not account:
        return None, "choose an account: clanker new <slot> [name]   (e.g. clanker new a3 foo)\n" + usable_summary(sl)
    s = get(account, sl)
    if not s:
        return None, f"unknown account '{account}'\n" + usable_summary(sl)
    if not s["usable"]:
        return None, f"account {s['id']} is not usable: {s['why']}\n" + usable_summary(sl)
    return s, None


# ─── leases ────────────────────────────────────────────────────────────────

def holder(session):
    return HOLDER_PREFIX + session


def lease_user(slot, session, pid, need=None, ttl=LEASE_TTL):
    """Take a user lease for `session` on `slot`, bound to `pid`.
    Returns {"lease": dict|None, "refused": [...], "warnings": [...]}.
    Single-slot mode: no lease, nothing refused. Raises AccountsError."""
    if slot.get("single"):
        return {"lease": None, "refused": [], "warnings": [], "off": True}
    args = ["lease", slot["id"], "--user", "--holder", holder(session), "--pid", str(pid),
            "--ttl", str(int(ttl)), "--json"]
    if need is not None:
        args[3:3] = ["--need", str(need)]
    p = _run(args)
    if p.returncode not in (0, 1):
        raise AccountsError(f"lease {slot['id']}: rc {p.returncode}: {(p.stderr or p.stdout).strip()[:300]}")
    d = _json(p, "lease")
    return {"lease": d.get("lease"), "refused": d.get("refused") or [], "warnings": d.get("warnings") or []}


def renew(lease_id, pid=None, ttl=LEASE_TTL):
    """Extend a lease (and move it to `pid`). True when it is live afterwards."""
    if not lease_id or not enabled():
        return False
    args = ["renew", lease_id, "--ttl", str(int(ttl))] + (["--pid", str(pid)] if pid else [])
    try:
        return _run(args).returncode == 0
    except AccountsError:
        return False


def release(lease_id):
    if not lease_id or not enabled():
        return False
    try:
        return _run(["release", lease_id]).returncode == 0
    except AccountsError:
        return False


def leases(max_age=LIST_TTL):
    """The live leases (claude-acct leases --json); [] in single-slot mode."""
    cmd = accounts_cmd()
    if not cmd:
        return []
    key = ("leases", tuple(cmd))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    p = _run(["leases", "--json"])
    ls = _json(p, "leases")
    if not isinstance(ls, list):
        raise AccountsError("leases: not a list")
    _cache[key] = (time.time(), ls)
    return ls


def lease_of_pid(pid, ls):
    return next((l for l in ls if l.get("pid") == pid), None)


def forget_cache():
    _cache.clear()
