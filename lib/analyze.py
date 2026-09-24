"""Analysis pipeline — cost-weighted rankings, anomaly detection."""

import json
import os
import glob
from collections import defaultdict
from datetime import datetime, timedelta

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")
SESSIONS_DIR = os.path.join(DATA_DIR, "raw/sessions")

# S1: memoize parsed session records. Keyed on (newest sessions-dir mtime, file
# count, last_days, dedup, cutoff-date) and invalidated automatically when a
# session file is appended-to or added (the mtime/count fingerprint changes) or
# the day rolls over — so the dashboard refresher stops re-reading + re-parsing
# 90 days of JSONL on every warm cycle.
_sessions_memo = {}


def _sessions_signature():
    """Cheap stat-only fingerprint of SESSIONS_DIR: (newest mtime, file count).
    Changes whenever a session file is appended-to or a new one appears."""
    try:
        files = glob.glob(os.path.join(SESSIONS_DIR, "*.jsonl"))
        newest = max((os.path.getmtime(f) for f in files), default=0.0)
        return (newest, len(files))
    except OSError:
        return (0.0, 0)


def _prune_sessions_memo():
    """Keep the memo tiny — only a couple of (last_days, dedup) combos are ever
    used, but each new fingerprint adds a key; drop all but the newest few."""
    if len(_sessions_memo) > 8:
        for k in list(_sessions_memo)[:-4]:
            del _sessions_memo[k]

# Cap session duration at 8 hours — longer durations are idle tmux sessions,
# not continuous work. This prevents inflated cost metrics.
MAX_DURATION_S = 28800  # 8 hours

# Normalize project names from bootstrap data to registry names
PROJECT_ALIASES = {
    "Quanta-AI-V1": "quanta-ai", "quantaaiv1": "quanta-ai", "quanta_ai_v1": "quanta-ai",
    "Eigenstate-V2": "eigenstate", "eigenstatev2": "eigenstate",
    "Research": "eigenstateresearch", "research": "eigenstateresearch",
    "FlowStudio": "flowstudio",
    "Zergrush": "zergrush",
    "Mac-Mini": "macmini",
    "Titrin": "titrin",
}

# Worktree/branch directories (e.g. ~/projects/<base>-<wave-tag>, ~/projects/<base>-worktrees)
# are the SAME project for analytics. Collapse "<base>-anything" → "<base>" for these bases.
# Without this, every worktree spawns a phantom project (e.g. polymarkethftinfrastructure-g2-handshake).
WORKTREE_BASES = (
    "polymarkethftinfrastructure",
    "hftlogger-rust",
    "hftbacktester",
    "hftlogger",
)

# Fixture/test rows must never reach analytics. The suite is data-isolated via
# tests/conftest.py (CLANKER_DATA → tmpdir, 2026-07-19), but historical pytest
# runs wrote these into the live store, and any future writer that skips the
# fixture would too — belt-and-suspenders at read time.
TEST_PROJECTS = {"test-project", "test-stdin"}


def normalize_project(name):
    """Normalize a project name to its canonical registry name.

    Applies, in order: (1) explicit bootstrap aliases, (2) worktree-family
    collapse so `<base>-<worktree-suffix>` and `<base>-worktrees` map to `<base>`.
    """
    if not name:
        return "global"
    name = PROJECT_ALIASES.get(name, name)
    for base in WORKTREE_BASES:
        if name == f"{base}-worktrees" or name.startswith(f"{base}-"):
            return base
    return name


def session_kind(s):
    """"interactive", "nested" or "unknown" for one session row.

    nested is a scripted `claude -p` run (CLAUDE_CODE_ENTRYPOINT=sdk-cli): the
    row's `nested` flag (session hooks since 2026-09-24), else its entrypoint
    (sdk-* is nested), else `kind` (`clanker wrap` rows say nested). Rows
    written before those fields existed are "unknown", not interactive: 90%
    of the rows measured on 2026-09-24 were nested runs."""
    k = s.get("kind")
    if k in ("interactive", "nested"):
        return k
    n = s.get("nested")
    if isinstance(n, bool):
        return "nested" if n else "interactive"
    ep = s.get("entrypoint")
    if isinstance(ep, str) and ep:
        return "nested" if ep.startswith("sdk-") else "interactive"
    return "unknown"


def load_sessions(last_days=7, dedup=True, kind=None):
    """Load session metrics from JSONL files.

    Every record carries `kind` (session_kind: interactive / nested /
    unknown). kind=None returns all of them: nested runs are real spend
    (autonomous builds), so they are split out, never dropped. kind="nested"
    (or a tuple of kinds) returns only those.

    KEYSTONE FIX: the SessionEnd/Stop hooks re-fire on every `--resume` and
    re-parse the ENTIRE growing transcript, appending a fresh cumulative record
    each time. One real session was logged up to 354×, so the raw log inflates
    661 real sessions into ~9,070 phantom rows (and every downstream metric with
    it). We dedup by session_id keeping the LAST record — which is the true final
    state of that session (the latest re-parse covers the whole transcript).

    Set dedup=False only to inspect the raw event stream.
    """
    rows = _load_sessions(last_days, dedup)
    if kind is None:
        return rows
    want = (kind,) if isinstance(kind, str) else tuple(kind)
    return [s for s in rows if s["kind"] in want]


def _load_sessions(last_days, dedup):
    from datetime import timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=last_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    sig = _sessions_signature()
    key = (sig[0], sig[1], last_days, dedup, cutoff_str)
    cached = _sessions_memo.get(key)
    if cached is not None:
        return list(cached)   # shallow copy so a caller's in-place sort can't corrupt the memo

    records = []
    for f in sorted(glob.glob(os.path.join(SESSIONS_DIR, "*.jsonl"))):
        basename = os.path.basename(f).replace(".jsonl", "")
        if basename < cutoff_str:
            continue
        try:
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        s = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(s, dict):
                        continue
                    s["project"] = normalize_project(s.get("project", "global"))
                    if s["project"] in TEST_PROJECTS:
                        continue
                    s["kind"] = session_kind(s)
                    records.append(s)
        except OSError:
            pass

    if not dedup:
        _sessions_memo[key] = records
        _prune_sessions_memo()
        return list(records)

    # Dedup by session_id, last-write-wins. Files are date-sorted and lines are
    # appended chronologically, so the final occurrence of an id is its latest
    # (most complete) state. Records without a session_id (some ingested/bootstrap
    # events) can't be deduped — keep them all.
    by_id = {}
    anon = []
    for s in records:
        sid = s.get("session_id")
        if sid:
            by_id[sid] = s
        else:
            anon.append(s)
    result = list(by_id.values()) + anon
    _sessions_memo[key] = result
    _prune_sessions_memo()
    return list(result)


def run_analysis(mode, by="project", last_days=7, json_output=False, rewrites=False):
    """Run analysis and print results. rewrites=True (or mode "rewrites")
    prints the cache-rewrite report instead and returns its exit code."""
    if rewrites or mode == "rewrites":
        return rewrite_report(last_days=last_days, json_output=json_output)
    if mode == "daily":
        sessions = load_sessions(last_days=1)
    elif mode == "weekly":
        sessions = load_sessions(last_days=7)
    elif mode == "errors":
        sessions = load_sessions(last_days=30)
        sessions.sort(key=lambda s: -s.get("errors", 0))
        _print_error_report(sessions[:20], json_output)
        return
    elif mode == "slow":
        sessions = load_sessions(last_days=30)
        sessions.sort(key=lambda s: -s.get("duration_s", 0))
        _print_slow_report(sessions[:20], json_output)
        return
    else:
        sessions = load_sessions(last_days=last_days)

    if sessions:
        if by == "project":
            _print_project_report(sessions, mode, json_output)
        elif by == "tool":
            _print_tool_report(sessions, json_output)
    else:
        print("No sessions found.")
    if mode == "weekly" and not json_output:
        _print_memory_debt()


def _print_memory_debt():
    """Weekly-digest section (audit P5d): the gc measures orphan debt weekly —
    this puts the top offenders where the operator actually looks. Zero
    orphans → no section (a surface nobody needs is not a surface)."""
    try:
        from memorycmd import orphans_top
        total, top = orphans_top(10)
    except Exception:
        return
    if not total:
        return
    print()
    print("=== Memory debt (global namespace) ===")
    print(f"Orphans: {total} file(s) unreachable from the router indexes "
          f"(INDEX_ALL.md §ORPHANS). Top {len(top)} by size:")
    for name, kb in top:
        print(f"  {kb:8.1f}KB  {name}")
    print("Triage: add a pointer line in MEMORY.md / *-POINTERS.md, or delete the file.")


def _print_project_report(sessions, mode, json_output):
    by_project = defaultdict(lambda: {"count": 0, "errors": 0, "time": 0, "tools": 0})
    for s in sessions:
        p = s.get("project", "global")
        by_project[p]["count"] += 1
        by_project[p]["errors"] += s.get("errors", 0)
        by_project[p]["time"] += min(s.get("duration_s", 0), MAX_DURATION_S)
        by_project[p]["tools"] += sum(s.get("tool_uses", {}).values())

    total_errors = sum(d["errors"] for d in by_project.values())
    total_time = sum(d["time"] for d in by_project.values())

    print(f"=== {mode.upper()} Report ===")
    print(f"Sessions: {len(sessions)} | Time: {total_time/3600:.1f}h | Errors: {total_errors}")
    kinds = defaultdict(int)
    for s in sessions:
        kinds[s.get("kind") or session_kind(s)] += 1
    print(f"Interactive: {kinds['interactive']} | Nested: {kinds['nested']} | "
          f"Unknown (rows before 2026-09-24): {kinds['unknown']}")
    print()

    # Cost-weighted ranking
    ranked = []
    for p, d in by_project.items():
        avg_time = d["time"] / d["count"] if d["count"] > 0 else 0
        cost = d["errors"] * (avg_time / 3600)
        err_rate = d["errors"] / d["count"] if d["count"] > 0 else 0
        avg_h = avg_time / 3600
        ranked.append((p, d, cost, err_rate, avg_h))
    ranked.sort(key=lambda x: -x[2])

    if json_output:
        for p, d, cost, err_rate, avg_h in ranked:
            print(json.dumps({"project": p, **d, "cost": round(cost, 1),
                              "err_rate": round(err_rate, 1), "avg_hours": round(avg_h, 1)}))
    else:
        print(f"{'Project':<20} {'Sess':>5} {'Errs':>6} {'Err/S':>6} {'AvgH':>6} {'Cost':>8}")
        print("-" * 58)
        for p, d, cost, err_rate, avg_h in ranked:
            print(f"{p:<20} {d['count']:>5} {d['errors']:>6} {err_rate:>6.1f} {avg_h:>6.1f} {cost:>8.1f}")

        # Recommendations
        print()
        print("=== Recommendations ===")
        has_recs = False
        for p, d, cost, err_rate, avg_h in ranked:
            if cost > 5:
                print(f"HIGH: {p} — {cost:.0f} error-hours. Investigate error patterns.")
                has_recs = True
            if avg_h > 3:
                print(f"MEDIUM: {p} — sessions average {avg_h:.1f}h. Consider task decomposition.")
                has_recs = True
        if not has_recs:
            print("No actionable recommendations — all projects within normal parameters.")


def _print_error_report(sessions, json_output):
    print(f"{'Date':<12} {'Project':<20} {'Errors':>8} {'Duration':>10}")
    print("-" * 55)
    for s in sessions:
        dur = f"{s.get('duration_s', 0) // 60}m"
        print(f"{s.get('timestamp', '')[:10]:<12} {s.get('project', '?'):<20} {s.get('errors', 0):>8} {dur:>10}")


def _print_slow_report(sessions, json_output):
    print(f"{'Date':<12} {'Project':<20} {'Duration':>10} {'Errors':>8}")
    print("-" * 55)
    for s in sessions:
        dur = f"{s.get('duration_s', 0) / 3600:.1f}h"
        print(f"{s.get('timestamp', '')[:10]:<12} {s.get('project', '?'):<20} {dur:>10} {s.get('errors', 0):>8}")


def _print_tool_report(sessions, json_output):
    tools = defaultdict(lambda: {"uses": 0, "errors": 0})
    for s in sessions:
        for tool, count in s.get("tool_uses", {}).items():
            tools[tool]["uses"] += count
        for tool, count in s.get("error_tools", {}).items():
            tools[tool]["errors"] += count

    print(f"{'Tool':<15} {'Uses':>8} {'Errors':>8} {'Error%':>8}")
    print("-" * 42)
    for tool in sorted(tools, key=lambda t: -tools[t]["uses"]):
        d = tools[tool]
        pct = (d["errors"] / d["uses"] * 100) if d["uses"] > 0 else 0
        print(f"{tool:<15} {d['uses']:>8} {d['errors']:>8} {pct:>7.1f}%")


# ---- cache rewrites (design audit 2026-09-24, proposal 3) --------------------------

# Rewrites with no compaction before them, by the gap to the call before:
# under 5 minutes no cache TTL has expired; under 1 hour the 1-hour TTL, which
# main sessions write with, has not expired either.
FAST_GAP_S = 300
HOUR_GAP_S = 3600


def _transcripts_dir():
    """Where Claude Code keeps main transcripts: <dir>/<project slug>/<session>.jsonl."""
    return (os.environ.get("CLANKER_TRANSCRIPTS_DIR")
            or os.path.join(os.path.expanduser("~"), ".claude", "projects"))


def _ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def transcript_rewrites(path):
    """Rewrite figures of one main transcript, counted as the session-end hook
    counts them: calls deduplicated by message id, then pricing.rewrites()
    (the block the hook carries byte for byte). With the context that tells a
    rewrite with a known cause from an unexplained one:
      compactions           compact_boundary lines in the transcript
      after_compact_tokens  rewrite tokens of calls that follow a compaction
      fast_tokens           rewrite tokens of the other calls that came under
                            FAST_GAP_S after the call before them
      hour_tokens           the same under HOUR_GAP_S (fast_tokens included)
    None when the file cannot be read."""
    from collections import Counter
    import pricing
    calls, order, first_ts, after_compact = {}, [], {}, {}
    cwd = entry = None
    compactions, pending = 0, False
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"compact_boundary"' in raw:
                    try:
                        o = json.loads(raw)
                    except ValueError:
                        o = None
                    if isinstance(o, dict) and o.get("subtype") == "compact_boundary":
                        compactions += 1
                        pending = True
                    continue
                if b'"usage"' not in raw or b'"assistant"' not in raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "assistant":
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                usage, model = msg.get("usage"), msg.get("model")
                if not isinstance(usage, dict) or model == "<synthetic>":
                    continue
                cwd = cwd or obj.get("cwd")
                entry = entry or obj.get("entrypoint")
                key = pricing.message_key(obj, msg)
                if key not in calls:
                    order.append(key)
                    first_ts[key] = _ts(obj.get("timestamp"))
                    after_compact[key] = pending
                    pending = False
                pricing.note_usage(calls, key, model, usage)
    except OSError:
        return None
    rw = pricing.rewrites(calls, order)
    pos = {k: i for i, k in enumerate(order)}
    fast = hour = compacted = 0
    for k in rw:
        cc = calls[k][4]
        if after_compact.get(k):
            compacted += cc
            continue
        a, b = first_ts.get(order[pos[k] - 1]), first_ts.get(k)
        if a is not None and b is not None:
            fast += cc if b - a < FAST_GAP_S else 0
            hour += cc if b - a < HOUR_GAP_S else 0
    tokens, _cost, n = pricing.totals(calls)
    models = Counter(calls[k][0] for k in order)
    return {"rewrite_tokens": sum(calls[k][4] for k in rw), "rewrites": len(rw),
            "fast_tokens": fast, "hour_tokens": hour, "after_compact_tokens": compacted,
            "compactions": compactions, "api_calls": n,
            "cache_create": tokens["cache_create"],
            "model": models.most_common(1)[0][0] if models else None,
            "cwd": cwd, "entrypoint": entry}


def rewrite_report(last_days=7, top=10, json_output=False, transcripts_dir=None):
    """Sessions of the last `last_days` days ranked by rewrite_tokens: the
    cache writes of calls whose cached prefix shrank against the call before
    them on the same model (proposal 3; a candidate for `claude --debug api`).

    Sources: every main transcript under the transcript dir written in the
    window (the archive, read fresh, with cause columns), and session rows
    that carry rewrite_tokens (hook rows since 2026-09-24) for sessions whose
    transcript is gone. Returns the exit code (0)."""
    import time as _time
    t0 = _time.monotonic()
    root = transcripts_dir or _transcripts_dir()
    rows = {s.get("session_id"): s for s in load_sessions(last_days=last_days)
            if s.get("session_id")}
    cutoff = _time.time() - last_days * 86400
    paths = {}
    for p in glob.glob(os.path.join(glob.escape(root), "*", "*.jsonl")):
        try:
            if os.path.getmtime(p) >= cutoff:
                paths[os.path.basename(p)[:-len(".jsonl")]] = p
        except OSError:
            continue
    found, from_rows, missing = [], 0, 0
    for sid in sorted(set(rows) | set(paths)):
        row = rows.get(sid) or {}
        d = transcript_rewrites(paths[sid]) if sid in paths else None
        if d is not None:
            src = "transcript"
        elif isinstance(row.get("rewrite_tokens"), int):
            d = {"rewrite_tokens": row["rewrite_tokens"], "rewrites": None,
                 "fast_tokens": None, "hour_tokens": None, "after_compact_tokens": None,
                 "compactions": None, "api_calls": row.get("api_calls"),
                 "cache_create": (row.get("tokens") or {}).get("cache_create"),
                 "model": row.get("model"), "cwd": row.get("cwd"),
                 "entrypoint": row.get("entrypoint")}
            src = "row"
            from_rows += 1
        else:
            missing += 1
            continue
        kind = session_kind(row) if row else "unknown"
        if kind == "unknown" and d.get("entrypoint"):       # the transcript's own field
            kind = "nested" if str(d["entrypoint"]).startswith("sdk-") else "interactive"
        found.append({"session_id": sid, "cwd": d.get("cwd") or row.get("cwd"),
                      "project": row.get("project"), "kind": kind, "source": src, **{
                          k: v for k, v in d.items() if k not in ("cwd",)}})
    found.sort(key=lambda e: (-(e["rewrite_tokens"] or 0), e["session_id"]))
    elapsed = _time.monotonic() - t0
    total_rw = sum(e["rewrite_tokens"] or 0 for e in found)
    total_cc = sum(e.get("cache_create") or 0 for e in found)
    if json_output:
        for rank, e in enumerate(found[:top], 1):
            print(json.dumps({"rank": rank, **e}))
        return 0
    print(f"=== Cache rewrites, last {last_days} days ===")
    print(f"{len(found)} sessions ({len(found) - from_rows} from transcripts, {from_rows} "
          f"from rows only; {missing} rows without data) in {elapsed:.1f} s")
    share = f" ({total_rw / total_cc:.0%} of main cache writes)" if total_cc else ""
    print(f"rewrite tokens: {total_rw / 1e6:,.1f}M{share}")
    print("A rewrite is a call that read less from the cache than the call before it on")
    print("the same model. <5min and <1h: rewrite tokens that came that soon after the")
    print("call before, with no compaction between; compact: rewrites after a compaction.")
    if not found:
        print("No sessions with rewrite data.")
        return 0
    print()
    print(f"{'#':>2} {'rewrite':>8} {'n':>4} {'<5min':>7} {'<1h':>7} {'compact':>7} "
          f"{'share':>5} {'calls':>5} {'kind':<11} {'session':<36}  cwd")

    def m(v):
        return "-" if v is None else f"{v / 1e6:.1f}M"
    for rank, e in enumerate(found[:top], 1):
        cc = e.get("cache_create") or 0
        sh = f"{e['rewrite_tokens'] / cc:.0%}" if cc else "-"
        print(f"{rank:>2} {m(e['rewrite_tokens']):>8} "
              f"{'-' if e['rewrites'] is None else e['rewrites']:>4} "
              f"{m(e['fast_tokens']):>7} {m(e['hour_tokens']):>7} "
              f"{m(e['after_compact_tokens']):>7} {sh:>5} "
              f"{e.get('api_calls') or '-':>5} {e['kind']:<11} {e['session_id']:<36}  "
              f"{e.get('cwd') or '?'}")
    return 0
