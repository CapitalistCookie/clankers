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


# ── UplinkTracker — the CLIENT -> SERVER direction (2026-09-10) ──────────────
# Regression cover for "the session streams updates but nothing I type arrives".
# The stall watchdog above cannot see that failure: it counts ANY inbound frame
# as proof of life, and a pane that is printing sends content forever. Only an
# echo of something WE sent proves the uplink still carries our frames.

def test_uplink_settles_a_round_trip():
    out = _run_js("""
    const up = W.createUplinkTracker({ now, timeoutMs: 6000, deadAfter: 2 });
    const a = up.track({ kind: 'compose', text: 'hello' });
    check('seqs start at 1', a === 1);
    check('one round trip outstanding', up.pending() === 1);
    advance(50);
    const entry = up.settle(a);
    check('settle returns what was tracked', entry && entry.meta.text === 'hello');
    check('nothing outstanding after the echo', up.pending() === 0);
    check('a settled round trip is not a loss', up.dead() === false);
    advance(60000);
    check('a settled entry can never expire later', up.sweep().length === 0);
    """)
    assert out["total"] == 6


def test_uplink_lost_send_is_reported_once_with_its_payload():
    """THE regression test: a send that is never echoed must come back to the
    caller — with the text — instead of being silently forgotten. That payload
    is what puts the operator's message back in the compose box."""
    out = _run_js("""
    const up = W.createUplinkTracker({ now, timeoutMs: 6000, deadAfter: 2 });
    up.track({ kind: 'compose', text: 'the message that vanished' });
    advance(3000);
    check('not judged while plausibly in flight', up.sweep().length === 0);
    advance(3500);                       // now past timeoutMs
    const lost = up.sweep();
    check('the lost send surfaces', lost.length === 1);
    check('it carries the text back', lost[0].meta.text === 'the message that vanished');
    check('reported ONCE, not every sweep', up.sweep().length === 0);
    check('and it counted as a loss', up.lost() === 1);
    """)
    assert out["total"] == 5


def test_uplink_declares_death_only_after_repeated_loss():
    out = _run_js("""
    const up = W.createUplinkTracker({ now, timeoutMs: 1000, deadAfter: 2 });
    up.track({ kind: 'ping' }); advance(1500); up.sweep();
    check('one lost round trip is not a verdict', up.dead() === false);
    up.track({ kind: 'ping' }); advance(1500); up.sweep();
    check('two in a row is', up.dead() === true);
    """)
    assert out["total"] == 2


def test_uplink_recovery_clears_the_loss_streak():
    """A slow path that comes back must not stay condemned — otherwise every
    later send reconnects the socket forever."""
    out = _run_js("""
    const up = W.createUplinkTracker({ now, timeoutMs: 1000, deadAfter: 2 });
    up.track({ kind: 'ping' }); advance(1500); up.sweep();
    check('one loss recorded', up.lost() === 1);
    const s = up.track({ kind: 'ping' }); up.settle(s);
    check('a successful echo clears the streak', up.lost() === 0);
    check('and the verdict with it', up.dead() === false);
    """)
    assert out["total"] == 3


def test_uplink_late_echo_is_not_a_fresh_success():
    """A seq we already gave up on must not settle as if it worked — the caller
    has already restored that message, and acking it twice would be a lie."""
    out = _run_js("""
    const up = W.createUplinkTracker({ now, timeoutMs: 1000, deadAfter: 5 });
    const s = up.track({ kind: 'compose', text: 'x' });
    advance(1500);
    check('given up on', up.sweep().length === 1);
    check('the late echo settles nothing', up.settle(s) === null);
    check('and does not clear the loss streak', up.lost() === 1);
    check('an unknown seq is also nothing', up.settle(9999) === null);
    """)
    assert out["total"] == 4


def test_uplink_teardown_drains_without_condemning_the_next_socket():
    """closeTerminal fires a burst of page-downs and then closes. Those can
    never be echoed, so they must be handed back as failures — but a deliberate
    teardown is not evidence that the NEXT socket's uplink is dead."""
    out = _run_js("""
    const up = W.createUplinkTracker({ now, timeoutMs: 6000, deadAfter: 2 });
    up.track({ kind: 'key' }); up.track({ kind: 'key' }); up.track({ kind: 'key' });
    const drained = up.sweep(true);
    check('everything in flight is handed back', drained.length === 3);
    check('nothing left outstanding', up.pending() === 0);
    check('a teardown is not a loss streak', up.lost() === 0);
    check('so no death verdict', up.dead() === false);
    up.reset();
    check('reset is clean', up.pending() === 0 && up.lost() === 0);
    """)
    assert out["total"] == 5
