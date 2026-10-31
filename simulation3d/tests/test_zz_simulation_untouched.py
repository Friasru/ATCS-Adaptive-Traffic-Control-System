"""Running the 3D simulator and its tests only reads the repository's `simulation/` directory.

The file name makes it run last. The snapshot is taken when pytest collects the tests, before any of them has run, so
this fails if anything in the run (the service, a worker, a SUMO started from `baseline.sumocfg`, a test helper) wrote a
file into `simulation/` or changed one. `simulation/output/` is the web app's own output folder and bytecode caches are
not source; both are left out.
"""
import os

from atcs3d import paths


def snapshot():
    found = {}
    for root, dirs, files in os.walk(paths.ATCS_SOURCE):
        dirs[:] = [d for d in dirs if d not in ("output", "__pycache__")]
        for name in files:
            full = os.path.join(root, name)
            st = os.stat(full)
            found[os.path.relpath(full, paths.ATCS_SOURCE)] = (st.st_size, st.st_mtime_ns)
    return found


BEFORE = snapshot()


def test_the_run_left_the_simulation_directory_as_it_found_it():
    after = snapshot()
    assert sorted(set(after) - set(BEFORE)) == [], "files were written into simulation/"
    assert sorted(set(BEFORE) - set(after)) == [], "files were removed from simulation/"
    assert sorted(k for k in BEFORE if BEFORE[k] != after[k]) == [], "files in simulation/ were changed"
