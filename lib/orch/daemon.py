"""The supervise loop — MVP-3 of the orchestration subsystem.

Ported from ECC's `session::daemon` (affaan-m/ECC): `run` / `resume_crashed_sessions`
/ `pid_is_alive`. One pass over the orchestrated sessions that, in order:

  1. no-ops unless the master switch (`control.enabled`) is on,
  2. reconciles each active session's state from its tmux pane + bumps its heartbeat —
     including DONE-DETECTION: a pane showing the session's exit sentinel (typed by
     `orch.spawn` after the agent command) moves it to `done` (rc 0) / `failed`
     (rc != 0), and an interactive session whose pane has been stable and free of
     human-blocking prompts for `done_idle_secs` (opt-in, default 0 = off) goes `done`,
  3. reaps sessions whose pid is dead (-> `failed`),
  4. flags (does NOT kill) the fleet going over the global cost budget,
  5. flags files being touched by >=2 in-flight sessions (collision risk),
  6. (only if `auto_nudge`) auto-continues routine waiting sessions.

Like ECC's daemon — and like `orch.spawn` — every side-effecting call is funnelled
through an injectable callback so the tests need no real tmux, no real processes,
and no real `nudge` module. The production defaults shell out via subprocess using
the same `tmux capture-pane` / `tmux send-keys` recipes the dashboard uses
(see `lib/serve.py`). Each pass is wrapped in try/except: a failing pane capture or
a flaky nudge can never take the loop down (matching ECC's `if let Err(e) = ...`
per-pass error isolation in `run`).

`nudge` is the MVP-2 module. It may not be importable yet; when it is, the real
`nudge.should_nudge` / `nudge.nudge_text` are used. Callers (and tests) may inject
their own via the `should_nudge` / `nudge_text` parameters.

Public API:
    pid_alive(pid)                                 -> bool
    reap_dead(store_path=None, now=None)           -> list[dict]   (reaped records)
    supervise_once(config=None, store_path=None, now=None,
                   capture_pane=None, send_keys=None, get_pane_state=None,
                   should_nudge=None, nudge_text=None, classify_wait=None) -> dict
"""
import errno
import hashlib
import os
import re
import shutil
import signal
import subprocess
from datetime import datetime, timezone

from . import control, store

# `nudge` is MVP-2 and may not exist yet. Import defensively so this module
# always loads; callers/tests can also inject their own nudge callables.
try:  # pragma: no cover - exercised by presence/absence of the sibling module
    from . import nudge as _nudge
except Exception:  # ImportError (module absent) or any import-time failure
    _nudge = None


def _now_iso(now=None):
    """ISO-8601 'Z' timestamp, matching store._now(). `now` may be a datetime."""
    if isinstance(now, datetime):
        dt = now
    else:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── process liveness (ports pid_is_alive) ────────────────────────────────────
def pid_alive(pid):
    """True iff `pid` names a live process.

    Mirrors ECC's `pid_is_alive`: probe with `kill(pid, 0)` (sends no signal).
    Success -> alive. EPERM (process exists but we may not signal it) -> alive.
    ESRCH (no such process) / None / <= 0 / a non-integer -> dead.
    """
    if pid is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:        # ESRCH
        return False
    except PermissionError:           # EPERM -> it exists, we just can't signal it
        return True
    except OSError as e:              # be explicit about EPERM on odd platforms
        return e.errno == errno.EPERM
    return True


# ── reaping dead sessions (ports resume_crashed_sessions) ────────────────────
def reap_dead(store_path=None, now=None):
    """Mark every active session whose pid is dead as `failed`.

    For each active session that carries a pid which `pid_alive` reports dead:
    force the state to `failed` (force=True — a session can crash from any active
    state, not just `running`, so we bypass the transition guard like ECC's
    unconditional `update_state_and_pid(.., Failed, None)`), drop the stale pid,
    and log a `reaped` event. Sessions with no pid (never launched / not yet
    attached) are left alone.

    Returns the list of reaped session records (as they were *before* the update),
    so callers can report what died.
    """
    reaped = []
    for sess in store.list_sessions(active_only=True, path=store_path):
        pid = sess.get("pid")
        if pid is None:
            continue
        if pid_alive(pid):
            continue
        sid = sess.get("id")
        store.set_state(sid, "failed", path=store_path, force=True)
        store.update_session(sid, path=store_path, pid=None)
        store.log_event(
            sid, "reaped", f"pid {pid} dead; marked failed", path=store_path
        )
        reaped.append(sess)
    return reaped


# ── default (real) tmux callbacks — kept hermetic-overridable ────────────────
def _default_capture_pane(tmux_target):
    """Capture a pane's visible content via `tmux capture-pane -p`. '' on failure."""
    if not tmux_target or not shutil.which("tmux"):
        return ""
    try:
        p = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", tmux_target],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout or ""


def _default_send_keys(tmux_target, text):
    """Send literal `text` + Enter to a pane (`tmux send-keys -l` then Enter)."""
    if not tmux_target or not shutil.which("tmux"):
        return
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_target, "-l", "--", text],
            capture_output=True, text=True, timeout=10,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_target, "Enter"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return


# Prompt markers that indicate the agent has stopped and is awaiting input.
_PROMPT_MARKERS = ("❯", "Do you want", "(y/n)", "│ >", "esc to interrupt")
# Activity words that indicate the agent is mid-task (subset of serve.PROGRESS_WORDS).
_BUSY_WORDS = (
    "Thinking", "Cooking", "Envisioning", "Planning", "Generating", "Reading",
    "Editing", "Writing", "Searching", "Running", "Analyzing", "Investigating",
    "Exploring", "Reasoning", "Reflecting", "Reviewing", "tokens)", "esc to interrupt",
)


def _default_get_pane_state(tmux_target, pane_text):
    """Heuristic mapping of pane content to a session state.

    Returns one of "running" | "waiting" | "idle":
      - a busy/progress word in the tail -> "running"
      - a prompt marker ('❯', a y/n confirmation, …) -> "waiting"
      - neither -> "idle"
    `tmux_target` is accepted for signature parity / future use; the decision is
    made from `pane_text`. Deliberately simple — the WebUI's richer detector
    (lib/serve.py) is the production-grade analog.
    """
    text = pane_text or ""
    # Busy words win: an agent printing a spinner line is running even if an old
    # prompt is still visible higher up the scrollback.
    if any(w in text for w in _BUSY_WORDS):
        return "running"
    if any(m in text for m in _PROMPT_MARKERS):
        return "waiting"
    return "idle"


def _resolve_nudge_callables(should_nudge, nudge_text):
    """Pick the should_nudge / nudge_text callables: explicit args win, then the
    real `nudge` module if importable, else inert fallbacks (never nudge)."""
    sn = should_nudge
    if sn is None and _nudge is not None:
        sn = getattr(_nudge, "should_nudge", None)
    nt = nudge_text
    if nt is None and _nudge is not None:
        nt = getattr(_nudge, "nudge_text", None)
    return sn, nt


# ── done-detection ────────────────────────────────────────────────────────────
def _sentinel_exit_rc(pane_text, sentinel_prefix):
    """The exit code printed by the session's exit sentinel, or None if absent.

    `orch.spawn` types `claude …; printf '\\n<prefix>%s>>\\n' "$?"` into the pane,
    so `<prefix><digits>>>` appears only once the agent process has terminated —
    the echoed command line shows the literal `%s` and can never match. Takes the
    LAST match so a stale sentinel above a manual relaunch doesn't shadow a newer one.
    """
    if not pane_text or not sentinel_prefix:
        return None
    found = re.findall(re.escape(sentinel_prefix) + r"(\d+)>>", pane_text)
    if not found:
        return None
    try:
        return int(found[-1])
    except ValueError:  # pragma: no cover - \d+ guarantees int()
        return None


def _parse_ts(value):
    """ISO-8601 (optionally 'Z'-suffixed) → aware UTC datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _as_seconds(value):
    """Coerce a config value to non-negative float seconds (anything bad → 0.0)."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


# How many trailing non-blank pane lines constitute the session's CURRENT
# context. Classifying the full capture makes scrolled-past history sticky: an
# old "(y/n)" question would read as a pending routine wait forever.
_PANE_TAIL_LINES = 15


def _pane_tail(text, lines=_PANE_TAIL_LINES):
    """The last `lines` non-blank lines of a pane capture (the current context)."""
    if not text:
        return ""
    keep = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(keep[-lines:])


def _idle_done_allowed(pane_text, classify):
    """True iff the pane tail shows NOTHING pending — the only wait that may
    idle out to done.

    Consults `nudge.classify_wait` (or an injected classifier) on the pane tail.
    The kinds partition the lanes: `routine` is the auto-nudger's territory (a
    nudgeable continue/permission prompt — idling it to done would terminate a
    mid-task session, as a live trial demonstrated), `decision` /
    `destructive_confirm` / `error` park for the operator, and only `unknown`
    (an at-rest pane with no question in view) may idle to done. With no
    classifier available we fail safe and never idle-done.
    """
    if classify is None:
        return False
    try:
        kind = (classify(_pane_tail(pane_text)) or {}).get("kind")
    except Exception:
        return False
    return kind == "unknown"


# ── the supervise pass (ports run's per-tick body) ───────────────────────────
def supervise_once(config=None, store_path=None, now=None,
                   capture_pane=None, send_keys=None, get_pane_state=None,
                   should_nudge=None, nudge_text=None, classify_wait=None):
    """Run ONE supervision pass over the orchestrated sessions.

    Args:
        config: effective config dict (as from `control.get_config()`). When None,
                it is loaded. Relevant keys: enabled, auto_nudge, budget_usd,
                done_idle_secs, plus whatever `nudge.should_nudge` consumes
                (nudge_idle_secs, …).
        store_path: override the store DB path (tests pass a temp db).
        now: optional datetime for deterministic event stamping + idle-done aging.
        capture_pane(tmux_target) -> str: pane content. Default: real tmux.
        send_keys(tmux_target, text) -> None: send input + Enter. Default: real tmux.
        get_pane_state(tmux_target, pane_text) -> "running"|"waiting"|"idle":
                map a pane to a state. Default: `_default_get_pane_state`.
        should_nudge(session, pane_text, config) -> (bool, reason): nudge gate.
                Default: `nudge.should_nudge` if the module is importable.
        nudge_text() -> str: the keystrokes to send. Default: `nudge.nudge_text`.
        classify_wait(pane_text) -> {"kind": ...}: wait classifier consulted before
                an interactive idle-done. Default: `nudge.classify_wait` if importable.

    Every step is isolated in try/except so one failure (a dead pane, a flaky
    nudge) cannot abort the others or the caller's loop.

    Done-detection (within reconcile):
      - exit sentinel (any session spawned by `orch.spawn`): the pane shows
        `<<CLANKER_EXIT:<sid8>:<rc>>>` once the agent process terminates →
        `done` (rc 0) / `failed` (rc != 0); the exit code + final pane tail land
        in the session meta. Process exit is ground truth, so the transition is
        forced (mirroring `reap_dead`).
      - interactive idle (opt-in, `done_idle_secs` > 0): a NON-headless session
        whose pane has been byte-stable for `done_idle_secs` while waiting/idle,
        with no human-blocking prompt per `classify_wait` → `done`. Headless
        sessions are excluded (a quiet `--print` run looks idle while working).

    Returns a summary dict:
        {"enabled": bool, "actions": [str], "reconciled": int, "reaped": int,
         "finished": int, "nudged": int, "overlaps": int, "merged": int,
         "pruned": int, "budget": <budget state str>}
    When disabled, returns the minimal no-op shape: {"enabled": False, "actions": []}.
    """
    cfg = config if config is not None else control.get_config()

    # 1. Master switch — read-only no-op unless explicitly enabled.
    if not cfg.get("enabled"):
        return {"enabled": False, "actions": []}

    cap = capture_pane or _default_capture_pane
    snd = send_keys or _default_send_keys
    pane_state = get_pane_state or _default_get_pane_state
    sn, nt = _resolve_nudge_callables(should_nudge, nudge_text)
    cw = classify_wait
    if cw is None and _nudge is not None:
        cw = getattr(_nudge, "classify_wait", None)

    actions = []
    reconciled = 0
    finished = 0
    nudged = 0
    overlaps_n = 0
    budget_state = "unconfigured"
    pane_cache = {}  # session_id -> captured pane text (reuse for the nudge pass)

    done_idle = _as_seconds(cfg.get("done_idle_secs"))
    now_dt = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    # Snapshot the active fleet once; reconcile mutates state but we iterate the
    # records we already loaded (ECC reloads per pass — one snapshot per tick here).
    try:
        active = store.list_sessions(active_only=True, path=store_path)
    except Exception:
        active = []

    # 2. Reconcile: capture each pane, map to a state, persist + heartbeat.
    for sess in active:
        sid = sess.get("id")
        target = sess.get("tmux_target")
        try:
            text = cap(target) if target else ""
            pane_cache[sid] = text
            meta = sess.get("meta") if isinstance(sess.get("meta"), dict) else {}

            # 2a. Exit sentinel — the agent process terminated; its exit code is
            # ground truth, so force the transition (mirrors reap_dead).
            rc_exit = _sentinel_exit_rc(text, meta.get("exit_sentinel"))
            if rc_exit is not None:
                final = "done" if rc_exit == 0 else "failed"
                if store.set_state(sid, final, path=store_path, force=True):
                    new_meta = dict(meta)
                    new_meta["exit_rc"] = rc_exit
                    new_meta["final_output"] = text[-4000:]
                    store.update_session(sid, path=store_path, meta=new_meta)
                    store.log_event(
                        sid, "exited", f"agent exit rc={rc_exit} -> {final}",
                        path=store_path,
                    )
                    finished += 1
                    reconciled += 1
                    actions.append(f"exited:{sid}:{final}")
                continue

            new_state = pane_state(target, text)
            if new_state in ("running", "waiting", "idle"):
                changed = store.set_state(sid, new_state, path=store_path)
                store.touch_heartbeat(sid, path=store_path)
                reconciled += 1
                if changed and new_state != sess.get("state"):
                    actions.append(f"reconcile:{sid}->{new_state}")

            # 2b. Pane-stability ledger (drives idle-done AND the nudge idle gate;
            # heartbeat_at is useless for idleness — this loop bumps it every
            # pass). A content hash + changed-at pair in meta tracks the last
            # observed pane activity.
            if ((done_idle > 0 or cfg.get("auto_nudge"))
                    and new_state in ("waiting", "idle")):
                sha = hashlib.sha256(
                    (text or "").encode("utf-8", "replace")).hexdigest()[:16]
                if meta.get("pane_sha") != sha:
                    new_meta = dict(meta)
                    new_meta["pane_sha"] = sha
                    new_meta["pane_changed_at"] = _now_iso(now_dt)
                    store.update_session(sid, path=store_path, meta=new_meta)
                elif done_idle > 0 and not sess.get("headless"):
                    # Interactive idle → done (opt-in): a pane that hasn't changed
                    # for done_idle_secs while waiting/idle — and isn't blocked on
                    # a human — is finished. Headless relies on the sentinel only.
                    since = _parse_ts(meta.get("pane_changed_at"))
                    stable = (now_dt - since).total_seconds() if since else 0.0
                    if (since and stable >= done_idle
                            and _idle_done_allowed(text, cw)
                            and store.set_state(sid, "done", path=store_path)):
                        store.log_event(
                            sid, "idle_done",
                            f"pane stable {stable:.0f}s >= done_idle_secs "
                            f"{done_idle:.0f}s at a non-blocking prompt",
                            path=store_path,
                        )
                        finished += 1
                        actions.append(f"idle_done:{sid}")
        except Exception:
            # A single bad pane must not abort reconciliation of the rest.
            continue

    # 3. Reap dead processes.
    reaped = []
    try:
        reaped = reap_dead(store_path=store_path, now=now)
        for r in reaped:
            actions.append(f"reaped:{r.get('id')}")
    except Exception:
        reaped = []

    # 4. Budget — flag (do NOT kill) when the fleet is over the cap.
    try:
        from ecc import budget as _budget
        limit = cfg.get("budget_usd")
        # Re-read the fleet: reaping moved some sessions to terminal, and their
        # cost should drop out of the "active spend" total.
        try:
            live = store.list_sessions(active_only=True, path=store_path)
        except Exception:
            live = active
        total = 0.0
        for s in live:
            try:
                total += float(s.get("cost_usd") or 0.0)
            except (TypeError, ValueError):
                pass
        verdict = _budget.evaluate_budget(total, limit)
        budget_state = verdict.get("state", "unconfigured")
        if limit and budget_state == "over":
            store.log_event(
                None, "budget_over",
                f"active spend ${total:.4f} >= budget ${float(limit):.4f}",
                path=store_path,
            )
            actions.append("budget_over")
    except Exception:
        pass

    # 5. Overlap — flag files touched by >=2 in-flight sessions.
    try:
        from ecc import overlap as _overlap
        try:
            live = store.list_sessions(active_only=True, path=store_path)
        except Exception:
            live = active
        # Map orch records to the shape overlap.file_overlaps expects: a
        # session_id + a files_touched list (pulled from meta if present). These
        # are all already active (active_only), so disable overlap's own outcome
        # gate and let every record through.
        mapped = []
        for s in live:
            meta = s.get("meta") or {}
            files = meta.get("files_touched") if isinstance(meta, dict) else None
            mapped.append({
                "session_id": s.get("id"),
                "project": s.get("project"),
                "files_touched": files or [],
            })
        found = _overlap.file_overlaps(mapped, active_only=False)
        overlaps_n = len(found)
        for o in found:
            store.log_event(
                None, "overlap",
                f"{o['file']} touched by {o['count']} sessions: "
                f"{','.join(str(x) for x in o['sessions'])}",
                path=store_path,
            )
            actions.append(f"overlap:{o['file']}")
    except Exception:
        pass

    # 6. Auto-nudge — only if the sub-toggle is on AND we have a gate to consult.
    if cfg.get("auto_nudge") and sn is not None:
        # Re-read so the nudge pass sees freshly-reconciled states (a session we
        # just moved to `waiting` should be eligible this same tick).
        try:
            live = store.list_sessions(active_only=True, path=store_path)
        except Exception:
            live = active
        nudge_cap = int(cfg.get("nudge_max") or 0)
        for s in live:
            if s.get("state") != "waiting":
                continue
            sid = s.get("id")
            target = s.get("tmux_target")
            try:
                text = pane_cache.get(sid)
                if text is None:
                    text = cap(target) if target else ""
                meta_s = s.get("meta") if isinstance(s.get("meta"), dict) else {}

                # Lifetime nudge cap: a finished interactive agent at its prompt
                # classifies as a routine wait, so an uncapped nudger would loop
                # nudge → reply → nudge forever (each reply resets the idle
                # ledger). After nudge_max nudges the session is parked for the
                # operator instead (and the stable pane can then idle to done).
                count = int(meta_s.get("nudge_count") or 0)
                if nudge_cap > 0 and count >= nudge_cap:
                    if not meta_s.get("nudge_capped"):
                        new_meta = dict(meta_s)
                        new_meta["nudge_capped"] = True
                        store.update_session(sid, path=store_path, meta=new_meta)
                        store.log_event(
                            sid, "nudge_capped",
                            f"{count} nudges sent; cap {nudge_cap} reached — "
                            "surfacing to operator", path=store_path,
                        )
                        actions.append(f"nudge_capped:{sid}")
                    continue

                # Provide the explicit waiting_secs `should_nudge` prefers, from
                # the pane-stability ledger. Without it the gate would derive
                # idleness from heartbeat_at — which reconcile bumps every pass,
                # so the nudge_idle_secs threshold could never be reached.
                changed_at = _parse_ts(meta_s.get("pane_changed_at"))
                if changed_at is not None:
                    s = dict(s)
                    s["waiting_secs"] = max(
                        0.0, (now_dt - changed_at).total_seconds())
                # The gate sees the pane TAIL (current context): a question or
                # error that scrolled out of view is not pending any more, and a
                # finished agent must not be re-nudged because an old routine
                # prompt is still visible higher up.
                decision = sn(s, _pane_tail(text), cfg)
                ok = decision[0] if isinstance(decision, (tuple, list)) else bool(decision)
                reason = (decision[1] if isinstance(decision, (tuple, list))
                          and len(decision) > 1 else "")
                if not ok:
                    continue
                keys = nt() if nt is not None else ""
                snd(target, keys)
                new_meta = dict(meta_s)
                new_meta["nudge_count"] = count + 1
                store.update_session(sid, path=store_path, meta=new_meta)
                store.touch_heartbeat(sid, path=store_path)
                store.log_event(sid, "nudged", str(reason), path=store_path)
                nudged += 1
                actions.append(f"nudged:{sid}")
            except Exception:
                # A flaky nudge for one session must not block the others.
                continue

    # 7. Auto-merge finished worktrees (only when the sub-toggle is on).
    merged = 0
    try:
        from . import merge as _merge
        if cfg.get("auto_merge"):
            results = _merge.merge_ready_worktrees(config=cfg, store_path=store_path)
            merged = sum(1 for r in results if r.get("status") == "merged")
            for r in results:
                actions.append(f"merge:{r.get('session')}:{r.get('status')}")
    except Exception:
        pass

    # 8. Prune worktrees of terminal sessions (safe every pass — terminal-only).
    pruned = 0
    try:
        from . import merge as _merge
        pruned = len(_merge.prune_worktrees(store_path=store_path))
    except Exception:
        pass

    return {
        "enabled": True,
        "actions": actions,
        "reconciled": reconciled,
        "reaped": len(reaped),
        "finished": finished,
        "nudged": nudged,
        "overlaps": overlaps_n,
        "merged": merged,
        "pruned": pruned,
        "budget": budget_state,
    }


# Re-exported so callers that want ECC's original spelling still resolve it.
pid_is_alive = pid_alive

# Silence "imported but unused" for the stdlib symbols kept for the default
# callbacks' platform behaviour / signature clarity.
_ = (signal, errno)
