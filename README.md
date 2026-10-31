# ATCS - Adaptive Traffic Control System

Holberton capstone project. An adaptive traffic light controller tested in the SUMO simulator and compared against a fixed-timer baseline.

## Results

Fixed timer vs. queue-based adaptive controller (minimum green 20 s, switch margin 2 cars). One simulated hour per scenario, times in seconds. The held-out scenario was not used when tuning.

| Scenario (EW / NS veh/h) | Avg wait fixed | Avg wait adaptive | Time loss fixed | Time loss adaptive | Max wait fixed | Max wait adaptive |
|---|---|---|---|---|---|---|
| uneven (500 / 200) | 13.39 | 6.07 | 23.35 | 15.11 | 46 | 25 |
| rush_hour (900 / 700) | 18.06 | 18.26 | 32.77 | 36.93 | 59 | 52 |
| balanced (300 / 300) | 13.37 | 6.21 | 22.42 | 14.78 | 46 | 21 |
| held_out (200 / 600) | 14.80 | 6.01 | 25.66 | 15.59 | 46 | 25 |

The adaptive controller wins in uneven, balanced and held-out traffic. In saturated rush-hour traffic the fixed timer has slightly lower average wait and time loss (the adaptive controller has the lower worst-case wait), because both roads are busy and there is little room to improve. The parameter sweep is in results/queue_based_tuning.txt.

## Structure

- simulation/ - SUMO network, traffic scenarios, controllers, metrics
- backend/ - Flask API and database
- frontend/ - scenario configuration page and results dashboard
- results/ - shared results data
- monitor/ - ATCS Monitor camera detection and counting app, imported from its own repository with its own environment (see monitor/UPSTREAM.md)
- simulation3d/ - service behind the 3D Simulator page: a real SUMO simulation with the ATCS controllers, drawn in 3D (see simulation3d/README.md)

## Wait-time controller

A second adaptive controller (`simulation/controllers/wait_time.py`) gives green to the direction whose vehicles have accumulated more waiting time, with a 20 s minimum green, a 45 s maximum green (only enforced when the other direction has someone waiting) and a switch margin of 0 s. The values come from a sweep (`simulation/tune_wait_time.py`) that minimises mean time loss over the uneven, rush and balanced scenarios while never letting the worst-case wait exceed the fixed timer's; the held-out scenario is only used afterwards to validate.

| | Fixed | Queue-based | Wait-time |
|---|---|---|---|
| Mean time loss, training scenarios (s) | 26.18 | 22.27 | 22.34 |
| Time loss, held-out 200 / 600 (s) | 25.66 | 15.59 | 16.27 |
| Max wait, held-out (s) | 46 | 25 | 25 |

The full sweep is in results/wait_time_tuning.txt.

## Team

- Ian Aviles: queue-based controller, simulation runner, Flask API and SQLAlchemy models, scenario configuration page, results generator, tests
- Alberto Frias: wait-time controller and its tuning sweep, comparison and CSV export endpoints, JWT login, comparison dashboard, tests

## Setup

Requires Python 3.10+, SUMO 1.18 or newer and the Python `traci` library (installed with SUMO, or `pip install traci`).

    cd backend
    python3 -m venv --system-site-packages venv
    venv/bin/pip install -r requirements-dev.txt

`--system-site-packages` lets the venv see a `traci` installed with the system SUMO package.

## Running the simulation

    cd simulation
    python3 runner.py fixed
    python3 runner.py queue_based
    python3 runner.py wait_time
    python3 metrics.py output/tripinfo_queue_based.xml

`--ew` and `--ns` set vehicles per hour, `--gui` opens sumo-gui.

## Running the web app

The app will not start without a JWT signing secret. Pick one of:

Local development (quickest). A random secret is generated each time the server starts, so you have to sign in again after a restart or code reload:

    cd backend
    ATCS_DEV=1 venv/bin/python run.py

Stable secret (shared or deployed setups, or to stay signed in across restarts). Use a random value of at least 32 bytes and keep it out of the repository:

    cd backend
    export JWT_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    venv/bin/python run.py

A missing, short or previously published secret stops startup with an error that says what to do. `ATCS_DEV=1` never overrides a weak `JWT_SECRET_KEY`; never set `ATCS_DEV=1` outside local development.

Then open http://127.0.0.1:5000/scenarios.html to create scenarios and run controllers, and http://127.0.0.1:5000/dashboard.html to compare them. The dashboard needs an account: use "Create account" on its sign-in form, or `POST /api/auth/register`. Tokens expire after one hour. The dashboard stores its authentication token in sessionStorage for the current browser tab, preserving the session across page reloads and navigation. The session is cleared when the user signs out, the token expires, or the tab's session ends.

The SQLite database is `backend/atcs.db`; tables are created on startup, existing data is kept.

## Running the 3D Simulator

The **3D Simulator** tab shows a real SUMO simulation of the intersection in 3D, steered by the Fixed, Queue-Based and Wait-Time controllers of `simulation/`. Unlike the Simulator and Compare pages (a simplified model in the browser), it needs one more local program next to the web app, which the web app starts and stops by itself. Install its one extra package once (`aiohttp`, into the same virtual environment as the web app):

    backend/venv/bin/pip install -r simulation3d/requirements.txt

Then start ATCS as usual (above) and click **3D Simulator**, or open http://localhost:5000/simulator3d.html. The page asks the web app for the service, which starts it (a second or two; nothing is started before the tab is opened) and stops it when the web app exits. It listens on 127.0.0.1:8766 (the Monitor service uses 8765); if another program already has that port, a free one is used. If it cannot be started, the page says why and offers **Try again**. `ATCS3D_AUTOSTART=0` switches this off, and `simulation3d/run.sh` starts the service by hand, as before. The rest of the site does not depend on it. Demo Day steps, ports, troubleshooting and what was copied from the standalone 3dATCS project: [simulation3d/README.md](simulation3d/README.md).

## API

Public (no token):

| Method | Path | Notes |
|---|---|---|
| GET | /api/health | |
| GET | /api/controllers | allowed controller names |
| GET, POST | /api/scenarios | list / create |
| GET | /api/scenarios/<id> | |
| GET, POST | /api/runs | list (filters `scenario_id`, `controller`) / run a simulation |
| GET | /api/runs/<id> | |
| GET | /api/runs/<id>/timeseries | halting vehicles over time from the run's SUMO summary file, about 200 points; 404 if the file is not on this machine |
| POST | /api/auth/register | `{"username", "password"}`: 201, 400 invalid, 409 taken |
| POST | /api/auth/login | `{"username", "password"}`: 200 with `access_token`, 401 invalid credentials |

Need `Authorization: Bearer <access_token>` (401 otherwise):

| Method | Path | Notes |
|---|---|---|
| GET | /api/compare?scenario_id=N | latest completed run per controller and `improvement_vs_fixed_pct` per metric, `(fixed - controller) / fixed * 100` rounded to 0.1. Null when there is no completed fixed run or the fixed value is 0. 400 bad id, 404 unknown scenario |
| GET | /api/runs/export.csv | all runs as CSV, optional `scenario_id` and `controller` filters, 400 on invalid filters |

Monitor endpoints (`/api/monitor/status`, `sources`, `sessions`, `frame.jpg`, `start`, `stop`, `recording/start`, `recording/stop`) also need a token, and additionally a viewer (read only) or operator (control) grant given on the server with `backend/monitor_access.py`; everybody else gets 403, and registering an account grants nothing. They proxy to the local monitor service and answer 503 while it is not configured or not running. Setup, environment variables and the full behaviour are in [monitor/docs/web_service.md](monitor/docs/web_service.md).

Desktop telemetry: a desktop ATCS Monitor registered with `venv/bin/python monitor_access.py device add <name>` sends its live status and finished sessions to `POST /api/desktop/telemetry/status`, `.../sessions/<id>/events` and `.../sessions/<id>/summary` with its device token (accepted from loopback addresses only; browser JWTs are refused there). Signed-in viewers and operators read them with `GET /api/monitor/desktop/status`, `/api/monitor/desktop/sessions` and `/api/monitor/desktop/sessions/<id>`. Device setup, credential handling, document formats and Windows-to-WSL2 connectivity are in [monitor/docs/desktop_telemetry.md](monitor/docs/desktop_telemetry.md).

The desktop side (Phase 5b) is the desktop link: the original desktop app opened through `atcs-monitor://open` sends its status and finished sessions with a durable local outbox. Setup on Windows, the launcher and its security limits are in [monitor/docs/desktop_link.md](monitor/docs/desktop_link.md).

The Monitor page (Phase 5c) is desktop-first: **Open Desktop Monitor** opens the desktop app on the computer running the browser (only on a click), and the page shows the connection, live counts and the stored desktop sessions from the three read-only endpoints above. The Phase 4 server-side monitor is kept as a collapsed "Server-side Monitor — Legacy" panel whose controls appear only while that service answers. Behaviour and manual checks: [monitor/docs/desktop_link.md, section 9](monitor/docs/desktop_link.md#9-web-monitor-page-phase-5c).

## Tests

    cd backend && venv/bin/pytest
    cd simulation && ../backend/venv/bin/python -m pytest tests
    cd monitor && .venv/bin/python -m pytest
    cd frontend/tests && npm install && npm test
    simulation3d/run.sh test -m "not slow"

The backend tests use an in-memory database and a test-only JWT secret, need no environment variables and never start SUMO.

The frontend tests drive the Scenarios, Dashboard and Monitor pages in headless Chromium (Playwright) against a local mock of the API; the desktop payloads they use are real backend output, checked by `backend/tests/test_desktop_dashboard_contract.py`.

The 3D Simulator tests use real SUMO and a real browser (see simulation3d/README.md); the tab and page checks are in `backend/tests/test_simulator3d.py` and need no SUMO.

The simulation tests include a few real SUMO runs that check the adaptive controller against the fixed timer, and skip themselves if SUMO is not installed.

## Known limitations

- Runs execute synchronously inside the POST /api/runs request (a few seconds per simulated hour); there is no job queue.
- SUMO output files are named by run id in simulation/output/. If backend/atcs.db is deleted, new runs reuse ids and overwrite older files.
- Timeseries data is only available on the machine that ran the simulation.
- Chart.js 4.5.1 is vendored in frontend/js/vendor/ (MIT licence alongside it), so the dashboard works offline.
