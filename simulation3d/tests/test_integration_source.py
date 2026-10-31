"""The 3D simulator runs the repository's own ATCS controllers, and the copied 3dATCS code is the validated code.

(The standalone 3dATCS checks its private copy of the controllers against checksums. Here there is no copy: the worker
imports `simulation/` of this repository, so these tests check exactly that.)
"""
import hashlib
import os
import subprocess
import sys

from atcs3d import paths

SUMS = os.path.join(paths.ROOT, "SHA256SUMS")
CONTROLLER_FILES = ("controllers/__init__.py", "controllers/fixed.py", "controllers/queue_based.py", "controllers/wait_time.py",
                    "runner.py", "metrics.py", "intersection_final.net.xml", "baseline.sumocfg")


def test_the_copied_3datcs_files_are_unchanged():
    """Everything listed in SHA256SUMS was copied byte for byte from 3dATCS (see PROVENANCE.md).

    A deliberate change to one of these files: edit it, then regenerate the list with
    `cd simulation3d && sha256sum <file> ...` and update PROVENANCE.md.
    """
    changed = []
    with open(SUMS) as f:
        for line in f:
            digest, name = line.split(None, 1)
            with open(os.path.join(paths.ROOT, name.strip()), "rb") as g:
                if hashlib.sha256(g.read()).hexdigest() != digest:
                    changed.append(name.strip())
    assert changed == []


def test_the_signal_logic_is_the_repositorys_simulation_directory():
    assert paths.ATCS_SOURCE == os.path.join(paths.REPO_ROOT, "simulation")
    for name in CONTROLLER_FILES:
        assert os.path.isfile(os.path.join(paths.ATCS_SOURCE, name)), name


def test_there_is_no_second_copy_of_the_controllers_in_this_component():
    found = []
    for root, dirs, files in os.walk(paths.ROOT):
        dirs[:] = [d for d in dirs if d not in ("output", "__pycache__", ".pytest_cache")]
        found += [os.path.join(root, f) for f in files if f in ("fixed.py", "queue_based.py", "wait_time.py", "runner.py", "metrics.py")]
    assert found == []


def test_the_worker_imports_its_controllers_and_metrics_from_simulation():
    code = ("from atcs3d import paths; paths.use_atcs_source(); import controllers, runner, metrics, os;"
            "print(os.path.dirname(controllers.__file__)); print(runner.__file__); print(metrics.__file__);"
            "print(sorted(controllers.CONTROLLERS))")
    out = subprocess.run([sys.executable, "-B", "-c", code], cwd=paths.ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    assert out[0] == os.path.join(paths.ATCS_SOURCE, "controllers")
    assert out[1] == os.path.join(paths.ATCS_SOURCE, "runner.py")
    assert out[2] == os.path.join(paths.ATCS_SOURCE, "metrics.py")
    assert out[3] == "['fixed', 'queue_based', 'wait_time']"


def test_running_the_3d_simulator_writes_nothing_into_simulation():
    """SUMO output and the demand file go to simulation3d/output; simulation/ is only read."""
    assert not (paths.OUTPUT_DIR + os.sep).startswith(paths.ATCS_SOURCE + os.sep)
    assert os.path.commonpath([paths.OUTPUT_DIR, paths.ROOT]) == paths.ROOT
