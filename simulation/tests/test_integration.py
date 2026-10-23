import shutil

import pytest

import metrics
import runner

pytestmark = pytest.mark.skipif(shutil.which("sumo") is None, reason="SUMO is not installed")


def simulate(controller, ew, ns):
    tripinfo, _ = runner.run(controller, ew=ew, ns=ns, tag=f"pytest_{controller}")
    return metrics.summarize(tripinfo)


@pytest.fixture(scope="module")
def fixed_result():
    return simulate("fixed", 500, 200)


@pytest.fixture(scope="module")
def queue_result():
    return simulate("queue_based", 500, 200)


def test_all_vehicles_complete(fixed_result, queue_result):
    assert fixed_result["vehicles_completed"] == 1400
    assert queue_result["vehicles_completed"] == 1400


def test_fixed_baseline_matches_reference(fixed_result):
    assert 12.5 < fixed_result["avg_wait_s"] < 14.5


def test_queue_based_cuts_average_wait(fixed_result, queue_result):
    assert queue_result["avg_wait_s"] < 0.6 * fixed_result["avg_wait_s"]


def test_queue_based_cuts_time_loss(fixed_result, queue_result):
    assert queue_result["avg_time_loss_s"] < fixed_result["avg_time_loss_s"]


def test_queue_based_worst_case_wait_not_worse(fixed_result, queue_result):
    assert queue_result["max_wait_s"] <= fixed_result["max_wait_s"]


def test_queue_based_generalizes_to_unseen_traffic():
    fixed = simulate("fixed", 200, 600)
    adaptive = simulate("queue_based", 200, 600)
    assert adaptive["avg_wait_s"] < fixed["avg_wait_s"]
