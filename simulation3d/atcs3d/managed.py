"""The 3D service as the ATCS backend runs it: `python -m atcs3d.managed --port N`.

It is the same server as `atcs3d.server` (same app, same routes, same protection against other web pages), with two
additions that only make sense for a child process that a parent owns:

  * `GET /api/identity` answers `{"service": "atcs3d", "instance": <ATCS3D_INSTANCE>, "pid": ...}`. The parent gives the
    child a random instance id when it starts it, so it can tell its own child from any other program on the port.
  * The service exits, taking every simulation worker and its SUMO with it, when its stdin is closed. The parent keeps
    the other end of that pipe open for as long as it lives, so if the parent dies in any way (even `kill -9`) the
    service does not outlive it. Do not run this module from a terminal; use `simulation3d/run.sh` for that.
"""
import argparse
import logging
import os
import signal
import threading

from aiohttp import WSCloseCode, web

from . import server

INSTANCE_ENV = "ATCS3D_INSTANCE"


def make_app(instance):
    @web.middleware
    async def identity(request, handler):
        if request.path == "/api/identity":
            return web.json_response({"service": "atcs3d", "instance": instance, "pid": os.getpid()},
                                     headers={"Cache-Control": "no-store"})
        return await handler(request)

    async def close_clients(app):
        # Without this a shutdown waits for the browsers' open WebSockets (up to the shutdown timeout) before it
        # reaches the cleanup that ends the simulations.
        for ws in list(app[server.HUB_KEY].clients):
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"service stopping")

    app = server.make_app()
    app.middlewares.insert(0, identity)
    app.on_shutdown.append(close_clients)
    return app


def _stop_when_owner_is_gone():
    try:
        while os.read(0, 4096):
            pass
    except OSError:
        pass
    os.kill(os.getpid(), signal.SIGTERM)  # the same graceful path as `kill`: workers and SUMO are taken down first


def main():
    ap = argparse.ArgumentParser(description="3dATCS service, run by the ATCS backend")
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    threading.Thread(target=_stop_when_owner_is_gone, daemon=True).start()
    try:
        web.run_app(make_app(os.environ.get(INSTANCE_ENV, "")), host="127.0.0.1", port=args.port, print=None,
                    handle_signals=True, access_log=None, shutdown_timeout=3)
    except OSError as exc:
        raise SystemExit(f"3D service cannot listen on 127.0.0.1:{args.port}: {exc.strerror or exc}")


if __name__ == "__main__":
    main()
