"""`clanker orch <action>` — control the optional orchestration subsystem.

OFF by default. `on`/`off` flip the master switch; `set <key> <val>` tunes a toggle;
`spawn`/`list`/`stop` manage sessions; `supervise` runs one pass manually; `enqueue`/
`dispatch` drive the backlog. The same controls are exposed in the WebUI.
"""
import json

from . import control, store, spawn as spawn_mod, daemon, router


def _status(args):
    cfg = control.get_config()
    print("=== Orchestration ===")
    print(f"  enabled: {cfg['enabled']}    auto_nudge: {cfg['auto_nudge']}    "
          f"auto_spawn: {cfg['auto_spawn']}    auto_merge: {cfg['auto_merge']}")
    print(f"  max_parallel: {cfg['max_parallel']}    nudge_idle_secs: {cfg['nudge_idle_secs']}    "
          f"nudge_risk_max: {cfg['nudge_risk_max']}    nudge_max: {cfg['nudge_max']}    "
          f"done_idle_secs: {cfg['done_idle_secs']}    budget_usd: {cfg['budget_usd']}")
    sess = store.list_sessions()
    active = [s for s in sess if s["state"] in store.ACTIVE_STATES]
    print(f"\n  sessions: {len(active)} active / {len(sess)} total")
    for s in active[:25]:
        print(f"    [{s['state']:8}] {s['id'][:10]}  {(s.get('project') or '-'):16} "
              f"{(s.get('task') or '')[:48]}")
    bl = store.list_backlog()
    if bl:
        print(f"\n  backlog: {len(bl)} queued")


def _on(args):
    control.set_config(enabled=True)
    print("orchestration ENABLED — read-only supervision on; auto_nudge/auto_spawn/auto_merge "
          "stay OFF until you turn them on.")


def _off(args):
    control.set_config(enabled=False)
    print("orchestration DISABLED.")


def _set(args):
    cl = args.cmdline or []
    key = cl[0] if cl else None
    if key not in control.DEFAULTS:
        print(f"unknown key '{key}'. valid: {', '.join(control.DEFAULTS)}")
        return 1
    if len(cl) < 2:
        print(f"{key} = {control.get_config().get(key)}")
        return 0
    val = cl[1]
    if key in ("max_parallel", "nudge_idle_secs", "done_idle_secs", "nudge_max"):
        val = int(val)
    elif key == "budget_usd":
        val = None if str(val).lower() in ("none", "off", "") else float(val)
    cfg = control.set_config(**{key: val})
    print(f"{key} = {cfg[key]}")


def _spawn(args):
    if not control.enabled():
        print("orchestration is OFF — enable first: clanker orch on")
        return 1
    task = " ".join(args.cmdline or [])
    if not task:
        print("usage: clanker orch spawn \"<task>\" --project X [--headless]")
        return 1
    rec = spawn_mod.spawn(task, project=args.project, headless=args.headless)
    if rec.get("error"):
        print(f"could not spawn: {rec['error']} ({rec.get('active')}/{rec.get('max_parallel')})")
        return 1
    print(f"spawned {rec['id'][:10]} → tmux {rec.get('tmux_target')}  "
          f"(worktree: {rec.get('worktree') or 'none'})")


def _list(args):
    for s in store.list_sessions():
        print(f"  [{s['state']:8}] {s['id'][:12]}  {(s.get('project') or '-'):16} "
              f"{(s.get('task') or '')[:48]}")


def _stop(args):
    sid = (args.cmdline or [None])[0]
    if not sid:
        print("usage: clanker orch stop <id>")
        return 1
    matches = [s for s in store.list_sessions() if s["id"].startswith(sid)]
    if not matches:
        print("no such session")
        return 1
    print("stopped" if spawn_mod.stop(matches[0]["id"]) else "stop failed")


def _supervise(args):
    r = daemon.supervise_once()
    if args.json:
        print(json.dumps(r, default=str, indent=2))
    else:
        print(f"enabled={r.get('enabled')} reconciled={r.get('reconciled')} "
              f"reaped={r.get('reaped')} finished={r.get('finished')} nudged={r.get('nudged')} "
              f"overlaps={r.get('overlaps')} merged={r.get('merged')} pruned={r.get('pruned')} "
              f"budget={r.get('budget')}")


def _enqueue(args):
    task = " ".join(args.cmdline or [])
    if not task:
        print("usage: clanker orch enqueue \"<task>\" --project X")
        return 1
    print(f"queued {router.enqueue_task(task, project=args.project)}")


def _dispatch(args):
    if not control.enabled():
        print("orchestration is OFF — enable first: clanker orch on")
        return 1
    res = router.dispatch_backlog(spawn_fn=lambda t, p: spawn_mod.spawn(t, project=p).get("id"))
    done = [r for r in res if r["action"] != "queue"]
    print(f"dispatched {len(done)} item(s)")
    for r in res:
        print(f"  {r['action']:6} {r['backlog_id']} → {r.get('target') or '-'}")


_ACTIONS = {"status": _status, "on": _on, "off": _off, "set": _set, "spawn": _spawn,
            "list": _list, "stop": _stop, "supervise": _supervise,
            "enqueue": _enqueue, "dispatch": _dispatch}


def run(args):
    fn = _ACTIONS.get(getattr(args, "action", None) or "status")
    if not fn:
        print(f"unknown action. choose: {', '.join(sorted(_ACTIONS))}")
        return 1
    return fn(args) or 0
