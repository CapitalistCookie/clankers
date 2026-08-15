"""Behavioral tests for lib/web/livewatch.js — the live-terminal liveness layer.

Run under real node against the real module (no DOM needed: livewatch.js is
pure by construction, which is WHY it was extracted). These are regression
tests for the 2026-08-15 "terminal stops updating until you close and reopen
the session" bug, whose two causes were:

  1. the paint latch never cleared when a frozen mobile tab discarded a pending
     requestAnimationFrame callback -> every later frame silently dropped;
  2. recovery keyed on WebSocket.readyState, which stays OPEN forever on a
     half-open socket -> no reconnect ever fired.

A fake clock and a fake rAF let us reproduce both deterministically, including
the freeze/discard sequence that is otherwise only reachable by backgrounding
a phone browser at exactly the wrong moment.
"""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

_WATCH = os.path.join(os.path.dirname(__file__), "..", "lib", "web", "livewatch.js")

# Fake clock + fake rAF harness. `rafQ` holds armed callbacks so a test can
# choose to run them (normal) or DISCARD them (frozen tab).
_HARNESS = """
const W = require(%s);
let t = 0;
const now = () => t;
const advance = (ms) => { t += ms; };
let rafQ = [], rafId = 0;
const raf = (fn) => { rafId++; rafQ.push([rafId, fn]); return rafId; };
const cancelRaf = (id) => { rafQ = rafQ.filter(([i]) => i !== id); };
const runRaf = () => { const q = rafQ; rafQ = []; q.forEach(([, fn]) => fn()); };
const discardRaf = () => { rafQ = []; };   // frozen tab: callbacks never delivered
const results = [];
function check(name, cond) { results.push({ name, ok: !!cond }); }
"""

_REPORT = """
const failed = results.filter(r => !r.ok);
console.log(JSON.stringify({ total: results.length, failed: failed.map(f => f.name) }));
process.exit(failed.length ? 1 : 0);
"""


def _run_js(body):
    src = (_HARNESS % json.dumps(os.path.abspath(_WATCH))) + body + _REPORT
    with tempfile.NamedTemporaryFile("w", suffix="-livewatch-test.js",
                                     delete=False) as f:
        f.write(src)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (
            f"node assertions failed: {r.stdout.strip()}\n{r.stderr[:800]}")
        out = json.loads(r.stdout.strip().splitlines()[-1])
        assert out["total"] > 0 and not out["failed"]
        return out
    finally:
        os.unlink(path)


pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")


def test_paint_latch_coalesces_and_paints():
    out = _run_js("""
    let painted = 0;
    const latch = W.createPaintLatch({ raf, cancelRaf, now, staleMs: 1000 });
    check('first schedule arms', latch.schedule(() => painted++) === true);
    check('second schedule coalesces', latch.schedule(() => painted++) === false);
    check('nothing painted before rAF runs', painted === 0);
    runRaf();
    check('exactly one paint per frame', painted === 1);
    check('latch released after paint', latch.pending() === false);
    // and it keeps working frame after frame
    latch.schedule(() => painted++); runRaf();
    check('second frame paints', painted === 2);
    """)
    assert out["total"] == 6


def test_paint_latch_self_heals_when_callback_is_discarded():
    """THE regression test. Freeze the tab between arming rAF and its callback
    (the browser drops it), then keep streaming frames: painting must resume
    WITHOUT anyone closing and reopening the terminal."""
    out = _run_js("""
    let painted = 0;
    const latch = W.createPaintLatch({ raf, cancelRaf, now, staleMs: 1000 });
    latch.schedule(() => painted++);
    discardRaf();                       // tab frozen: callback never delivered
    check('latch still believes a paint is in flight', latch.pending() === true);
    advance(200);
    check('a fresh frame is coalesced while plausibly in flight',
          latch.schedule(() => painted++) === false);
    advance(1000);                      // now past staleMs: presume discarded
    check('stale latch re-arms', latch.schedule(() => painted++) === true);
    runRaf();
    check('painting resumed with no teardown', painted === 1);
    // the OLD behavior: without self-healing, painted would stay 0 forever
    advance(50); latch.schedule(() => painted++); runRaf();
    check('and keeps painting after recovery', painted === 2);
    """)
    assert out["total"] == 5


def test_paint_latch_discarded_callback_cannot_double_paint():
    """A callback discarded, then delivered late (browser resumes the queue)
    must not paint on top of the re-armed one."""
    out = _run_js("""
    let painted = 0;
    const latch = W.createPaintLatch({ raf, cancelRaf, now, staleMs: 1000 });
    latch.schedule(() => painted++);
    const stale = rafQ.slice();          // capture the 'lost' callback
    rafQ = [];
    advance(1500);
    latch.schedule(() => painted++);     // re-armed
    stale.forEach(([, fn]) => fn());     // late delivery of the superseded frame
    check('superseded callback is inert', painted === 0);
    runRaf();
    check('only the live frame paints', painted === 1);
    latch.clear();
    check('clear releases the latch', latch.pending() === false);
    """)
    assert out["total"] == 3


def test_stall_watchdog_detects_silence():
    """Half-open socket: readyState never changes, so silence is the only signal."""
    out = _run_js("""
    const wd = W.createStallWatchdog({ now, stallMs: 30000, graceMs: 4000 });
    check('fresh watchdog is not stalled', wd.stalled() === false);
    advance(29000);
    check('under threshold is not stalled', wd.stalled() === false);
    advance(2000);
    check('silence past threshold stalls', wd.stalled() === true);
    wd.touch();
    check('a frame clears the stall', wd.stalled() === false);
    """)
    assert out["total"] == 4


def test_stall_watchdog_idle_pane_with_heartbeats_never_stalls():
    """An idle session sends no content — only the server heartbeat keeps it
    alive. 10 minutes of pure heartbeats must never trip the watchdog."""
    out = _run_js("""
    const wd = W.createStallWatchdog({ now, stallMs: 30000, graceMs: 4000 });
    let tripped = false;
    for (let i = 0; i < 60; i++) {       // 60 heartbeats x 10s = 10 min idle
      advance(10000);
      if (wd.stalled()) tripped = true;
      wd.touch();                        // hb frame arrives
    }
    check('idle pane with heartbeats never stalls', tripped === false);
    """)
    assert out["total"] == 1


def test_stall_watchdog_hidden_tab_grace_then_verdict():
    """Backgrounded tabs throttle timers and suspend delivery, so a gap while
    hidden proves nothing; on return, buffered frames get a grace window before
    any verdict — but a genuinely dead stream is still caught."""
    out = _run_js("""
    // (a) dead stream: nothing arrives on resume
    const dead = W.createStallWatchdog({ now, stallMs: 30000, graceMs: 4000 });
    dead.setHidden(true);
    advance(600000);                     // 10 min backgrounded
    check('hidden never stalls', dead.stalled() === false);
    dead.setHidden(false);
    check('grace window suppresses the verdict', dead.stalled() === false);
    advance(4500);
    check('after grace, a dead stream is caught', dead.stalled() === true);

    // (b) live stream: the suspended queue drains on resume
    const live = W.createStallWatchdog({ now, stallMs: 30000, graceMs: 4000 });
    live.setHidden(true);
    advance(600000);
    live.setHidden(false);
    advance(500); live.touch();          // buffered frames land during grace
    advance(4500);
    check('a recovered stream is not reconnected', live.stalled() === false);
    """)
    assert out["total"] == 4
