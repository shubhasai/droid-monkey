"""Process death: the OS kills the app in the background, the user comes back."""

import time

from ..observe import classify
from .base import Attack


class ProcessDeath(Attack):
    name = "process_death"

    def execute(self, scenario):
        self.prepare(scenario)
        before = self.fields()
        before_screen = self.snapshot("before")
        self.fire(scenario.action)

        self.device.kill_in_background()  # verified: the process is really gone
        self.device.launch()               # like returning from recents
        self.device.set_rotation(0)
        time.sleep(2)

        crashes = self.crashes()
        after_screen = self.snapshot("after")
        after = {} if crashes else self.fields()
        restored = self._same_screen(before_screen, after_screen)
        return classify(crashes, before, after, screen_restored=restored), before, after

    def _same_screen(self, a, b) -> bool:
        fa, fb = a.fingerprint(self.cfg.package), b.fingerprint(self.cfg.package)
        if not fa:
            return True
        return len(fa & fb) / len(fa | fb) >= 0.5
