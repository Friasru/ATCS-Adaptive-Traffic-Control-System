import sys
import unittest
from unittest import mock

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

import tune_wait_time as tw  # noqa: E402
from controllers import wait_time  # noqa: E402


def result(losses, max_waits):
    return {
        n: {"avg_time_loss_s": loss, "max_wait_s": mw, "avg_wait_s": 0.0}
        for n, loss, mw in zip(tw.SCENARIOS, losses, max_waits)
    }


FIXED = result([20, 30, 20], [46, 59, 46])


class TuneWaitTimeTest(unittest.TestCase):
    def test_training_and_held_out_are_disjoint(self):
        self.assertEqual(set(tw.SCENARIOS), {"uneven", "rush", "balanced"})
        name, ew, ns = tw.HELD_OUT
        self.assertEqual((name, ew, ns), ("held_out", 200, 600))
        self.assertNotIn((ew, ns), set(tw.SCENARIOS.values()))

    def test_mean_loss(self):
        self.assertAlmostEqual(tw.mean_loss(result([10, 20, 30], [0, 0, 0])), 20)

    def test_admissible_equal_max_wait_allowed(self):
        self.assertTrue(tw.admissible(result([1, 1, 1], [46, 59, 46]), FIXED))

    def test_admissible_rejects_any_scenario_over_fixed(self):
        # worse in one scenario only, even though the overall max is lower than fixed's
        self.assertFalse(tw.admissible(result([1, 1, 1], [47, 40, 40]), FIXED))

    def test_select_skips_inadmissible_best(self):
        rows = [
            ((10, 0, 60), result([1, 1, 1], [99, 10, 10])),
            ((20, 0, 60), result([5, 5, 5], [10, 10, 10])),
            ((30, 0, 60), result([9, 9, 9], [10, 10, 10])),
        ]
        self.assertEqual(tw.select(rows, FIXED), (20, 0, 60))

    def test_select_none_when_nothing_admissible(self):
        rows = [((10, 0, 60), result([1, 1, 1], [99, 99, 99]))]
        self.assertIsNone(tw.select(rows, FIXED))

    def test_select_tie_prefers_smaller_config(self):
        same = result([5, 5, 5], [10, 10, 10])
        self.assertEqual(tw.select([((20, 0, 60), same), ((20, 0, 45), same)], FIXED), (20, 0, 45))

    def test_configure_sets_controller_constants(self):
        with mock.patch.multiple(wait_time, MIN_GREEN=0, SWITCH_MARGIN=0, MAX_GREEN=0):
            tw.configure(15, 25, 45)
            self.assertEqual((wait_time.MIN_GREEN, wait_time.SWITCH_MARGIN, wait_time.MAX_GREEN), (15, 25, 45))

    def test_table_marks_status(self):
        rows = [((10, 0, 60), result([1, 1, 1], [99, 10, 10])), ((20, 0, 60), result([5, 5, 5], [10, 10, 10]))]
        lines = tw.table(rows, FIXED)
        self.assertEqual(len(lines), 4)
        status = {line.split()[-1]: line.split()[-2] for line in lines[1:] if line.endswith("fixed")}
        status.update({" ".join(line.split()[-3:]): line.split()[-4] for line in lines[1:] if "min_green" in line})
        self.assertEqual(status["fixed"], "baseline")
        self.assertEqual(status["min_green=10 margin=0 max_green=60"], "max_wait")
        self.assertEqual(status["min_green=20 margin=0 max_green=60"], "ok")
        # sorted by mean loss: lowest first
        self.assertIn("min_green=10", lines[1])


if __name__ == "__main__":
    unittest.main()
