"""Behavioral tests for the compose bar's DELIVERY CONTRACT in lib/web/live.js.

The 2026-09-10 bug lived in the wiring, not in a primitive: composeSend cleared
the textarea and pushed the message into history the instant the button was
pressed, and sendText returned silently whenever the socket was not OPEN. A
message that never left the browser was therefore pixel-identical to one that
was delivered — the operator's only signal was that nothing happened, and their
text was gone. Three healthy sockets ate 148 seconds of typing that way.

test_livewatch_js.py covers the pure tracker; test_spa_js.py covers syntax.
Neither could have caught this, because the defect was in how live.js USES
them. So this file loads the REAL live.js into a node vm on a deliberately
permissive fake DOM and drives the real functions.

The DOM stub answers ANY id with a generic element and swallows unknown calls —
that is on purpose. It keeps the harness from rotting every time an unrelated
part of the SPA touches a new global, while still failing loudly on the thing
under test.
"""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

_WEB = os.path.join(os.path.dirname(__file__), "..", "lib", "web")
_LIVE = os.path.abspath(os.path.join(_WEB, "live.js"))
_WATCH = os.path.abspath(os.path.join(_WEB, "livewatch.js"))

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

// ── permissive fake DOM ────────────────────────────────────────────────────
// Unknown property -> no-op function. Unknown id -> a fresh element. The SPA
// may grow; this harness must not need edits every time it does.
function makeEl(id) {
  const el = {
    id: id || '', value: '', textContent: '', innerHTML: '', tagName: 'DIV',
    scrollTop: 0, scrollHeight: 0, clientHeight: 0, offsetTop: 0,
    hidden: false, disabled: false, checked: false,
    style: {}, dataset: {}, children: [], _handlers: {},
    classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      toggle(c, on) { if (on === undefined) on = !this._s.has(c);
                      on ? this._s.add(c) : this._s.delete(c); return on; },
      contains(c) { return this._s.has(c); },
    },
    addEventListener(ev, fn) { (this._handlers[ev] = this._handlers[ev] || []).push(fn); },
    removeEventListener() {},
    dispatch(ev, arg) { (this._handlers[ev] || []).forEach(f => f(arg || {})); },
    appendChild(c) { this.children.push(c); return c; },
    removeChild() {}, remove() {}, insertBefore(c) { return c; },
    focus() {}, blur() {}, click() { this.dispatch('click', { preventDefault() {} }); },
    setAttribute() {}, getAttribute() { return null; },
    querySelector() { return makeEl(); }, querySelectorAll() { return []; },
    getBoundingClientRect() { return { top: 0, left: 0, width: 100, height: 100 }; },
    scrollIntoView() {}, closest() { return null; },
  };
  return new Proxy(el, {
    get(t, k) {
      if (k in t) return t[k];
      if (typeof k === 'symbol') return undefined;
      return () => {};      // unknown DOM method: harmless no-op
    },
    set(t, k, v) { t[k] = v; return true; },
  });
}

const els = {};
const store = {};
const sent = [];           // every frame the code handed to the socket

class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 1;   // OPEN — tests flip it deliberately
    this.bufferedAmount = 0;
    this.onopen = this.onmessage = this.onclose = this.onerror = null;
    FakeSocket.last = this;
  }
  send(s) {
    if (this.readyState !== 1) throw new Error('InvalidStateError');
    sent.push(JSON.parse(s));
  }
  close() { this.readyState = 3; }
  addEventListener() {}
  deliver(obj) { if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) }); }
}
FakeSocket.CONNECTING = 0; FakeSocket.OPEN = 1;
FakeSocket.CLOSING = 2;    FakeSocket.CLOSED = 3;

const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval,
  JSON, Math, Date, Set, Map, Promise, Object, Array, String, Number,
  RegExp, Error, parseInt, parseFloat, isNaN, encodeURIComponent,
  decodeURIComponent, WebSocket: FakeSocket,
  requestAnimationFrame: (fn) => { setTimeout(fn, 0); return 1; },
  cancelAnimationFrame: () => {},
  ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  fetch: () => new Promise(() => {}),          // never resolves: no status polling
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
  navigator: { vibrate: () => {}, userAgent: 'node', maxTouchPoints: 0 },
  location: { protocol: 'https:', host: 'example.invalid', href: 'https://example.invalid/' },
  matchMedia: () => ({ matches: true, addEventListener() {}, addListener() {} }),
  document: {
    hidden: false,
    body: makeEl('body'),
    documentElement: makeEl('html'),
    getElementById: (id) => (els[id] = els[id] || makeEl(id)),
    createElement: (t) => makeEl(t),
    querySelector: () => makeEl(),
    querySelectorAll: () => [],
    addEventListener: () => {},
    removeEventListener: () => {},
  },
  visualViewport: { offsetTop: 0, offsetLeft: 0, width: 400, height: 800,
                    addEventListener() {} },
  addEventListener: () => {},           // window.*: pageshow, resize, keydown…
  removeEventListener: () => {},
  scrollTo: () => {}, getComputedStyle: () => ({}),
  innerWidth: 400, innerHeight: 800, devicePixelRatio: 2,
};
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(%(watch)s, 'utf8'), sandbox, { filename: 'livewatch.js' });
vm.runInContext(fs.readFileSync(%(live)s, 'utf8'), sandbox, { filename: 'live.js' });

// Reach live.js's top-level `let` bindings, which a vm script keeps lexically
// scoped. Written here (not in live.js) so production ships no test hooks.
vm.runInContext(`
  globalThis.__t = {
    composeSend, sendText, wsKey, connectView, closeTerminal, _ackSweep,
    setMode: (m) => { liveMode = m; liveName = 'sess'; },
    ackSupported: () => viewAckSupported,
    uplinkPending: () => (uplink ? uplink.pending() : -1),
    ACK_TIMEOUT_MS,
  };
`, sandbox);

const T = sandbox.__t;
const el = (id) => sandbox.document.getElementById(id);
const results = [];
function check(name, cond) { results.push({ name, ok: !!cond }); }
function reset() { sent.length = 0; }
// Bring a session up exactly as openTerminal would, then hand it the server's
// meta frame so ack support is negotiated rather than assumed.
function connect(ackSupported) {
  T.setMode('view');
  T.connectView('sess');
  const ws = FakeSocket.last;
  ws.deliver(ackSupported === false ? { type: 'meta', tui: true }
                                    : { type: 'meta', tui: true, ack: true });
  reset();
  return ws;
}
"""

_REPORT = """
const failed = results.filter(r => !r.ok);
console.log(JSON.stringify({ total: results.length, failed: failed.map(f => f.name) }));
process.exit(failed.length ? 1 : 0);
"""


def _run_js(body):
    src = (_HARNESS % {"watch": json.dumps(_WATCH), "live": json.dumps(_LIVE)})
    src += body + _REPORT
    with tempfile.NamedTemporaryFile("w", suffix="-livejs-test.js",
                                     delete=False) as f:
        f.write(src)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (
            f"node assertions failed: {r.stdout.strip()}\n{r.stderr[:1500]}")
        out = json.loads(r.stdout.strip().splitlines()[-1])
        assert out["total"] > 0 and not out["failed"]
        return out
    finally:
        os.unlink(path)


def test_live_js_loads_and_wires_the_compose_bar():
    """Load-time sanity: the whole SPA evaluates, and the send button really is
    bound. An IIFE that binds listeners at load is exactly the kind of thing
    that silently does nothing if the element is not there yet."""
    out = _run_js("""
    check('live.js evaluated', typeof T.composeSend === 'function');
    const btn = el('compose-send');
    check('send button has a click handler', (btn._handlers.click || []).length === 1);
    const inp = el('compose-input');
    check('input has a keydown handler', (inp._handlers.keydown || []).length === 1);
    """)
    assert out["total"] == 3


def test_message_is_sent_as_text_then_enter_with_receipts():
    out = _run_js("""
    const ws = connect(true);
    el('compose-input').value = 'hello world';
    check('send reports success', T.composeSend() === true);
    check('two frames on the wire', sent.length === 2);
    check('literal text first', sent[0].type === 'keys' && sent[0].data === 'hello world');
    check('then the submit', sent[1].type === 'key' && sent[1].data === 'Enter');
    check('both carry receipts', typeof sent[0].seq === 'number' && typeof sent[1].seq === 'number');
    check('receipts are distinct', sent[0].seq !== sent[1].seq);
    check('box cleared once it left the browser', el('compose-input').value === '');
    check('both round trips outstanding', T.uplinkPending() === 2);
    ws.deliver({ type: 'ack', seq: sent[0].seq, ok: true });
    ws.deliver({ type: 'ack', seq: sent[1].seq, ok: true });
    check('acks settle them', T.uplinkPending() === 0);
    check('no failure shown', el('compose-note').classList.contains('show') === false);
    """)
    assert out["total"] == 10


def test_closed_socket_keeps_the_message_instead_of_eating_it():
    """THE regression test. Before the fix this path cleared the box, pushed the
    text into history and sent nothing — the exact reported symptom."""
    out = _run_js("""
    const ws = connect(true);
    ws.readyState = 3;                       // CLOSED under the operator
    el('compose-input').value = 'do not lose me';
    check('send reports failure', T.composeSend() === false);
    check('nothing was written to a dead socket', sent.length === 0);
    check('the message is still in the box', el('compose-input').value === 'do not lose me');
    check('and the failure is visible', el('compose-note').classList.contains('show') === true);
    check('with a reason', /not sent/.test(el('compose-note').textContent));
    """)
    assert out["total"] == 5


def test_connecting_socket_is_not_mistaken_for_a_live_one():
    """A socket mid-handshake reports CONNECTING, not OPEN. Sends must fail
    loudly rather than vanish into a socket that cannot carry them yet."""
    out = _run_js("""
    const ws = connect(true);
    ws.readyState = 0;                       // CONNECTING
    el('compose-input').value = 'early';
    check('send fails', T.composeSend() === false);
    check('nothing sent', sent.length === 0);
    check('text preserved', el('compose-input').value === 'early');
    """)
    assert out["total"] == 3


def test_server_nack_puts_the_message_back():
    """The server ran send-keys and it FAILED. The operator must get their text
    back, not a silent success."""
    out = _run_js("""
    const ws = connect(true);
    el('compose-input').value = 'rejected message';
    T.composeSend();
    check('box cleared optimistically', el('compose-input').value === '');
    ws.deliver({ type: 'ack', seq: sent[0].seq, ok: false, err: "can't find pane" });
    check('text restored on nack', el('compose-input').value === 'rejected message');
    check('failure surfaced', el('compose-note').classList.contains('show') === true);
    check("reason carried through", /not sent/.test(el('compose-note').textContent));
    """)
    assert out["total"] == 4


def test_silence_restores_the_message_and_rebuilds_the_socket():
    """No ack at all — the shape of a half-open uplink, where readyState stays
    OPEN forever and the downstream keeps streaming. Nothing else in the client
    can see this."""
    out = _run_js("""
    const ws = connect(true);
    el('compose-input').value = 'swallowed by the tunnel';
    T.composeSend();
    check('two round trips in flight', T.uplinkPending() === 2);
    T._ackSweep();
    check('not judged while plausibly in flight', el('compose-input').value === '');
    // Let both round trips age past ACK_TIMEOUT_MS without an answer.
    const realNow = Date.now;
    Date.now = () => realNow() + T.ACK_TIMEOUT_MS + 1000;
    T._ackSweep();
    Date.now = realNow;
    check('the message comes back', el('compose-input').value === 'swallowed by the tunnel');
    check('and the operator is told', el('compose-note').classList.contains('show') === true);
    check('a dead uplink rebuilds the socket', FakeSocket.last !== ws);
    """)
    assert out["total"] == 5


def test_stalled_send_buffer_fails_loudly():
    """bufferedAmount is the one signal a half-open socket cannot fake: bytes
    queued in the browser that the socket never drains. readyState says OPEN
    the whole time."""
    out = _run_js("""
    const ws = connect(true);
    ws.bufferedAmount = 4 * 1024 * 1024;     // queued and not draining
    el('compose-input').value = 'backed up';
    check('send fails', T.composeSend() === false);
    check('nothing queued on top', sent.length === 0);
    check('text preserved', el('compose-input').value === 'backed up');
    """)
    assert out["total"] == 3


def test_old_server_without_receipts_still_works():
    """Forward/backward skew: a server that does not advertise ack support must
    not have every send reported as lost. Fire-and-forget, exactly as before."""
    out = _run_js("""
    const ws = connect(false);
    check('capability not assumed', T.ackSupported() === false);
    el('compose-input').value = 'legacy path';
    check('send succeeds', T.composeSend() === true);
    check('two frames sent', sent.length === 2);
    check('no receipts attached', sent[0].seq === undefined && sent[1].seq === undefined);
    check('nothing is tracked', T.uplinkPending() === 0);
    const realNow = Date.now;
    Date.now = () => realNow() + 600000;
    T._ackSweep();
    Date.now = realNow;
    check('and nothing is ever falsely reported lost',
          el('compose-note').classList.contains('show') === false);
    check('box stays cleared', el('compose-input').value === '');
    """)
    assert out["total"] == 7


def test_scroll_paging_is_not_tracked():
    """closeTerminal fires 16 page-downs and then closes the socket. Tracking
    those would report 16 losses on every single close."""
    out = _run_js("""
    connect(true);
    T.wsKey('Enter');                        // a real key IS tracked
    check('user keys are tracked', T.uplinkPending() === 1);
    const before = T.uplinkPending();
    T.closeTerminal();
    check('teardown leaves nothing outstanding', T.uplinkPending() === -1);
    """)
    assert out["total"] == 2
