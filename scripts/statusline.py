#!/usr/bin/env python3
"""Claude Code status line — a 4-row style (2026-09-27) plus the segments the old bash line carried:
the F5 gauge (Fable weekly bucket, from ~/.cache/clanker/usage.json kept fresh by the detached refresher
~/.claude/scripts/statusline-usage-refresh.py, at most one run per usage.next window), the GPU host probe
(SSH, cached 300 s in /tmp/.claude-gpu-status.v2) and the user@host identity. Reads the status-line JSON on stdin.
Rows: LIMITS [CTX] [5H] [7D] [F5] · TOKENS [IN] [OUT] [CACHE] · CONFIG model · effort · duration · GPU · WHERE user@host · cwd · branch
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime

# ── ANSI colors ──────────────────────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
RED     = "\033[91m"
BLUE    = "\033[94m"
CYAN    = "\033[36m"
PINK    = "\033[95m"
MAGENTA = "\033[35m"
ORANGE  = "\033[38;5;208m"
LABEL   = "\033[38;5;244m"   # muted gray for row labels
DELTA   = "\033[38;5;244m"   # muted gray for (+N) deltas

USAGE_DIR = os.path.expanduser("~/.cache/clanker")
USAGE_TTL = 300          # s: the F5 cache is fresh this long; older starts one detached refresher
USAGE_MAX_AGE = 3600     # s: older than this the F5 segment is left out (the old line's rule)
USAGE_REFRESHER = os.path.expanduser("~/.claude/scripts/statusline-usage-refresh.py")
GPU_CACHE = "/tmp/.claude-gpu-status.v2"
GPU_TTL = 300
# the build this line watches: overridable, no machine literal in the script
BUILD_ROOT = os.environ.get("CLANKER_BUILD_ROOT") or os.path.expanduser("~/projects/colonizers")
BUILD_LOCK = os.environ.get("CLANKER_BUILD_LOCK") or "/data/colonizers/locks/main.lock"
RX_JOBS = os.environ.get("CLANKER_RX_JOBS") or "/data/colonizers/out/rx/jobs.jsonl"


def fmt_tokens(n):
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def fmt_k(n):
    """Short format: commas for <10k, 12.3k / 1.2m above."""
    try:
        n = int(n)
    except Exception:
        return str(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}m"
    if n >= 10_000:
        return f"{n/1_000:.1f}k"
    return f"{n:,}"


def pct_color(pct, yellow_at=50, red_at=80):
    """Gradient per meter: green below yellow_at, yellow below red_at, red from red_at (F5 reds at the 90 % line,
    CTX at 70 % used = the 30 %-remaining line the harness watches)."""
    if pct is None:
        return LABEL
    try:
        p = float(pct)
    except Exception:
        return LABEL
    if p < yellow_at:
        return GREEN
    if p < red_at:
        return YELLOW
    return RED


def to_epoch(v):
    """resets_at as an epoch int: a number, a numeric string, or an ISO timestamp."""
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except Exception:
        pass
    try:
        s = str(v).replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def fmt_reset(epoch):
    """24-hour clock: '16:32' for <12h, 'Fri 16:32' up to a week, '3d' beyond."""
    epoch = to_epoch(epoch)
    if not epoch:
        return ""
    try:
        dt = datetime.fromtimestamp(epoch)
    except Exception:
        return ""
    delta_s = epoch - int(time.time())
    hm = dt.strftime("%H:%M")
    if 0 <= delta_s < 12 * 3600:
        return hm
    if delta_s >= 7 * 86400:
        return f"{delta_s // 86400}d"
    return f"{dt.strftime('%a')} {hm}"


def fmt_duration(ms):
    """Compact session duration: 45s / 12m / 1h23m."""
    try:
        s = int(ms) // 1000
    except Exception:
        return ""
    if s <= 0:
        return ""
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def effort_color(level):
    return {"LOW": BLUE, "MEDIUM": YELLOW, "HIGH": ORANGE, "XHIGH": RED, "MAX": RED}.get((level or "").upper(), LABEL)


def shorten_home(path):
    home = os.path.expanduser("~")
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


def git_branch(cwd):
    d = os.path.abspath(cwd)
    while True:
        head = os.path.join(d, ".git", "HEAD")
        if os.path.isfile(head):
            try:
                with open(head) as f:
                    line = f.read().strip()
                if line.startswith("ref: refs/heads/"):
                    return line[len("ref: refs/heads/"):]
                return line[:7]
            except Exception:
                return None
        if os.path.isfile(os.path.join(d, ".git")):     # a worktree: .git is a file pointing at the gitdir
            try:
                with open(os.path.join(d, ".git")) as f:
                    gitdir = f.read().strip().split("gitdir: ", 1)[-1]
                with open(os.path.join(gitdir, "HEAD")) as f:
                    line = f.read().strip()
                return line[len("ref: refs/heads/"):] if line.startswith("ref: refs/heads/") else line[:7]
            except Exception:
                return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def read_effort_level(cwd):
    """Cascade: project local > project > user settings.json."""
    for path in (os.path.join(cwd, ".claude", "settings.local.json"),
                 os.path.join(cwd, ".claude", "settings.json"),
                 os.path.expanduser("~/.claude/settings.json")):
        try:
            with open(path) as f:
                v = json.load(f).get("effortLevel")
            if v:
                return v
        except Exception:
            continue
    return None


# ── F5: the Fable weekly bucket (the old line's cache + detached refresher) ──────────────────────────────
def fable_gauge_data():
    """{percent, resets_at} when the cache is usable (< 1 h old, window not reset), else None; keeps the cache fresh."""
    now = int(time.time())
    cache = {}
    try:
        with open(os.path.join(USAGE_DIR, "usage.json")) as f:
            cache = json.load(f)
    except Exception:
        cache = {}
    fetched = to_epoch(cache.get("fetched_at")) or 0
    age = now - fetched if fetched else -1
    if age < 0 or age >= USAGE_TTL:
        nxt = 0
        try:
            with open(os.path.join(USAGE_DIR, "usage.next")) as f:
                nxt = int(f.read().strip() or 0)
        except Exception:
            nxt = 0
        if now >= nxt and os.path.isfile(USAGE_REFRESHER):
            try:
                os.makedirs(USAGE_DIR, mode=0o700, exist_ok=True)
                tmp = os.path.join(USAGE_DIR, "usage.next.%d" % os.getpid())
                with open(tmp, "w") as f:
                    f.write("%d\n" % (now + 60))
                os.replace(tmp, os.path.join(USAGE_DIR, "usage.next"))
                subprocess.Popen(["setsid", "nohup", "python3", "-u", USAGE_REFRESHER],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            except Exception:
                pass
    fable = cache.get("fable") if isinstance(cache.get("fable"), dict) else None
    if not fable:
        return None
    resets = to_epoch(fable.get("resets_at"))
    if not resets or resets <= now:
        return {"percent": fable.get("percent"), "resets_at": None, "note": "\u21bb reset"}
    if age < 0 or age >= USAGE_MAX_AGE:
        return {"percent": fable.get("percent"), "resets_at": resets, "note": "\u21bb stale %dh" % max(1, age // 3600)}
    return {"percent": fable.get("percent"), "resets_at": resets}


# ── GPU: the research host's compute jobs over SSH, cached 300 s (the old line's probe) ───────────────
def gpu_status():
    now = int(time.time())
    try:
        with open(GPU_CACHE) as f:
            ts, status = f.read().strip().split(" ", 1)
        if status and 0 <= now - int(ts) < GPU_TTL:
            return status
    except Exception:
        pass
    env = {}
    try:
        with open(os.path.expanduser("~/.claude/research.env")) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    if not env.get("GPU_HOST"):
        return "unset"                          # no GPU host configured in ~/.claude/research.env: the segment is left out
    host = "%s@%s" % (env.get("GPU_USER", "root"), env["GPU_HOST"])
    remote = ("nvidia-smi --query-compute-apps=process_name,used_memory --format=csv,noheader,nounits 2>/dev/null"
              " | grep -iE 'python|torch|xgboost'"
              " | awk -F', *' '{ s+=$2 } END { if (s>0) printf \"busy %.1fGiB\", s/1024; else print \"idle\" }'")
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=2", "-o", "BatchMode=yes", host, remote],
                           capture_output=True, text=True, timeout=4)
        status = (r.stdout.strip() if r.returncode == 0 else "") or ("idle" if r.returncode == 0 else "offline")
    except Exception:
        status = "offline"
    try:
        tmp = "%s.%d" % (GPU_CACHE, os.getpid())
        with open(tmp, "w") as f:
            f.write("%d %s\n" % (now, status))
        os.replace(tmp, GPU_CACHE)
    except Exception:
        pass
    return status


HIST = os.path.join(USAGE_DIR, "statusline-hist.json")
BLOCKS = "▁▂▃▄▅▆▇█"
ICON = {"CTX": "▣", "5H": "⧗", "7D": "☷", "F5": "✧", "gpu": "⚙", "branch": "⎇", "land": "⚒",
        "sup": "♥", "walk": "♟", "load": "⚖", "rx": "⇄", "cycle": "↻", "cost": "$"}


def bar(pct, col, n=5):
    """Five blocks: ▰ filled, ▱ empty, in the meter's colour."""
    try:
        k = max(0, min(n, int(round(float(pct) / 100.0 * n))))
    except Exception:
        return DIM + "□" * n + RESET
    return col + "■" * k + DIM + "□" * (n - k) + RESET      # squares at text height: ■ filled, □ empty 


def countdown(epoch):
    d = (to_epoch(epoch) or 0) - int(time.time())
    if d <= 0:
        return ""
    h, m = divmod(d // 60, 60)
    return "↻%dh%02dm" % (h, m) if h else "↻%dm" % m


def history(key, pct):
    """Append (ts, pct) at most every 5 min; keep 8 points; return the sparkline (0–100 scaled)."""
    hist = {}
    try:
        hist = json.load(open(HIST))
    except Exception:
        hist = {}
    pts = [x for x in hist.get(key, []) if isinstance(x, list) and len(x) == 2][-8:]
    now = int(time.time())
    if pct is not None and (not pts or now - int(pts[-1][0]) >= 300):
        pts.append([now, float(pct)]); pts = pts[-8:]; hist[key] = pts
        try:
            tmp = HIST + ".%d" % os.getpid()
            with open(tmp, "w") as f:
                json.dump(hist, f)
            os.replace(tmp, HIST)
        except Exception:
            pass
    if len(pts) < 2:
        return ""
    return "".join(BLOCKS[max(0, min(7, int(v / 100.0 * 7.999)))] for _, v in pts)


def build_state():
    """The colonizers build from files the supervisor and the executor write (no process spawned):
    → the segments, colour only."""
    root = BUILD_ROOT
    now = int(time.time())
    segs = []
    try:
        hb = json.load(open(os.path.join(root, "ci/out/run/heartbeat.json")))
        age = now - to_epoch(hb.get("utc"))
        if age < 30:
            segs.append(DIM + ICON["sup"] + " sup ok" + RESET)
        else:
            segs.append(RED + ICON["sup"] + " sup %ds" % age + RESET)
    except Exception:
        segs.append(RED + ICON["sup"] + " sup ?" + RESET)
    try:
        lk = open(BUILD_LOCK).read().strip()
        if lk:
            m = re.search(r"row=(\S+)", lk) or re.search(r'"row"\s*:\s*"([^"]+)"', lk)
            segs.append(MAGENTA + ICON["land"] + " LAND " + (m.group(1) if m else "held") + RESET)
    except Exception:
        pass
    try:
        slots = [f for f in os.listdir(os.path.join(root, "ci/walk-slots")) if not f.startswith(".")]
        segs.append((MAGENTA if slots else DIM) + ICON["walk"] + " walks %d/2" % len(slots) + RESET)
    except Exception:
        pass
    try:
        l1 = os.getloadavg()[0]
        col = DIM if l1 < 16 else YELLOW if l1 < 30 else RED
        segs.append(col + ICON["load"] + " load %.0f" % l1 + RESET)
    except Exception:
        pass
    try:
        last = None
        with open(RX_JOBS, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 4000)); lines = f.read().decode(errors="ignore").strip().split("\n")
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("ev") in ("end", "fallback", "start"):
                last = e; break
        if last:
            host = last.get("host") or ("local" if last.get("ev") == "fallback" else "?")
            kevin = host == "kevin-node" and last.get("ev") != "fallback"
            segs.append((DIM if kevin else YELLOW) + ICON["rx"] + " rx " + ("kevin" if kevin else host) + RESET)
    except Exception:
        pass
    try:
        st = json.load(open(os.path.join(root, "ci/out/orch/state.json")))
        n = st.get("n")
        cdir = os.path.join(root, "ci/out/orch/cycles", str(n))
        live = n is not None and os.path.isdir(cdir) and not os.path.exists(os.path.join(cdir, "exit.json"))
        req = ""
        try:
            with open(os.path.join(root, "ci/out/orch/requests.jsonl")) as f:
                req = json.loads(f.read().strip().split("\n")[-1]).get("cmd", "")
        except Exception:
            pass
        state = "live" if live else ("paused" if req == "pause" else "idle")
        segs.append((CYAN if live else DIM) + ICON["cycle"] + " cycle #%s %s" % (n, state) + RESET)
    except Exception:
        pass
    return segs


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    SEP = DIM + " · " + RESET

    ctx = data.get("context_window") or {}
    used_pct  = ctx.get("used_percentage")
    total_in  = ctx.get("total_input_tokens") or 0
    total_out = ctx.get("total_output_tokens") or 0
    cu        = ctx.get("current_usage") or {}
    cu_in, cu_out = cu.get("input_tokens") or 0, cu.get("output_tokens") or 0
    cu_cc, cu_cr  = cu.get("cache_creation_input_tokens") or 0, cu.get("cache_read_input_tokens") or 0
    rate = data.get("rate_limits") or {}
    five_pct, seven_pct = (rate.get("five_hour") or {}).get("used_percentage"), (rate.get("seven_day") or {}).get("used_percentage")
    five_reset, seven_reset = (rate.get("five_hour") or {}).get("resets_at"), (rate.get("seven_day") or {}).get("resets_at")
    cost = data.get("cost") or {}
    duration_ms, cost_usd = cost.get("total_duration_ms"), cost.get("total_cost_usd")
    cwd_raw = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or os.getcwd()
    model = (data.get("model") or {}).get("display_name") or (data.get("model") or {}).get("id") or "?"
    effort = (read_effort_level(cwd_raw) or "?").upper()
    cwd, branch = shorten_home(cwd_raw), git_branch(cwd_raw)
    fable = fable_gauge_data()
    segs = build_state()

    # the meters, with their thresholds; the hottest (closest to its red line) shows a countdown instead of the clock
    meters = [("CTX", used_pct, None, 60, 70, None), ("5H", five_pct, five_reset, 50, 80, None), ("7D", seven_pct, seven_reset, 50, 80, None)]
    if fable:
        meters.append(("F5", fable["percent"], None if fable.get("note") else fable["resets_at"], 80, 90, fable.get("note")))
    def heat(m):
        try:
            return float(m[1]) - m[4]
        except Exception:
            return -999
    hot = max(meters, key=heat)[0] if meters else None

    # the terminal width is unknowable from a pipe: assume 80 columns unless the environment or a tty says more
    cols = int(os.environ.get("COLUMNS") or 0)
    for fd in (2, 1, 0):
        if cols:
            break
        try:
            cols = os.get_terminal_size(fd).columns
        except Exception:
            cols = 0
    wide = cols >= 120

    def fmt_pct_tight(pct):
        return "?" if pct is None else f"{int(round(float(pct)))}%"

    def gauge(label, pct, reset_epoch, yellow_at, red_at, note):
        col = pct_color(pct, yellow_at, red_at)
        calm = col == GREEN
        body = ICON[label] + " " + label + " " + bar(pct, col) + " " + BOLD + fmt_pct_tight(pct) + RESET
        # the reset time only on the hottest meter unless the terminal is wide: four meters must fit 80 columns
        when = countdown(reset_epoch) if (label == hot and reset_epoch) else (fmt_reset(reset_epoch) if wide else "")
        if when:
            body += " " + when
        if note:
            body += " " + note
        if calm:
            return DIM + "[" + body.replace(BOLD, "").replace(RESET, RESET + DIM) + "]" + RESET
        return DIM + "[" + RESET + col + body.replace(RESET, RESET + col) + RESET + DIM + "]" + RESET

    row1 = " ".join(gauge(*m) for m in meters)
    spark = history("five_hour", five_pct)
    if spark:
        row1 += " " + DIM + spark + RESET

    def delta(n):
        return "" if not n else " " + DELTA + f"+{fmt_tokens(n)}" + RESET
    def bracket(inner):
        return DIM + "[" + RESET + inner + DIM + "]" + RESET
    row2 = (bracket(LABEL + "IN " + RESET + fmt_k(total_in) + delta(cu_in)) + " "
            + bracket(LABEL + "OUT " + RESET + RED + fmt_k(total_out) + RESET + delta(cu_out)) + " "
            + bracket(LABEL + "CACHE " + RESET + ((YELLOW + "+" + fmt_k(cu_cc) + RESET + DIM + " / " + RESET) if cu_cc else "") + GREEN + fmt_k(cu_cr) + RESET))

    gpu = gpu_status()
    gpu_seg = "" if gpu == "unset" else (DIM if gpu == "idle" else RED if gpu == "offline" else MAGENTA) + ICON["gpu"] + " GPU " + gpu + RESET
    row3 = CYAN + model + RESET + SEP + BOLD + effort_color(effort) + effort + RESET
    dur = fmt_duration(duration_ms)
    if dur:
        row3 += SEP + LABEL + dur + RESET
    if cost_usd:
        try:
            row3 += SEP + LABEL + "$%.2f" % float(cost_usd) + RESET
        except Exception:
            pass
    row3 += SEP + gpu_seg
    ident = "%s@%s" % (os.environ.get("USER") or os.environ.get("LOGNAME") or "?", socket.gethostname().split(".")[0])
    row4 = SEP.join([GREEN + ident + RESET, PINK + cwd + RESET] + ([YELLOW + ICON["branch"] + " " + branch + RESET] if branch else []))

    # Exactly THREE rows, filled by flowing whole segments in priority order : a row never wraps
    # (the terminal would repeat the middle row) and a segment is never cut; what does not fit in three rows is dropped,
    # lowest priority first. 
    width = int(os.environ.get("COLUMNS") or 0)
    for fd in (2, 1, 0):                      # the status line runs on a pipe; stderr is often still the tty
        if width:
            break
        try:
            width = os.get_terminal_size(fd).columns
        except Exception:
            width = 0
    width = (width or 80) - 1                # unknown width → 80 columns: safe on every terminal
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    import unicodedata
    def vis(t):                              # terminal cells, not characters: wide glyphs take two (a miscount wraps the row)
        return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in ansi.sub("", t))
    width -= 1                               # one spare cell against ambiguous-width glyphs

    order = [gauge(*m) for m in meters]
    if spark:
        order.append(DIM + spark + RESET)
    order += [bracket(LABEL + "IN " + RESET + fmt_k(total_in) + delta(cu_in)),
              bracket(LABEL + "OUT " + RESET + RED + fmt_k(total_out) + RESET + delta(cu_out)),
              bracket(LABEL + "CACHE " + RESET + ((YELLOW + "+" + fmt_k(cu_cc) + RESET + DIM + " / " + RESET) if cu_cc else "") + GREEN + fmt_k(cu_cr) + RESET)]
    order += [CYAN + model + RESET, BOLD + effort_color(effort) + effort + RESET]
    if dur:
        order.append(LABEL + dur + RESET)
    if cost_usd:
        try:
            order.append(LABEL + "$%.2f" % float(cost_usd) + RESET)
        except Exception:
            pass
    order += ([gpu_seg] if gpu_seg else []) + [GREEN + ident + RESET, PINK + cwd + RESET]
    if branch:
        order.append(YELLOW + ICON["branch"] + " " + branch + RESET)
    order += segs

    rows, cur, cur_w = [], [], 0
    for seg in order:
        w = vis(seg)
        if cur and cur_w + 3 + w > width:
            rows.append(cur); cur, cur_w = [], 0
            if len(rows) == 3:
                break
        if not cur and w > width:
            continue                           # a single segment wider than the terminal is dropped, never cut
        cur.append(seg); cur_w += (3 if cur_w else 0) + w
    if cur and len(rows) < 3:
        rows.append(cur)
    while len(rows) < 3:
        rows.append([])
    for r in rows[:3]:
        print(SEP.join(r) if r else " ")

if __name__ == "__main__":
    main()
