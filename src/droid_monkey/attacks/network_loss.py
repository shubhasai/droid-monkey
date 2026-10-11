"""Network loss: connectivity drops right before the user triggers a network operation."""

import time

from ..observe import classify
from .base import Attack


class NetworkLoss(Attack):
    name = "network_loss"

    def execute(self, scenario):
        target = self.prepare(scenario)
        before = self.fields() if target.fill else {}
        self.snapshot("before")

        self.device.set_network(False)  # verified: no active default network
        self.fire(scenario.action)
        time.sleep(scenario.settle_seconds)

        crashes = self.crashes()
        self.snapshot("after")
        after = self.fields() if (before and not crashes) else {}
        return classify(crashes, before, after), before, after
