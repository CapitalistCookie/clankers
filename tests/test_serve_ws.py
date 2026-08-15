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
