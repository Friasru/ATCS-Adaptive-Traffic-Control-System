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

    cd backend
    export JWT_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    venv/bin/python run.py

Then open http://127.0.0.1:5000/scenarios.html to create scenarios and run controllers, and http://127.0.0.1:5000/dashboard.html to compare them. The dashboard needs an account: use "Create account" on its sign-in form, or `POST /api/auth/register`.

`JWT_SECRET_KEY` signs login tokens. Without it the app falls back to a fixed development-only key and logs a warning; anyone who knows that key can forge tokens, so always set a long random value outside local development. Tokens expire after one hour. The dashboard keeps its token in memory only, so reloading the page signs you out.

The SQLite database is `backend/atcs.db`; tables are created on startup, existing data is kept.

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

## Tests

    cd backend && venv/bin/pytest
    cd simulation && ../backend/venv/bin/python -m pytest tests

The backend tests use an in-memory database and never start SUMO.

## Known limitations

- Runs execute synchronously inside the POST /api/runs request (a few seconds per simulated hour); there is no job queue.
- SUMO output files are named by run id in simulation/output/. If backend/atcs.db is deleted, new runs reuse ids and overwrite older files.
- Timeseries data is only available on the machine that ran the simulation.
- Chart.js 4.5.1 is vendored in frontend/js/vendor/ (MIT licence alongside it), so the dashboard works offline.
