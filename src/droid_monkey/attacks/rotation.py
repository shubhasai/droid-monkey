"""Rotation: a configuration change recreates the Activity, possibly while work is in flight."""

import time

from ..observe import classify
from .base import Attack

IN_FLIGHT_DELAY = 1.0  # rotate this long after the trigger, while its work is still running


class Rotation(Attack):
    name = "rotation"

    def execute(self, scenario):
        target = self.prepare(scenario)
        before = self.fields() if target.fill else {}
        self.snapshot("before")

        self.fire(scenario.action)
        if scenario.action:
            time.sleep(IN_FLIGHT_DELAY)  # no screen dumps here: each one takes 1-2 s
        self.device.flip_rotation()      # verified: the display reports the new rotation
        time.sleep(scenario.settle_seconds)

        crashes = self.crashes()
        self.snapshot("after")
        after = self.fields() if (before and not crashes) else {}
        return classify(crashes, before, after), before, after
