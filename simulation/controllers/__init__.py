from controllers.fixed import FixedController
from controllers.queue_based import QueueBasedController

CONTROLLERS = {
    "fixed": FixedController,
    "queue_based": QueueBasedController,
}
