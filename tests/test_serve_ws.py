"""Live-socket integration tests: a REAL WebSocket client against the REAL
/ws/view handler (aiohttp test server, tmux stubbed out).

Regression cover for the 2026-08-15 "terminal stops updating" bug, server half:
  - an IDLE pane must still emit frames (heartbeat), else the browser cannot
    distinguish a quiet session from a dead socket — which is what let a
    half-open connection sit "connected" forever showing a frozen pane;
  - the client keepalive must be answered (pong), so liveness is provable
    round-trip;
  - a pane whose target moved must be re-resolved instead of streaming blank
    against the target captured once at connect;
  - a stream that ends must CLOSE the socket, never just stop sending.

tmux is never invoked: list_panes/capture_pane_ansi are monkeypatched. The
handler is mounted on a bare app (auth_middleware is covered in
test_serve_state.py; here we exercise the socket protocol itself).
"""
import asyncio
import json
import os
import sys
import tempfile
import time

import pytest
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

# Import-time isolation dance (see test_serve_state.py): importing serve pulls
# in webauth, which freezes CLANKER_DATA into module constants at import.
_OLD_DATA = os.environ.get("CLANKER_DATA")
os.environ["CLANKER_DATA"] = tempfile.mkdtemp(prefix="clk-servews-test-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import serve  # noqa: E402
if _OLD_DATA is None:
    os.environ.pop("CLANKER_DATA", None)
else:
    os.environ["CLANKER_DATA"] = _OLD_DATA

SESSION = "wstest"
TARGET = "wstest:0.0"


def _panes(target=TARGET, present=True):
    return [{"session": SESSION, "target": target}] if present else []


async def _collect(ws, want, timeout=6.0, limit=200):
    """Read frames until `want(frame)` is true. Returns (matched, all_frames).
    A closed socket ends the read — the caller asserts on that explicitly."""
    frames = []
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline and len(frames) < limit:
        try:
            msg = await ws.receive(timeout=max(0.05, deadline - loop.time()))
        except asyncio.TimeoutError:
            break
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            closed = {"type": "__closed__"}
            frames.append(closed)
            # a close IS an outcome a test may be waiting for — judge it, then stop
            return (closed if want(closed) else None), frames
        if msg.type is not WSMsgType.TEXT:
            continue
        try:
            frame = json.loads(msg.data)
        except ValueError:
            continue
        frames.append(frame)
        if want(frame):
            return frame, frames
    return None, frames


def _run(coro_factory, monkeypatch, hb=0.2, recheck=2):
    """Boot the handler on a test server and run one client coroutine."""
    monkeypatch.setattr(serve, "VIEW_HEARTBEAT_SECS", hb)
    monkeypatch.setattr(serve, "VIEW_EMPTY_RECHECK", recheck)

    async def main():
        app = web.Application()
        app.router.add_get("/ws/view/{session}", serve.handle_view)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            origin = f"http://{server.host}:{server.port}"
            ws = await client.ws_connect(f"/ws/view/{SESSION}",
                                         headers={"Origin": origin})
            try:
                return await coro_factory(ws)
            finally:
                await ws.close()
        finally:
            await client.close()

    return asyncio.run(main())


def test_idle_pane_still_heartbeats(monkeypatch):
    """The whole point: an unchanging pane must keep proving it is alive."""
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    monkeypatch.setattr(serve, "capture_pane_ansi",
                        lambda target, scrollback=False, **kw: "static pane\n")

    async def scenario(ws):
        first, _ = await _collect(ws, lambda f: f.get("type") == "content")
        assert first and first["data"] == "static pane\n"
        # ...and now nothing ever changes. Two heartbeats prove it is periodic,
        # not a one-off.
        hb1, _ = await _collect(ws, lambda f: f.get("type") == "hb")
        hb2, frames = await _collect(ws, lambda f: f.get("type") == "hb")
        assert hb1 and hb2, f"no periodic heartbeat on an idle pane: {frames}"
        assert all(f.get("type") != "__closed__" for f in frames)
        return True

    assert _run(scenario, monkeypatch) is True


def test_client_ping_is_answered(monkeypatch):
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    monkeypatch.setattr(serve, "capture_pane_ansi",
                        lambda target, scrollback=False, **kw: "x\n")

    async def scenario(ws):
        await ws.send_str(json.dumps({"type": "ping"}))
        pong, frames = await _collect(ws, lambda f: f.get("type") == "pong")
        assert pong, f"keepalive ping went unanswered: {frames}"
        return True

    # heartbeat pushed out of the way so the pong is not merely a stray hb
    assert _run(scenario, monkeypatch, hb=30) is True


def test_content_frames_follow_pane_changes(monkeypatch):
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    state = {"n": 0}

    def capture(target, scrollback=False, **kw):
        state["n"] += 1
        return f"frame {state['n'] // 3}\n"     # changes every few polls

    monkeypatch.setattr(serve, "capture_pane_ansi", capture)

    async def scenario(ws):
        f1, _ = await _collect(ws, lambda f: f.get("type") == "content")
        f2, frames = await _collect(
            ws, lambda f: f.get("type") == "content" and f["data"] != f1["data"])
        assert f1 and f2 and f1["data"] != f2["data"], frames
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_moved_target_is_reresolved(monkeypatch):
    """Pane moved (new window/pane index): capture_pane_ansi swallows the error
    and returns "", so without re-resolution the viewer streams blank forever."""
    moved = "wstest:9.9"
    calls = {"n": 0}

    def list_panes():
        # First call is the handler's connect-time resolution -> it captures the
        # STALE target. The pane moves immediately after, so every later lookup
        # reports the new one. Without re-resolution the viewer is stuck on the
        # stale target for the life of the socket.
        calls["n"] += 1
        return _panes(target=TARGET if calls["n"] == 1 else moved)

    monkeypatch.setattr(serve, "list_panes", list_panes)
    monkeypatch.setattr(
        serve, "capture_pane_ansi",
        lambda target, scrollback=False, **kw: (
            "alive on the new target\n" if target == moved else ""))

    async def scenario(ws):
        got, frames = await _collect(
            ws, lambda f: f.get("type") == "content" and "new target" in f.get("data", ""))
        assert got, f"stream never recovered onto the moved pane: {frames}"
        assert calls["n"] > 1, "target was never re-resolved"
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_vanished_session_closes_the_socket(monkeypatch):
    """A stream that cannot continue must CLOSE — the client's only other
    signal is silence, which is exactly the frozen-terminal failure."""
    alive = {"v": True}
    monkeypatch.setattr(serve, "list_panes", lambda: _panes(present=alive["v"]))

    def capture(target, scrollback=False, **kw):
        if alive["v"]:
            return "still here\n"
        return ""                      # pane gone: capture yields nothing

    monkeypatch.setattr(serve, "capture_pane_ansi", capture)

    async def scenario(ws):
        first, _ = await _collect(ws, lambda f: f.get("type") == "content")
        assert first
        alive["v"] = False             # session ends under the viewer
        closed, frames = await _collect(ws, lambda f: f.get("type") == "__closed__")
        assert closed, f"socket stayed open after the session vanished: {frames}"
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_heartbeat_threshold_is_under_the_client_stall_window():
    """Contract between serve.py and lib/web/live.js: the client declares a
    stall after STALL_MS of silence, so the server must heartbeat at least
    twice inside that window or a healthy idle session gets reconnect-looped."""
    web_dir = os.path.join(os.path.dirname(__file__), "..", "lib", "web")
    with open(os.path.join(web_dir, "live.js")) as f:
        js = f.read()
    import re
    m = re.search(r"const STALL_MS = (\d+)", js)
    assert m, "STALL_MS not found in live.js"
    stall_ms = int(m.group(1))
    assert serve.VIEW_HEARTBEAT_SECS * 1000 * 2 < stall_ms, (
        f"heartbeat {serve.VIEW_HEARTBEAT_SECS}s too slow for STALL_MS={stall_ms}")


# ══════════════════════════════════════════════════════════════════════════════
# Verified input (2026-09-10) — the CLIENT -> SERVER direction.
#
# Regression cover for "the terminal streams updates but nothing I type
# arrives": on 2026-09-10 three consecutive healthy sockets on the hyperliquid
# pane carried 148 seconds of the operator's typing into a void, and the box
# logged NOTHING about it — send-keys ran with capture_output=True and its exit
# status thrown away, so a lost keystroke and a delivered one were the same
# event. These tests pin the four properties that make that undiagnosable
# failure impossible: the outcome is checked, reported, survivable, and the one
# tmux state that silently eats input is healed.
# ══════════════════════════════════════════════════════════════════════════════

class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _stub_tmux(monkeypatch, in_mode="0", send_rc=0, send_stderr="", raises=None):
    """Replace subprocess.run under serve with a recorder. capture_pane_ansi
    uses check_output, so the content stream is unaffected."""
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if "display" in argv:
            return _FakeProc(0, in_mode, "")
        if "-X" in argv:                      # copy-mode cancel
            return _FakeProc(0, "", "")
        if raises is not None:
            raise raises
        return _FakeProc(send_rc, "", send_stderr)

    monkeypatch.setattr(serve.subprocess, "run", fake_run)
    return calls


def _sends(calls):
    return [c for c in calls if "send-keys" in c and "-X" not in c]


def test_meta_advertises_the_ack_protocol(monkeypatch):
    """A client must be able to TELL whether this server confirms delivery —
    assuming receipts against an older server would fail every send on timeout."""
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    monkeypatch.setattr(serve, "capture_pane_ansi",
                        lambda target, scrollback=False, **kw: "x\n")

    async def scenario(ws):
        meta, frames = await _collect(ws, lambda f: f.get("type") == "meta")
        assert meta, frames
        assert meta.get("ack") is True, f"ack support not advertised: {meta}"
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_input_is_delivered_and_acked(monkeypatch):
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    monkeypatch.setattr(serve, "capture_pane_ansi",
                        lambda target, scrollback=False, **kw: "x\n")
    calls = _stub_tmux(monkeypatch)

    async def scenario(ws):
        await ws.send_str(json.dumps({"type": "keys", "data": "hello", "seq": 7}))
        ack, frames = await _collect(ws, lambda f: f.get("type") == "ack")
        assert ack, f"input was never acknowledged: {frames}"
        assert ack["seq"] == 7 and ack["ok"] is True, ack
        sends = _sends(calls)
        assert sends and sends[-1] == ["tmux", "send-keys", "-t", TARGET, "-l", "--", "hello"], sends
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_failed_send_is_nacked_never_swallowed(monkeypatch):
    """THE core regression: send-keys' exit status used to be discarded, so a
    failure looked exactly like a success and the operator saw nothing."""
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    monkeypatch.setattr(serve, "capture_pane_ansi",
                        lambda target, scrollback=False, **kw: "x\n")
    _stub_tmux(monkeypatch, send_rc=1, send_stderr="can't find pane")

    async def scenario(ws):
        await ws.send_str(json.dumps({"type": "keys", "data": "hello", "seq": 1}))
        ack, frames = await _collect(ws, lambda f: f.get("type") == "ack")
        assert ack, f"a FAILED send went unreported: {frames}"
        assert ack["ok"] is False, ack
        assert "can't find pane" in ack.get("err", ""), ack
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_copy_mode_is_cancelled_before_input(monkeypatch):
    """A pane in tmux copy-mode routes send-keys into the copy-mode key table:
    the application never sees the keys, while capture-pane keeps returning
    content. That is precisely the "updates stream, input vanishes" shape, and
    it is invisible from the exit status (send-keys succeeds). Heal it first."""
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    monkeypatch.setattr(serve, "capture_pane_ansi",
                        lambda target, scrollback=False, **kw: "x\n")
    calls = _stub_tmux(monkeypatch, in_mode="1")

    async def scenario(ws):
        await ws.send_str(json.dumps({"type": "keys", "data": "hi", "seq": 3}))
        ack, frames = await _collect(ws, lambda f: f.get("type") == "ack")
        assert ack and ack["ok"] is True, f"{ack} {frames}"
        cancels = [c for c in calls if "-X" in c]
        assert cancels, f"pane was in copy-mode and was NOT cancelled: {calls}"
        assert cancels[0] == ["tmux", "send-keys", "-X", "-t", TARGET, "cancel"], cancels
        # order matters: cancel must precede the payload, or it lands in the mode
        assert calls.index(cancels[0]) < calls.index(_sends(calls)[-1])
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_healthy_pane_is_not_cancelled(monkeypatch):
    """Healing must be surgical: a pane that is NOT in a mode must never be sent
    an -X cancel (it would interrupt whatever the app is doing)."""
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    monkeypatch.setattr(serve, "capture_pane_ansi",
                        lambda target, scrollback=False, **kw: "x\n")
    calls = _stub_tmux(monkeypatch, in_mode="0")

    async def scenario(ws):
        await ws.send_str(json.dumps({"type": "keys", "data": "hi", "seq": 4}))
        ack, _ = await _collect(ws, lambda f: f.get("type") == "ack")
        assert ack and ack["ok"] is True
        assert not [c for c in calls if "-X" in c], f"spurious cancel: {calls}"
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_send_timeout_is_nacked_and_the_socket_keeps_working(monkeypatch):
    """A subprocess.TimeoutExpired out of send-keys used to escape the per-frame
    handler and end the whole input loop — every LATER keystroke on that socket
    was then dropped, with the content stream still running. One bad frame must
    cost one frame."""
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    monkeypatch.setattr(serve, "capture_pane_ansi",
                        lambda target, scrollback=False, **kw: "x\n")
    state = {"boom": True}
    real = serve._tmux_input

    def flaky(target, argv, heal_mode=True):
        if state["boom"]:
            state["boom"] = False
            raise subprocess_TimeoutExpired
        return real(target, argv, heal_mode=heal_mode)

    subprocess_TimeoutExpired = __import__("subprocess").TimeoutExpired("tmux", 2)
    monkeypatch.setattr(serve, "_tmux_input", flaky)
    _stub_tmux(monkeypatch)

    async def scenario(ws):
        await ws.send_str(json.dumps({"type": "keys", "data": "first", "seq": 1}))
        a1, frames = await _collect(ws, lambda f: f.get("type") == "ack" and f.get("seq") == 1)
        assert a1 and a1["ok"] is False, f"timeout not reported: {a1} {frames}"
        # ...and the socket is still in service for the NEXT keystroke
        await ws.send_str(json.dumps({"type": "keys", "data": "second", "seq": 2}))
        a2, frames = await _collect(ws, lambda f: f.get("type") == "ack" and f.get("seq") == 2)
        assert a2 and a2["ok"] is True, f"input loop died on one bad frame: {a2} {frames}"
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_disallowed_key_is_nacked_and_never_executed(monkeypatch):
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    monkeypatch.setattr(serve, "capture_pane_ansi",
                        lambda target, scrollback=False, **kw: "x\n")
    calls = _stub_tmux(monkeypatch)

    async def scenario(ws):
        await ws.send_str(json.dumps({"type": "key", "data": "-X", "seq": 5}))
        ack, frames = await _collect(ws, lambda f: f.get("type") == "ack")
        assert ack and ack["ok"] is False, f"{ack} {frames}"
        assert "not allowed" in ack.get("err", ""), ack
        assert not _sends(calls), f"rejected key still reached tmux: {calls}"
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_oversized_burst_is_rejected(monkeypatch):
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    monkeypatch.setattr(serve, "capture_pane_ansi",
                        lambda target, scrollback=False, **kw: "x\n")
    calls = _stub_tmux(monkeypatch)

    async def scenario(ws):
        await ws.send_str(json.dumps({"type": "keys",
                                      "data": "z" * (serve.VIEW_MAX_KEYS + 1), "seq": 6}))
        ack, _ = await _collect(ws, lambda f: f.get("type") == "ack")
        assert ack and ack["ok"] is False and "too large" in ack.get("err", ""), ack
        assert not _sends(calls), calls
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_keepalive_echoes_the_seq(monkeypatch):
    """An idle pane emits content nobody sent, so only the pong's seq can prove
    that OUR frames are still reaching the server."""
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    monkeypatch.setattr(serve, "capture_pane_ansi",
                        lambda target, scrollback=False, **kw: "x\n")

    async def scenario(ws):
        await ws.send_str(json.dumps({"type": "ping", "seq": 42}))
        pong, frames = await _collect(ws, lambda f: f.get("type") == "pong")
        assert pong and pong.get("seq") == 42, f"{pong} {frames}"
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_untracked_input_still_delivered_without_an_ack(monkeypatch):
    """Back-compat both ways: an older client sends no seq, and the high-rate
    wheel path deliberately does not either. Those must still be delivered —
    and must NOT produce ack frames an old client would not understand."""
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    monkeypatch.setattr(serve, "capture_pane_ansi",
                        lambda target, scrollback=False, **kw: "x\n")
    calls = _stub_tmux(monkeypatch)

    async def scenario(ws):
        await ws.send_str(json.dumps({"type": "keys", "data": "legacy"}))
        await ws.send_str(json.dumps({"type": "ping"}))
        pong, frames = await _collect(ws, lambda f: f.get("type") == "pong")
        assert pong and "seq" not in pong, pong
        assert not [f for f in frames if f.get("type") == "ack"], frames
        sends = _sends(calls)
        assert sends and sends[-1][-1] == "legacy", sends
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_input_does_not_block_the_content_stream(monkeypatch):
    """send-keys used to run inline on the event loop, so every keystroke froze
    the whole server for a tmux round trip and a hung one froze it for seconds.
    A slow send must not stop the frames."""
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    counter = {"n": 0}

    def capture(target, scrollback=False, **kw):
        counter["n"] += 1
        return f"frame {counter['n']}\n"        # changes every poll

    monkeypatch.setattr(serve, "capture_pane_ansi", capture)

    def slow(target, argv, heal_mode=True):
        time.sleep(1.0)                          # blocking, like the real tmux call
        return True, ""

    monkeypatch.setattr(serve, "_tmux_input", slow)

    async def scenario(ws):
        await _collect(ws, lambda f: f.get("type") == "content")
        await ws.send_str(json.dumps({"type": "keys", "data": "slow", "seq": 1}))
        seen = 0
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 6
        while loop.time() < deadline:
            msg = await ws.receive(timeout=max(0.05, deadline - loop.time()))
            if msg.type is not WSMsgType.TEXT:
                break
            f = json.loads(msg.data)
            if f.get("type") == "content":
                seen += 1
            if f.get("type") == "ack":
                break
        assert seen >= 3, (
            f"only {seen} content frames arrived during a 1s send — "
            "the input path is blocking the event loop again")
        return True

    assert _run(scenario, monkeypatch, hb=30) is True


def test_ack_timeout_contract_between_client_and_server():
    """Contract: the client gives up on a round trip after ACK_TIMEOUT_MS. That
    must comfortably exceed the server's own send-keys timeout, or a send that
    the server is still honestly working on gets reported as lost."""
    web_dir = os.path.join(os.path.dirname(__file__), "..", "lib", "web")
    with open(os.path.join(web_dir, "live.js")) as f:
        js = f.read()
    import re
    m = re.search(r"const ACK_TIMEOUT_MS = (\d+)", js)
    assert m, "ACK_TIMEOUT_MS not found in live.js"
    ack_ms = int(m.group(1))
    assert ack_ms > serve.VIEW_INPUT_TIMEOUT * 1000 * 2, (
        f"ACK_TIMEOUT_MS={ack_ms} too tight for VIEW_INPUT_TIMEOUT="
        f"{serve.VIEW_INPUT_TIMEOUT}s")
    # ...and the client must actually read the capability the server advertises.
    assert "msg.ack" in js, "live.js does not read the server's ack capability"


def test_view_slot_is_released_on_every_exit(monkeypatch):
    """MAX_VIEWS leaked slots lock every viewer out until a restart, so the
    counter must balance across normal closes AND early rejections."""
    monkeypatch.setattr(serve, "list_panes", lambda: _panes())
    monkeypatch.setattr(serve, "capture_pane_ansi",
                        lambda target, scrollback=False, **kw: "x\n")
    before = serve._view_count

    async def scenario(ws):
        await _collect(ws, lambda f: f.get("type") == "content")
        assert serve._view_count == before + 1, "viewer was never counted"
        return True

    assert _run(scenario, monkeypatch, hb=30) is True
    assert serve._view_count == before, (
        f"view slot leaked: {serve._view_count} != {before}")
