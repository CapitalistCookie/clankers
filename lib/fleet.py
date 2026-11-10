"""The fleet at a glance (`clanker ls`) and forgiving session names (`clanker open`).

Rows come from tmux (sessions, panes, attached clients) and from Claude Code's own
registry of every account slot (lib/claude_registry.py): state, slot and the time
since the last status change. A record is tied to its pane by PANE ID (the
dashboard's web-* bridge renames the session in records). Usage of every account
comes from claude-acct, whose newest figure includes the ones live sessions'
statuslines publish (scripts/statusline.py harvest_limits).
"""
import difflib
import json
import os
import re
import subprocess
import sys
import time

import accounts
import claude_registry

STATE = {"busy": "working", "shell": "working", "idle": "waiting", "waiting": "decision"}
RANK = {"decision": 0, "waiting": 1, "starting": 2, "working": 3, "unknown": 4, "shell": 5}
STUCK_S = 12 * 3600       # "working" this long without a status change: probably stuck
SHELLS = ("bash", "zsh", "sh", "dash", "fish")


# ─── tmux ──────────────────────────────────────────────────────────────────

def _tmux(args):
    try:
        p = subprocess.run(["tmux"] + args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout if p.returncode == 0 else ""


def panes():
    """Every pane of every non-bridge session."""
    fmt = "\t".join(["#{session_name}", "#{pane_id}", "#{pane_pid}", "#{pane_current_command}",
                     "#{session_attached}", "#{pane_current_path}"])
    out = []
    for ln in _tmux(["list-panes", "-a", "-F", fmt]).splitlines():
        parts = ln.split("\t")
        if len(parts) < 6 or parts[0].startswith("web-"):
            continue
        try:
            ppid = int(parts[2])
        except ValueError:
            ppid = None
        cmd = "claude" if parts[3] in ("claude", "claude.exe") else parts[3]
        out.append({"session": parts[0], "pane_id": parts[1], "pane_pid": ppid, "command": cmd,
                    "attached": parts[4] not in ("", "0"), "path": parts[5]})
    return out


def session_names():
    return sorted({p["session"] for p in panes()})


# ─── rows ──────────────────────────────────────────────────────────────────

def _short(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path and (path == home or path.startswith(home + "/")) else (path or "")


def rows(sl=None, now=None):
    """One row per tmux session: {name, panes, claude, slots, state, since_s, stuck,
    attached, dir, project, session_ids}. state is the most urgent of its Claude
    panes, or "shell" when no Claude runs there; project is the registered project
    its directory belongs to (None outside every project)."""
    import session_flow
    now = now or time.time()
    if sl is None:
        sl, _err = accounts.slots_or_home()
    recs = claude_registry.read_all(sl)
    try:
        projs = session_flow.projects()
    except Exception:
        projs = {}
    out, where, seen = {}, {}, {}
    for p in panes():
        r = out.setdefault(p["session"], {"name": p["session"], "panes": 0, "claude": 0, "slots": [],
                                          "states": [], "since_s": None, "attached": False,
                                          "dir": _short(p["path"]), "session_ids": []})
        where.setdefault(p["session"], p["path"])
        r["panes"] += 1
        r["attached"] = r["attached"] or p["attached"]
        rec = claude_registry.record_for_pane(p, recs)
        if not rec and p["command"] == "claude":
            # Claude runs but wrote no registry record (2026-10-29: `main`, a Claude Code
            # from a since-replaced binary, had only its sessions/<pid>.key): its state is
            # unknown, but it is no plain shell
            r["claude"] += 1
            r["states"].append("unknown")
            kids = claude_registry.proc_children(p["pane_pid"]) if p["pane_pid"] else []
            s = accounts.slot_of_pid(kids[0], sl) if kids else None
            sid = s["id"] if s else "?"
            if sid not in r["slots"]:
                r["slots"].append(sid)
            continue
        if not rec:
            continue
        r["claude"] += 1
        s = accounts.slot_of_pid(rec["pid"], sl)
        sid = s["id"] if s else "?"
        if sid not in r["slots"]:
            r["slots"].append(sid)
        r["states"].append(STATE.get(rec.get("status"), "starting" if not rec.get("status") else "working"))
        changed = (rec.get("statusUpdatedAt") or rec.get("updatedAt") or 0) / 1000
        if changed:
            age = max(0, now - changed)
            r["since_s"] = age if r["since_s"] is None else min(r["since_s"], age)
        r["dir"] = _short(rec.get("cwd") or p["path"])
        where[p["session"]] = rec.get("cwd") or p["path"]
        if rec.get("sessionId"):
            r["session_ids"].append(rec["sessionId"])
    res = []
    for r in out.values():
        r["state"] = min(r["states"], key=lambda x: RANK.get(x, 9)) if r["states"] else "shell"
        r["stuck"] = r["state"] == "working" and (r["since_s"] or 0) > STUCK_S
        w = where.get(r["name"])
        if w not in seen:                         # many sessions share a dir (~): one lookup each
            seen[w] = session_flow.project_of(w, projs)[0] if projs else None
        r["project"] = seen[w]
        del r["states"]
        res.append(r)
    res.sort(key=lambda r: (RANK.get(r["state"], 9), r["name"].lower()))
    return res


def usage(sl, fleet_rows=()):
    """Every account worth showing (not empty): figures, resets, freshness, and
    how many Claude sessions run on it."""
    count = {}
    for r in fleet_rows:
        for s in r["slots"]:
            count[s] = count.get(s, 0) + 1
    out = []
    for s in sl:
        if s["status"] == "empty" or s.get("single"):
            continue
        out.append(dict(s, sessions=count.get(s["id"], 0)))
    return out


# ─── rendering ─────────────────────────────────────────────────────────────

# Colour roles → palette accents (gruvbox-material: the box's palette.json, global
# rule 21 — never a retyped hex). Without the file: the terminal's own 16 colours.
ROLE = {"decision": "orange", "waiting": "yellow", "working": "green", "starting": "blue",
        "stuck": "red", "unknown": "grey", "shell": "grey", "dim": "grey",
        "ok": "green", "warn": "yellow", "hot": "red"}
ANSI16 = {"red": "31", "orange": "33", "yellow": "33", "green": "32", "blue": "34", "grey": "90"}


def palette_path():
    return os.environ.get("CLANKER_PALETTE") or os.path.expanduser("~/.config/gruvbox-material/palette.json")


def painter(stream=None):
    """paint(text, role) for a terminal, else None: no colour when the stream is not
    a terminal, NO_COLOR is set (no-color.org) or TERM=dumb."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return None
    try:
        if not stream.isatty():
            return None
    except (AttributeError, ValueError):
        return None
    codes = {}
    try:
        with open(palette_path()) as f:
            pal = json.load(f)
        for name in set(ROLE.values()):
            hexv = (pal.get("accents", {}).get(name) or pal.get("greys", {}).get(name) or {}).get("hex", "")
            if re.fullmatch(r"#[0-9a-fA-F]{6}", hexv):
                r, g, b = (int(hexv[i:i + 2], 16) for i in (1, 3, 5))
                codes[name] = f"38;2;{r};{g};{b}"
    except (OSError, ValueError, AttributeError):
        pass

    def paint(text, role):
        name = ROLE.get(role)
        code = codes.get(name) or ANSI16.get(name)
        return f"\033[{code}m{text}\033[0m" if code and text.strip() else text
    return paint


def _plain(text, _role):
    return text


def _load(v):
    if not isinstance(v, (int, float)):
        return "dim"
    return "hot" if v >= 0.95 else "warn" if v >= 0.80 else "ok"

def ago(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    if sec < 60:
        return "now"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def until(ts, now):
    """Countdown to an ISO or epoch reset time ('1h12m', '3d4h'), '' when unknown or past."""
    if not ts:
        return ""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        try:
            from datetime import datetime
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return ""
    d = int(t - now)
    if d <= 0:
        return ""
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h{(d % 3600) // 60:02d}m"
    return f"{d // 86400}d{(d % 86400) // 3600}h"


def pct(v):
    return f"{round(v * 100)}%" if isinstance(v, (int, float)) else "?"


def _source(s, now):
    src = (s.get("figure_source") or "").replace("mirror:", "")
    src = {"clanker-statusline": "live", "probe": "probe"}.get(src, src)
    at = s.get("figure_at")
    age = ""
    if at:
        try:
            from datetime import datetime
            a = ago(now - datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp())
            age = "just now" if a == "now" else a + " ago"
        except ValueError:
            age = ""
    return " ".join(x for x in (src, age) if x) + (" (stale)" if s.get("stale") else "")


def render_usage(us, now, paint=None):
    paint = paint or _plain
    if not us:
        return ""
    lines = [f"{'ACCOUNT':8} {'PLAN':5} {'5H':>4}  {'RESETS':8} {'7D':>4}  {'RESETS':8} {'SESS':>4}  FIGURE"]
    off = []
    for s in us:
        if not s["usable"]:
            off.append(f"{s['id']} ({s['why'].split(' (')[0]})")
            continue
        r = s.get("resets_at") or {}
        five = paint(f"{pct(s.get('five_hour')):>4}", _load(s.get("five_hour")))
        seven = paint(f"{pct(s.get('seven_day')):>4}", _load(s.get("seven_day")))
        lines.append(f"{s['id'] + ('*' if s['home'] else ''):8} {(s.get('plan') or '-'):5} "
                     f"{five}  {until(r.get('five_hour'), now):8} {seven}  {until(r.get('seven_day'), now):8} "
                     f"{s['sessions']:>4}  {paint(_source(s, now), 'dim')}")
    if off:
        lines.append(paint("not usable: " + ", ".join(off), "dim"))
    if any(s["home"] and s["usable"] for s in us):
        lines.append("* = the home login (~/.claude)    FIGURE: live = from a running session's statusline")
    return "\n".join(lines)


def render_rows(rs, now, show_shells=False, paint=None):
    paint = paint or _plain
    shown = [r for r in rs if r["claude"] or show_shells]
    lines = [f"{'SESSION':30} {'ACCT':5} {'STATE':9} {'SINCE':6} PROJECT"]
    for r in shown:
        name = r["name"] + (f" ({r['claude']})" if r["claude"] > 1 else "") + (" *" if r["attached"] else "")
        state = r["state"] + ("?" if r["stuck"] else "")
        where = r.get("project") or f"— {r['dir']}"
        role = "stuck" if r["stuck"] else r["state"]
        lines.append(f"{name[:30]:30} {','.join(r['slots']) or '-':5} {paint(f'{state:9}', role)} "
                     f"{ago(r['since_s']) if r['claude'] else '':6} "
                     f"{where[:70] if r.get('project') else paint(where[:70], 'dim')}")
    hidden = len(rs) - len(shown)
    foot = []
    if hidden:
        foot.append(f"{hidden} plain shell session(s) — clanker ls -a")
    loose = sum(1 for r in shown if r["claude"] and not r.get("project"))
    if loose:
        foot.append(f"— = outside every project ({loose} Claude session(s)); new sessions always start in one")
    if any(r["state"] == "unknown" for r in shown):
        foot.append("unknown = Claude runs but wrote no registry record (an old Claude Code?)")
    if any(r["stuck"] for r in shown):
        foot.append(f"working? = no status change for over {STUCK_S // 3600} h (stuck or forgotten)")
    if any(r["attached"] for r in shown):
        foot.append("* = a terminal is attached")
    return "\n".join(lines + ([""] + foot if foot else []))


def as_json(rs, us):
    keep = ("id", "status", "plan", "usable", "why", "warnings", "five_hour", "seven_day", "resets_at",
            "figure_at", "figure_source", "stale", "sessions", "home")
    return json.dumps({"accounts": [{k: s.get(k) for k in keep} for s in us], "sessions": rs}, indent=1)


# ─── names ─────────────────────────────────────────────────────────────────

def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def resolve(words, names):
    """(name, how, suggestions): the session `words` names. Tries, in order: exact
    (words joined by '-', by '', or the slug), case-insensitive, letters-and-digits
    only (`construction management` → constructionmanagement), unique prefix. When
    none fits, `suggestions` holds the closest names (typos: maintanence →
    maintainence)."""
    names = list(names)
    raw = " ".join(words).strip()
    if not raw:
        return None, "", []
    tries = [raw, "-".join(words), "".join(words), re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")]
    for t in tries:
        if t in names:
            return t, "exact", []
    low = {n.lower(): n for n in names}
    for t in tries:
        if t.lower() in low:
            return low[t.lower()], "case", []
    nq = _norm(raw)
    hits = [n for n in names if _norm(n) == nq]
    if len(hits) == 1:
        return hits[0], "spacing", []
    pre = [n for n in names if n.lower().startswith(raw.lower()) or _norm(n).startswith(nq)]
    if len(pre) == 1 and nq:
        return pre[0], "prefix", []
    bynorm = {}
    for n in names:
        bynorm.setdefault(_norm(n), n)
    close = difflib.get_close_matches(nq, list(bynorm), n=4, cutoff=0.72)
    sugg = [bynorm[c] for c in close]
    for n in pre:
        if n not in sugg:
            sugg.append(n)
    return None, "", sugg[:6]
