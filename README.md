# ATCS - Adaptive Traffic Control System

Holberton capstone project. An adaptive traffic light controller tested in the SUMO simulator and compared against a fixed-timer baseline.

## Results so far

Uneven traffic scenario, one simulated hour, 1400 vehicles.

| Metric | Fixed timer | Adaptive |
|---|---|---|
| Avg wait (s) | 13.39 | 4.02 |
| Max wait (s) | 46.00 | 16.00 |
| Avg time loss (s) | 23.35 | 13.07 |
| Avg trip duration (s) | 52.45 | 42.16 |

## Structure

- simulation/ - SUMO network, traffic scenarios, controllers, metrics
- backend/ - Flask API and database
- frontend/ - scenario configuration page and results dashboard
- results/ - shared results data

## Team

- Ian Aviles: queue-based controller, simulation runner, Flask API and SQLAlchemy models, scenario configuration page, tests
- Alberto Frias: max-pressure controller, results and comparison endpoints, JWT login, results dashboard, tests

## Running the simulation

Requires SUMO 1.18 or newer and the Python traci library.

    cd simulation
    python3 runner.py fixed
    python3 runner.py queue_based
    python3 metrics.py output/tripinfo_queue_based.xml
