// Liveness primitives for the live-terminal sockets — PURE (no DOM, no globals),
// so node can unit-test them directly (tests/test_livewatch_js.py).
//
// Both exist because of the 2026-08-15 "terminal stops updating, close+reopen
// fixes it" bug. Its two independent causes shared one root: every recovery path
// keyed on a PROXY for health (an rAF handle being non-null / WebSocket
// .readyState) instead of the OUTCOME (did the frame paint, are frames arriving).
//
//   PaintLatch    — coalesces frames onto requestAnimationFrame and SELF-HEALS
//                   when a scheduled callback is never delivered. A mobile tab
//                   frozen between rAF-arm and rAF-fire discards the pending
//                   callback; the old `if (viewRaf) return` then dropped every
//                   later frame FOREVER, and only closeTerminal cleared it.
//   StallWatchdog — declares the stream dead from DATA SILENCE, not readyState.
//                   A half-open socket (mobile network switch, NAT/tunnel drop)
//                   stays OPEN with no close event, so onclose-driven reconnects
//                   never fire. Requires the server's app-level heartbeat
//                   (serve.py VIEW_HEARTBEAT_SECS) so an idle pane still emits
//                   frames — then silence means exactly one thing: it's dead.
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.ClankerLiveWatch = api;
})(typeof self !== 'undefined' ? self : null, function () {
  'use strict';

  // opts: {raf, cancelRaf, now, staleMs}
  // staleMs = how long a scheduled-but-unpainted frame may sit before we presume
  // its callback was discarded. Longer than any real frame interval (a 1fps
  // display is 1000ms) and far shorter than a human noticing a frozen pane.
  function createPaintLatch(opts) {
    var raf = opts.raf, cancelRaf = opts.cancelRaf, now = opts.now;
    var staleMs = opts.staleMs == null ? 1000 : opts.staleMs;
    // `pending` (not `handle`) is the source of truth: a stub/synchronous raf
    // runs its callback before the handle is even assigned.
    var pending = false, handle = null, armedAt = 0, seq = 0;
    return {
      // Returns true when a paint was armed, false when coalesced into one
      // already in flight.
      schedule: function (paint) {
        if (pending) {
          if (now() - armedAt < staleMs) return false;   // genuinely in flight
          try { cancelRaf(handle); } catch (e) {}        // presumed discarded
          pending = false; handle = null;
        }
        var token = ++seq;
        pending = true;
        armedAt = now();
        handle = raf(function () {
          if (token !== seq) return;      // superseded by a re-arm; do nothing
          pending = false; handle = null;
          paint();
        });
        return true;
      },
      // Drop any in-flight paint and invalidate its callback (teardown, or a
      // foreground return where the pending callback may never be delivered).
      clear: function () {
        seq++;
        pending = false;
        if (handle !== null) { try { cancelRaf(handle); } catch (e) {} handle = null; }
      },
      pending: function () { return pending; }
    };
  }

  // opts: {now, stallMs, graceMs}
  // stallMs MUST exceed 2x the server heartbeat period, so one dropped or
  // late heartbeat can never be mistaken for a dead stream.
  function createStallWatchdog(opts) {
    opts = opts || {};
    var now = opts.now;
    var stallMs = opts.stallMs == null ? 30000 : opts.stallMs;
    var graceMs = opts.graceMs == null ? 4000 : opts.graceMs;
    var last = now(), hidden = false, graceUntil = 0;
    return {
      // Called for EVERY inbound frame — content, heartbeat, pong, terminal
      // bytes. Any byte from the server proves the path is alive.
      touch: function () { last = now(); },
      setHidden: function (h) {
        h = !!h;
        if (h === hidden) return;
        hidden = h;
        // Returning to the foreground: a suspended tab delivers its queued
        // frames on resume, so allow them to land before judging.
        if (!hidden) graceUntil = now() + graceMs;
      },
      stalled: function () {
        if (hidden) return false;              // throttled timers prove nothing
        if (now() < graceUntil) return false;  // let the resume queue drain
        return (now() - last) > stallMs;
      },
      sinceLast: function () { return now() - last; },
      reset: function () { last = now(); graceUntil = 0; }
    };
  }

  return {
    createPaintLatch: createPaintLatch,
    createStallWatchdog: createStallWatchdog
  };
});
