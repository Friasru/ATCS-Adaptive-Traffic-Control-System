# 3D Simulator

The **3D Simulator** tab of the ATCS website. It shows a real SUMO simulation of the intersection, steered by the real ATCS
controllers (Fixed, Queue-Based, Wait-Time), drawn in 3D with three.js. It is separate from the Simulator and Compare
pages, which run a simplified model written for the browser.

```
simulation/controllers ◄── TraCI ──► SUMO ── JSON lines ──► state bridge ── WebSocket ──► three.js renderer
 (the ATCS controllers)            (authoritative)          (atcs3d/)                       (web/)
                                                                │ serves
Flask website  ─ frontend/simulator3d.html ─ shows it in a frame ┘ http://localhost:8766/
   └─ starts it when the page asks, and stops it when the website exits
```

Information flows one way. SUMO moves every car and the ATCS controller changes every light; the page only draws what SUMO
reports and can send back run-control commands (select, start, pause, resume, stop, restart, speed, benchmark). It has no
command that touches a signal, a vehicle or a controller, and the server rejects unknown commands. `atcs3d/` is the 3dATCS
state bridge and `web/` its renderer, copied from the standalone 3dATCS project (see [PROVENANCE.md](PROVENANCE.md)).

**The controllers are not copied.** `atcs3d/paths.py` points at this repository's own `simulation/` directory (controllers,
runner, metrics, SUMO network), so there is one copy of the signal logic. Each simulation runs the ATCS loop in the ATCS
order, `traci.simulationStep()` then `controller.step(time)`, with a read-only snapshot taken in between. Nothing is ever
written into `simulation/`: SUMO output goes to `simulation3d/output/` (git-ignored).

## Why a second program

The website is Flask, which cannot hold the live WebSocket stream, and the 3D service is an asyncio server that starts one
SUMO process per simulation. So the 3D service runs next to the website, like the Monitor service does, and the page shows
it in a frame. In the frame the WebSocket belongs to the service's own origin, which keeps the service's own protection
(it refuses WebSocket connections from any other web page) in place. The website starts and stops the service (below); it
needs no new login rule. If the service cannot run, the page says why and the rest of ATCS works as before.

## Setup (once)

SUMO 1.18 or newer with its Python tools (`traci`, `sumolib`), as the rest of ATCS already needs. One extra Python package,
`aiohttp`, installed into the same virtual environment as the website (nothing system-wide):

    backend/venv/bin/pip install -r simulation3d/requirements.txt        # from the repository folder

Use the environment you run the website with (`backend/venv` in the main README). `simulation3d/run.sh` looks for its
Python in this order: `$ATCS3D_PYTHON`, the repository's `.venv/bin/python`, `backend/venv/bin/python`, then `python3`.

## Run it: one terminal

    cd backend && ATCS_DEV=1 venv/bin/python run.py        # the website, as before (see the main README)

Open http://localhost:5000/ and click **3D Simulator**. That is all: the page asks the website for the service
(`POST /api/simulator3d/start`), the website starts it, and the page loads it as soon as it answers, usually within a second.
Ctrl+C on the website stops the service with it.

**When it starts.** On demand, when the 3D Simulator page is opened, not when the website starts: no other page, and no test,
starts it, and nothing runs for people who never open the tab. (The first click therefore waits a moment; open the tab once
before presenting if even that matters.) Asking again while it is starting or running changes nothing, so several tabs, the
page's own polling and the Flask reloader's two processes never produce two services.

**What the website guarantees.**

* *It is the right service.* The website gives the process it starts a random instance id and only calls it ready when
  `GET /api/identity` returns that id: a port that merely accepts connections, or another program that answers `/api/health`,
  does not count.
* *It never takes a port.* If 8766 is used by another program, the service starts on a free port and the page is told which.
  If a 3D service that someone started with `run.sh` is already there, the page uses it and the website leaves it running;
  the website only ever signals the process it started itself (by process id and group, never by port or name).
* *Nothing is left behind.* On Ctrl+C, a normal exit, `kill`, or a reload of the development server, the website sends the
  service SIGTERM; the service closes its WebSockets, then every simulation worker and its SUMO, and exits (a few
  milliseconds when idle; it is killed after 10 s if it does not). If the website itself is killed (`kill -9`), the service
  notices that the pipe the website holds to its stdin has closed and does the same. After a reload of the development
  server the service is started again by the page's next request, so a reload during a running simulation ends it.
* *Failures are explained.* The page shows the reason (SUMO or `aiohttp` missing, the service stopped while starting, no
  answer within 20 s) with the last lines the service printed, and a **Try again** button. A failed start is not retried by
  itself. The service's output is in `simulation3d/output/service.log`.

Settings (environment variables of the website): `ATCS3D_AUTOSTART=0` turns automatic start off (then run `simulation3d/run.sh`
in a second terminal, as before), `ATCS3D_PORT` is the preferred port (default 8766), `ATCS3D_PYTHON` the Python that runs
the service (default: the website's own). It needs Linux or WSL; on native Windows run ATCS inside WSL.

To run the service by hand (a second terminal), or on another port:

    simulation3d/run.sh                      # then it is used as it is; the website neither starts nor stops it
    simulation3d/run.sh --port 9001          # then open simulator3d.html?port=9001 (a page with ?port only looks, it never asks the website)

| What | Where | Notes |
|---|---|---|
| ATCS website (Flask) | `127.0.0.1:5000` | unchanged |
| 3D service | `127.0.0.1:8766` | this computer only. Not 8765: the Monitor service uses that |
| Another port | `ATCS3D_PORT=9001` for the website, or `simulation3d/run.sh --port 9001` by hand | the page is told the port by the website; for a hand-started one open `simulator3d.html?port=9001` |

The service listens on 127.0.0.1 only and is meant to be used from the same computer. With ATCS in WSL and the browser in
Windows, WSL's localhost forwarding makes `http://localhost:5000` and `http://localhost:8766` reachable from Windows Chrome or
Edge. Open the website through `localhost` (or `127.0.0.1`) on that computer: the page looks for the service on the same host
name it was opened with.

## Demo Day

Everything shown is a real SUMO simulation; be ready to say so, because it is the point of the 3D page. Rehearse once on the
presentation computer. Use Chrome or Edge with hardware acceleration on, and press **F** inside the 3D view for full screen
(or **Open in its own tab** on the page, then F).

1. Open the website, click **3D Simulator**. The status on the page says *Service running*; the 3D view says *Ready*.
2. Pick a scenario (default *Uneven traffic*, east-west heavy: the clearest difference) and **Fixed timing**.
3. Set a speed (10×) and press **Start** (or Space). Traffic builds up on the red side while the other way may be empty.
4. Press **2** (Queue-Based) or **3** (Wait-Time): a fresh run of the same traffic starts. Watch the signal timing change.
5. Press **B** for side by side (Fixed on the left, ATCS on the right, identical traffic, stepped in lockstep).
6. Press **P** for the measured results.

| Key | Action | Key | Action |
|---|---|---|---|
| Space | start / pause / resume | O / T / D | camera overview / top / driver |
| R | restart the same experiment | H | hide the interface (clean projector view) |
| X | stop | P | measured results |
| 1 / 2 / 3 | Fixed / Queue-Based / Wait-Time (fresh run) | F | full screen |
| B | side by side, Fixed vs ATCS | ? | shortcut list |

Mouse: drag = orbit, right-drag = pan, wheel = zoom. Speeds 1× to 50×; at 50× a full simulated hour plays in about 75 seconds
and the results open by themselves when it ends.

**Where the numbers come from.** The results panel shows the stored benchmark (`results/benchmark_*.json`: a full simulated
hour per controller, headless SUMO, measured with the ATCS `metrics.summarize`, deterministic) and, when a live run finishes,
that run's own numbers. They are simulation results, not real-world measurements. The stored Fixed and Queue-Based figures
equal the ones this repository publishes in `results/results.json` (checked by `tests/test_results.py`); `simulation3d/run.sh
benchmark --all` regenerates them in about 10 seconds per scenario.

**If the screen or the computer fails.** The standalone 3dATCS project keeps the offline backup: `./run.sh record` there writes
`output/backup_demo.webm` (a recorded demo of the real page) and `docs/images/` holds screenshots. Those stay in the 3dATCS
repository, unchanged. Nothing here needs the internet: three.js is vendored in `web/vendor/`.

## If something goes wrong

| Problem | Fix |
|---|---|
| The page says *The 3D simulation service is not running* | Read the reason on the page and press **Try again**; `simulation3d/output/service.log` has what the service printed; `curl localhost:5000/api/simulator3d/status` shows the website's view. Or start it by hand with `simulation3d/run.sh`; the page connects by itself. Check: `curl localhost:8766/api/health` prints `{"ok": true}` |
| *Address already in use* (starting `run.sh` by hand) | The website's own service, or another program, has the port: use it, or `simulation3d/run.sh --port 9001` and open `simulator3d.html?port=9001`. The website itself never fails on this: it picks a free port |
| A run looks odd | Press **R** in the 3D view |
| The tab froze or was closed | Reload: the view rebuilds itself and a running simulation resumes (with nobody watching it pauses, and after 2 minutes it stops) |
| Slow or stuttering 3D | Open `simulator3d.html?shadows=0&dpr=1` |
| *Starting SUMO…* never ends | Press **X**, then Space; if it repeats, Ctrl+C the service and run it again |
| Stray processes | `pgrep -af "atcs3d\|sumo -c"` should print nothing once the website (and with it the service) has stopped; else `pkill -f atcs3d.sim_worker` and `pkill -f atcs3d.managed` |
| The results table is empty | `simulation3d/run.sh benchmark --all`, then press **P** |
| Nothing loads from Windows | In WSL check the `curl` above; then try `127.0.0.1` instead of `localhost`, or `wsl --shutdown` from Windows |

A simulation never outlives the service: stopping or restarting a run, and the service exiting, kill the worker and its SUMO
together (each worker has its own process group, and exits by itself if the service dies).

## Tests

    simulation3d/run.sh test -m "not slow"      # about 5 minutes; without -m, also the full-hour runs (about 3 more)
    cd backend && venv/bin/pytest tests/test_simulator3d.py tests/test_simulator3d_service.py     # the tab, the page, automatic start and stop; needs no SUMO

The component tests use real SUMO, a real server and, for the browser ones, Chromium through Playwright
(`pip install -r simulation3d/requirements-dev.txt`; the browser is taken from `~/.cache/ms-playwright`, or from
`npx playwright install chromium`).

| File | Covers |
|---|---|
| `test_dashboard_integration.py` | The Flask-served site end to end: the tab after Compare on every page, the page with and without the service, the 3D view inside the frame driving real SUMO (start, pause, resume, restart, the three controllers, side by side, stop), the service restarting under the page, no SUMO left behind |
| `test_server.py`, `test_bridge.py`, `test_counters.py`, `test_parity.py` | The bridge and server against real SUMO: frames equal an independent SUMO recording, determinism, signal timing rules of each controller, pause/restart/stop, no orphan processes, results equal the ATCS runner's |
| `test_web.py`, `test_counters_web.py` | The renderer in Chromium: it draws exactly what SUMO reports, signals follow SUMO's state, controls work |
| `test_autostart.py` | The service as the website runs it (identity, SIGTERM and a closed pipe end it and every SUMO), the website's manager running all three controllers and side by side, and the page getting the service started by itself, explaining a failed start and retrying |
| `test_results.py` | The stored benchmark equals the figures this repository publishes |
| `test_integration_source.py` | The copied code is unchanged, and the controllers are this repository's `simulation/` |
| `test_zz_simulation_untouched.py` | Runs last: the test run added, removed and changed nothing in `simulation/` |

## Third-party

three.js 0.170.0 (MIT) is vendored unmodified in `web/vendor/three/`; its licence is `web/vendor/three/LICENSE`. aiohttp
(Apache-2.0) is installed into the virtual environment. SUMO (EPL-2.0) runs as a separate program.
