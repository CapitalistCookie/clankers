"""`clanker ecc <action>` — report commands for the optional ECC subsystem.

These commands always run (invoking one is itself opting in). They read clanker's
own session metrics + the Claude Code transcripts and surface the ported analyses:
parked sessions, file overlaps, success-rate trends, per-tool risk, cost vs budget,
and a one-shot command guard. The automatic dashboard/alert hooks are separately
gated by `ecc.enabled()`.
"""
import glob
import json
import os

from . import risk, parked, trend, budget, overlap, guard


def _clanker_sessions(last_days=30):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from analyze import load_sessions
    return load_sessions(last_days=last_days)


def _to_run_records(sessions):
    """Map clanker session records → ECC run-records for trend analysis.
    success = reached a durable outcome; failure = abandoned; else partial.
    user_feedback 'corrected' when the human corrected the session."""
    out = []
    for s in sessions:
        oc = s.get("outcome")
        if oc in ("commit", "push", "deploy"):
            outcome = "success"
        elif oc == "abandoned":
            outcome = "failure"
        elif oc == "empty":
            continue
        else:
            outcome = "partial"
        out.append({
            "key": s.get("project", "global"),
            "outcome": outcome,
            "user_feedback": "corrected" if s.get("user_corrections", 0) > 0 else "accepted",
            "failure_reason": None,
            "tokens": sum((s.get("tokens") or {}).values()),
            "duration_ms": int(s.get("duration_s", 0)) * 1000,
            "recorded_at": s.get("timestamp"),
        })
    return out


def _parked(args):
    recs = parked.scan_dir(pending_secs=getattr(args, "pending", 1800))
    att = [r for r in recs if r.get("state") == "attention"]
    print(f"=== Parked sessions ({len(att)} need attention / {len(recs)} scanned) ===")
    for r in att:
        pt = r.get("pending_tool")
        det = (f"{pt['name']} pending {pt['age_secs'] // 60}m" if pt
               else "; ".join(r.get("reasons", [])))
        print(f"  ⚠ {r['session'][:18]:18}  {det}  → {r.get('recommended_action', '')}")
    if not att:
        print("  ✓ none parked")
    return att


def _overlap(args):
    ov = overlap.file_overlaps(_clanker_sessions(getattr(args, "last", 30)))
    print(f"=== File overlaps across active sessions ({len(ov)}) ===")
    for o in ov[:20]:
        who = ", ".join(s[:8] for s in o["sessions"])
        print(f"  {o['count']}× {o['file']}  ({who})")
    if not ov:
        print("  ✓ no overlaps")
    return ov


def _trend(args):
    recs = _to_run_records(_clanker_sessions(getattr(args, "last", 30)))
    rows = trend.trend_by_key(recs)
    print(f"=== Success-rate trend ({len(rows)} projects) ===")
    print(f"  {'Project':<24}{'7d':>6}{'30d':>7}{'trend':>11}{'corr':>7}")
    for r in rows:
        r7 = (r.get("rate_7d") or 0) * 100
        r30 = (r.get("rate_30d") or 0) * 100
        cor = (r.get("corrections_rate") or 0) * 100
        flag = "  ⚠ declining" if r.get("declining") else ""
        print(f"  {r['key']:<24}{r7:>5.0f}%{r30:>6.0f}%{r.get('trend', ''):>11}{cor:>6.0f}%{flag}")
    cl = trend.cluster_failures(recs)
    if cl:
        print("\n  Recurring failure signatures:")
        for c in cl[:8]:
            print(f"    {c['count']}× {c['signature'][:70]}")
    return rows


def _risk(args):
    paths = sorted(glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True),
                   key=lambda p: os.path.getmtime(p), reverse=True)[:getattr(args, "scan", 20)]
    flagged = []
    for p in paths:
        try:
            with open(p) as f:
                objs = [json.loads(line) for line in f if line.strip()]
        except Exception:
            continue
        events = [{"tool_name": tu.get("name"), "tool_input": tu.get("input")}
                  for tu in parked.extract_tool_uses(objs)]
        r = risk.score_session(events)
        if r["n_block"] or r["n_confirm"]:
            flagged.append((os.path.basename(p)[:16], r))
    flagged.sort(key=lambda x: -x[1]["max_score"])
    print(f"=== Risk scan ({len(paths)} recent transcripts, {len(flagged)} flagged) ===")
    for sid, r in flagged:
        print(f"  [{r['max_action']:>7}] {sid}  max={r['max_score']:.2f}  "
              f"block={r['n_block']} confirm={r['n_confirm']}")
        rk = r.get("riskiest")
        if rk:
            print(f"            {rk['tool_name']}: {str(rk['tool_input'])[:80]}")
    if not flagged:
        print("  ✓ nothing above review")
    return flagged


def _budget(args):
    sessions = _clanker_sessions(getattr(args, "last", 30))
    total = sum(budget.session_cost(s.get("tokens") or {}, s.get("model")) for s in sessions)
    limit = getattr(args, "limit", None)
    ev = budget.evaluate_budget(total, limit)
    print(f"=== Budget ({getattr(args, 'last', 30)}d) ===")
    if limit:
        print(f"  spend: ${total:,.2f} / ${limit:,.2f}  [{ev['state']}]  {ev['ratio'] * 100:.0f}%  "
              f"remaining ${ev['remaining']:,.2f}")
    else:
        print(f"  spend: ${total:,.2f}  (pass --limit to evaluate against a budget)")
    return ev


def _guard(args):
    cmd = " ".join(getattr(args, "cmdline", []) or [])
    c = guard.classify_command(cmd)
    nv = guard.blocks_no_verify(cmd)
    gov = guard.detect_governance(cmd)
    print(f"=== Guard: {cmd[:90]} ===")
    cats = f"  categories: {', '.join(c['categories'])}" if c["categories"] else ""
    print(f"  destructive: {c['destructive']}{cats}")
    for r in c["reasons"]:
        print(f"    - {r}")
    if nv:
        print("  ⚠ bypasses git hooks (--no-verify / core.hooksPath override)")
    for g in gov:
        print(f"  ⚠ {g['type']}: {g['match']}")
    if not (c["destructive"] or nv or gov):
        print("  ✓ clean")
    return {"classify": c, "blocks_no_verify": nv, "governance": gov}


def _scan(args):
    """Combined 'what needs attention' report."""
    print("############ ECC attention scan ############\n")
    _parked(args); print()
    _overlap(args); print()
    _risk(args); print()
    _trend(args)


_ACTIONS = {"parked": _parked, "overlap": _overlap, "trend": _trend,
            "risk": _risk, "budget": _budget, "guard": _guard, "scan": _scan}


def run(args):
    fn = _ACTIONS.get(args.action)
    if not fn:
        print(f"Unknown ecc action: {args.action}. Choose: {', '.join(sorted(_ACTIONS))}")
        return 1
    fn(args)
    return 0
