"""Permission revoke: the user grants a permission, later revokes it in Settings, then uses the feature again."""

import time

from ..observe import SKIPPED, Verdict, classify
from ..tools import logcat, ui
from .base import Attack


class PermissionRevoke(Attack):
    name = "permission_revoke"

    def execute(self, scenario):
        target = self.prepare(scenario, clear_data=True)  # first-time user: nothing granted, nothing cached
        granted_before = self.device.runtime_permissions()

        # 1. Use the feature once, granting whatever it asks for through the app's own flow.
        self.fire(scenario.action)
        if self.accept_permission_dialog():
            self.notes.append("granted the permission prompt on first use")
        time.sleep(2)
        if crashes := self.crashes():
            return classify(crashes, {}, {}), {}, {}

        granted_after = self.device.runtime_permissions()
        newly = [p for p, g in granted_after.items() if g and not granted_before.get(p)]
        to_revoke = newly or [p for p, g in granted_after.items() if g]
        if not to_revoke:
            return Verdict(SKIPPED, "the action did not use any runtime permission"), {}, {}
        self.notes.append(f"revoking: {', '.join(to_revoke)}")
        self.snapshot("before")

        # 2. The user revokes it in Settings; Android kills the app. Then they come back and try again.
        for perm in to_revoke:
            self.device.revoke(perm)
        time.sleep(1)
        logcat.clear(self.adb)
        self.device.launch()
        self.device.set_rotation(0)
        if not self._action_on_screen(scenario.action):
            self.navigate(target)
        self.fire(scenario.action)
        time.sleep(scenario.settle_seconds)

        crashes = self.crashes()
        self.snapshot("after")
        return classify(crashes, {}, {}), {}, {}

    def _action_on_screen(self, action: str | None) -> bool:
        if not action or " " not in action:
            return False
        return ui.dump(self.adb).find(action.split(" ", 1)[1]) is not None
