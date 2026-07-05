"""Mount watchyourclankers (the read-only IDE-spectator) under /wyc/ — M2 of the
wyc→clanker merge (watchyourclankers docs/MASTER_PLAN.md "Merge-into-clanker seam").

GRACEFUL BY DESIGN: if the `watchyourclankers` package isn't importable this is a
no-op and clanker runs exactly as before — so create_app() can call it
unconditionally and a clanker without wyc installed is unaffected.

Auth: clanker's auth_middleware (on the PARENT app) also runs for sub-app requests,
so /wyc/* is gated by the same signed clanker_session cookie. wyc adds NO auth of
its own (build_app auth=None) — the host owns auth. The wyc frontend derives its
own BASE from import.meta.url, so it serves correctly under the /wyc/ prefix with
no server-side URL rewriting.

Lifecycle: build_app() only WIRES the app; wyc's own serve() additionally runs the
watcher poll/tail/stitch loop + a snapshot publisher. We start those on the
sub-app's on_startup (and cancel on cleanup) or the spectator shows no live data.

Production install:
    pip install 'git+https://github.com/CapitalistCookie/watchyourclankers'
"""
import asyncio
import logging
import os
import sys

log = logging.getLogger("clanker.wyc")

# Dev fallback: the source checkout, used only if the package isn't pip-installed.
_WYC_DEV_PATH = os.path.expanduser("~/projects/watchyourclankers")


def _import_wyc():
    """Return (build_app, snapshot_publisher, Watcher) or (None, None, None)."""
    try:
        from wyc.server import build_app, _snapshot_publisher
        from wyc.watcher import Watcher
        return build_app, _snapshot_publisher, Watcher
    except Exception:
        if os.path.isdir(_WYC_DEV_PATH) and _WYC_DEV_PATH not in sys.path:
            sys.path.append(_WYC_DEV_PATH)
            try:
                from wyc.server import build_app, _snapshot_publisher
                from wyc.watcher import Watcher
                return build_app, _snapshot_publisher, Watcher
            except Exception as e:
                log.info("watchyourclankers at %s not importable: %s", _WYC_DEV_PATH, e)
        return None, None, None


def mount_wyc(app):
    """add_subapp('/wyc/', wyc) if watchyourclankers is available; else no-op.

    Returns True iff mounted. Never raises — a failure here must not stop clanker
    from serving the dashboard."""
    build_app, snapshot_publisher, Watcher = _import_wyc()
    if build_app is None:
        log.info("watchyourclankers not installed — /wyc/ spectator NOT mounted "
                 "(pip install 'git+https://github.com/CapitalistCookie/watchyourclankers')")
        return False
    try:
        watcher = Watcher()
        wyc_app = build_app(watcher, auth=None, url_prefix="/wyc")
    except Exception as e:
        log.warning("watchyourclankers build_app failed; /wyc/ not mounted: %s", e)
        return False

    tasks = []

    async def _on_start(_a):
        # start the watcher's poll/tail/stitch loop + the snapshot publisher on
        # clanker's event loop (mirrors wyc.server.serve()).
        tasks.append(asyncio.create_task(watcher.run()))
        tasks.append(asyncio.create_task(snapshot_publisher(watcher)))

    async def _on_clean(_a):
        for t in tasks:
            t.cancel()

    wyc_app.on_startup.append(_on_start)
    wyc_app.on_cleanup.append(_on_clean)
    try:
        app.add_subapp("/wyc/", wyc_app)
    except Exception as e:
        log.warning("watchyourclankers add_subapp('/wyc/') failed: %s", e)
        return False
    log.info("watchyourclankers mounted at /wyc/ (Spectate) — clanker auth covers it")
    return True
