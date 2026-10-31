"""HTTP + WebSocket server: serves the 3D page and streams SUMO state to it.

    python -m atcs3d.server                 # http://127.0.0.1:8765
    python -m atcs3d.server --port 9000 --host 0.0.0.0     # reachable from other machines on the LAN (projector PC)

Binding to 127.0.0.1 keeps the demo private; WSL2 forwards localhost to the Windows browser automatically.
"""
import argparse
import json
import logging
import os
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from . import paths
from .session import Hub

HUB_KEY = web.AppKey("hub", Hub)


@web.middleware
async def no_store(request, handler):
    resp = await handler(request)
    resp.headers["Cache-Control"] = "no-store"  # always serve the current files while developing / rehearsing
    return resp


async def index(request):
    return web.FileResponse(os.path.join(paths.WEB_DIR, "index.html"))


async def health(request):
    return web.json_response({"ok": True})


async def perf(request):
    hub = request.app[HUB_KEY]
    return web.json_response(hub.perf())


async def ws_handler(request):
    hub = request.app[HUB_KEY]
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:
        # Another web page in the same browser must not be able to drive the local simulator.
        raise web.HTTPForbidden(text="cross-origin WebSocket refused")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
    await ws.prepare(request)
    try:
        await hub.attach(ws)  # hello + full current picture (state, last frame, results), then live broadcasts in order
        await hub.client_joined()
        async for m in ws:
            if m.type == WSMsgType.TEXT:
                try:
                    msg = json.loads(m.data)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await hub.handle(msg)
            elif m.type == WSMsgType.ERROR:
                break
    finally:
        hub.detach(ws)
        await hub.client_left()
    return ws


async def on_cleanup(app):
    await app[HUB_KEY].shutdown()  # takes every worker and its sumo down with the server


def make_app():
    app = web.Application(middlewares=[no_store])
    app[HUB_KEY] = Hub()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/perf", perf)
    app.router.add_static("/", paths.WEB_DIR, show_index=False)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    ap = argparse.ArgumentParser(description="3dATCS server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    print(f"3dATCS: open http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}/", flush=True)
    web.run_app(make_app(), host=args.host, port=args.port, print=None, handle_signals=True, access_log=None)


if __name__ == "__main__":
    main()
