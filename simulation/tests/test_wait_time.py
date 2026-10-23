import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from controllers import wait_time  # noqa: E402

PHASES = [
    SimpleNamespace(state="GGrr"),
    SimpleNamespace(state="yyrr"),
    SimpleNamespace(state="rrGG"),
    SimpleNamespace(state="rryy"),
]
LANES = ["a0", "a1", "b0", "b1"]
WAITS = {}


def make(phase=0):
    traci = mock.MagicMock()
    traci.trafficlight.getControlledLanes.return_value = LANES
    traci.trafficlight.getAllProgramLogics.return_value = [SimpleNamespace(phases=PHASES)]
    traci.trafficlight.getPhase.return_value = phase
    traci.lane.getWaitingTime.side_effect = lambda lane: WAITS.get(lane, 0)
    return traci


class WaitTimeTest(unittest.TestCase):
    def controller(self, phase=0):
        traci = make(phase)
        patcher = mock.patch("controllers.wait_time.traci", traci)
        patcher.start()
        self.addCleanup(patcher.stop)
        from controllers.wait_time import WaitTimeController
        WAITS.clear()
        return WaitTimeController("A0"), traci

    def test_lane_selection(self):
        c, _ = self.controller()
        self.assertEqual(c.green, {0: {"a0", "a1"}, 2: {"b0", "b1"}})

    def test_wait_aggregation(self):
        c, _ = self.controller()
        WAITS.update(a0=3, a1=4)
        self.assertEqual(c.wait(c.green[0]), 7)

    def test_min_green(self):
        c, t = self.controller()
        WAITS.update(b0=50)
        c.step(0)
        c.step(wait_time.MIN_GREEN - 1)
        t.trafficlight.setPhase.assert_not_called()

    def test_higher_wait_switches_to_yellow(self):
        c, t = self.controller()
        WAITS.update(b0=50, a0=1)
        c.step(0)
        c.step(wait_time.MIN_GREEN)
        t.trafficlight.setPhase.assert_called_once_with("A0", 1)

    def test_tie_keeps_green(self):
        c, t = self.controller()
        WAITS.update(a0=5, b0=5)
        c.step(0)
        c.step(30)
        t.trafficlight.setPhase.assert_not_called()

    def test_empty_traffic_no_switch(self):
        c, t = self.controller()
        c.step(0)
        c.step(100)
        t.trafficlight.setPhase.assert_not_called()

    def test_max_green_fairness(self):
        c, _ = self.controller()
        self.assertTrue(c.should_switch(wait_time.MAX_GREEN, 100, 1))
        self.assertFalse(c.should_switch(wait_time.MAX_GREEN, 100, 0))
        self.assertFalse(c.should_switch(wait_time.MAX_GREEN - 1, 100, 1))

    def test_switch_margin(self):
        c, _ = self.controller()
        elapsed = wait_time.MIN_GREEN
        with mock.patch.object(wait_time, "SWITCH_MARGIN", 25):
            self.assertFalse(c.should_switch(elapsed, 10, 35))
            self.assertTrue(c.should_switch(elapsed, 10, 36))
        with mock.patch.object(wait_time, "SWITCH_MARGIN", 0):
            self.assertTrue(c.should_switch(elapsed, 10, 11))

    def test_no_switch_during_yellow(self):
        c, t = self.controller(phase=1)
        WAITS.update(b0=50)
        c.step(0)
        c.step(100)
        t.trafficlight.setPhase.assert_not_called()

    def test_wraps_around(self):
        c, t = self.controller(phase=2)
        WAITS.update(a0=50)
        c.step(0)
        c.step(wait_time.MIN_GREEN)
        t.trafficlight.setPhase.assert_called_once_with("A0", 3)


class RegistryTest(unittest.TestCase):
    def test_registered(self):
        from controllers import CONTROLLERS
        self.assertIn("wait_time", CONTROLLERS)
        self.assertEqual({"fixed", "queue_based"} - set(CONTROLLERS), set())


if __name__ == "__main__":
    unittest.main()
