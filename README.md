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

## Team

- Ian Aviles: queue-based controller, simulation runner, Flask API and SQLAlchemy models, scenario configuration page, tests
- Alberto Frias: max-pressure controller, results and comparison endpoints, JWT login, results dashboard, tests

## Running the simulation

Requires SUMO 1.18 or newer and the Python traci library.

    cd simulation
    python3 runner.py fixed
    python3 runner.py queue_based
    python3 metrics.py output/tripinfo_queue_based.xml
