from controllers.fixed import FixedController
from controllers.queue_based import QueueBasedController
from controllers.wait_time import WaitTimeController

CONTROLLERS = {
    "fixed": FixedController,
    "queue_based": QueueBasedController,
    "wait_time": WaitTimeController,
}
