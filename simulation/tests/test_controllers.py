from controllers import CONTROLLERS
from controllers.fixed import FixedController


def test_registry_contains_expected_controllers():
    assert {"fixed", "queue_based", "wait_time"} <= set(CONTROLLERS)


def test_fixed_controller_does_nothing():
    controller = FixedController("A0")
    assert controller.tls_id == "A0"
    assert controller.step(10) is None
