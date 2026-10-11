"""Verified device actions for one app. Each one confirms its effect, because a silent no-op makes a buggy app look "survived"."""

import re
import time

from .adb import Adb, AdbError

ROTATIONS = {0: 0, 1: 90, 2: 180, 3: 270}


class Device:
    def __init__(self, adb: Adb, package: str):
        self.adb = adb
        self.package = package
        self._activity: str | None = None
        self._revoked: list[str] = []
        self._initial_rotation: int | None = None

    # ---- app lifecycle -------------------------------------------------------------------------

    def launcher_activity(self) -> str:
        if self._activity is None:
            out = self.adb.shell(
                f"cmd package resolve-activity --brief -a android.intent.action.MAIN "
                f"-c android.intent.category.LAUNCHER {self.package}"
            ).strip().splitlines()
            component = out[-1].strip() if out else ""
            if "/" not in component:
                raise AdbError(f"no launcher activity for {self.package}: is the app installed?")
            self._activity = component
        return self._activity

    def launch(self) -> None:
        """Start exactly like a launcher tap, so an existing task (and its saved state) is brought back."""
        self.adb.shell(
            "am start -W -a android.intent.action.MAIN -c android.intent.category.LAUNCHER "
            f"-f 0x10200000 -n {self.launcher_activity()}"
        )

    def pid(self) -> str:
        return self.adb.shell(f"pidof {self.package}", check=False).strip()

    def force_stop(self) -> None:
        self.adb.shell(f"am force-stop {self.package}")

    def clear_data(self) -> None:
        self.adb.shell(f"pm clear {self.package}")

    def home(self) -> None:
        self.adb.shell("input keyevent 3")

    def kill_in_background(self, tries: int = 10) -> None:
        """HOME, then `am kill` until the process is gone, the way low-memory killing does it."""
        self.home()
        for _ in range(tries):
            time.sleep(1)
            self.adb.shell(f"am kill {self.package}")
            if not self.pid():
                return
        raise AdbError(f"{self.package} still alive after am kill: process death was not injected")

    # ---- rotation --------------------------------------------------------------------------------

    def rotation(self) -> int:
        out = self.adb.shell("dumpsys window displays")
        m = re.search(r"mCurrentRotation=ROTATION_(\d+)", out)
        return int(m.group(1)) if m else 0

    def set_rotation(self, user_rotation: int, timeout: float = 5.0) -> None:
        """Set rotation (0..3) and wait until the display reports it."""
        if self._initial_rotation is None:
            self._initial_rotation = {v: k for k, v in ROTATIONS.items()}.get(self.rotation(), 0)
        self.adb.shell("settings put system accelerometer_rotation 0")
        self.adb.shell(f"settings put system user_rotation {user_rotation}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.rotation() == ROTATIONS[user_rotation]:
                return
            time.sleep(0.25)
        raise AdbError(f"display never reached rotation {ROTATIONS[user_rotation]}")

    def flip_rotation(self) -> int:
        """Rotate to the other orientation from the current one; returns the new user_rotation."""
        target = 1 if self.rotation() in (0, 180) else 0
        self.set_rotation(target)
        return target

    # ---- network ---------------------------------------------------------------------------------

    def online(self) -> bool:
        out = self.adb.shell("dumpsys connectivity")
        m = re.search(r"Active default network: (\S+)", out)
        return bool(m) and m.group(1) != "none"

    def set_network(self, enabled: bool, timeout: float = 15.0) -> None:
        verb = "enable" if enabled else "disable"
        self.adb.shell(f"svc wifi {verb}")
        self.adb.shell(f"svc data {verb}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.online() == enabled:
                return
            time.sleep(0.5)
        if not enabled:
            raise AdbError("device still online after disabling wifi and data")

    # ---- permissions -----------------------------------------------------------------------------

    def runtime_permissions(self) -> dict[str, bool]:
        out = self.adb.shell(f"dumpsys package {self.package}")
        perms: dict[str, bool] = {}
        in_runtime = False
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("runtime permissions:"):
                in_runtime = True
                continue
            if in_runtime:
                m = re.match(r"([\w.]+): granted=(true|false)", s)
                if not m:
                    in_runtime = False
                    continue
                perms[m.group(1)] = m.group(2) == "true"
        return perms

    def revoke(self, permission: str) -> None:
        """Revoke a runtime permission, as the user would in Settings. Android kills the app."""
        self.adb.shell(f"pm revoke {self.package} {permission}")
        self._revoked.append(permission)

    # ---- restore ---------------------------------------------------------------------------------

    def restore(self) -> None:
        """Undo every fault so one attack never poisons the next. Always called in a finally block."""
        errors = []
        try:
            self.set_network(True)
        except AdbError as e:
            errors.append(str(e))
        try:
            self.set_rotation(self._initial_rotation or 0)
        except AdbError as e:
            errors.append(str(e))
        for perm in self._revoked:
            self.adb.shell(f"pm grant {self.package} {perm}", check=False)
        self._revoked.clear()
        if errors:
            raise AdbError("restore incomplete: " + "; ".join(errors))
