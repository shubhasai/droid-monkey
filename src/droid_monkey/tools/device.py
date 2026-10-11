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
        self._changed: set[str] = set()  # settings an attack changed, undone by restore()

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

    def launch(self) -> str:
        """Start exactly like a launcher tap, so an existing task (and its saved state) is brought back."""
        return self.adb.shell(
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

    # ---- configuration and system faults ---------------------------------------------------------

    def set_airplane(self, on: bool, timeout: float = 15.0) -> None:
        self.adb.shell(f"cmd connectivity airplane-mode {'enable' if on else 'disable'}")
        self._changed.add("airplane")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.online() != on:
                return
            time.sleep(0.5)
        if on:
            raise AdbError("device still online in airplane mode")

    def set_dark_mode(self, on: bool) -> None:
        self.adb.shell(f"cmd uimode night {'yes' if on else 'no'}")
        self._changed.add("dark_mode")

    def set_font_scale(self, scale: float) -> None:
        self.adb.shell(f"settings put system font_scale {scale}")
        self._changed.add("font_scale")

    def set_app_locale(self, locale: str) -> None:
        """Per-app language (Android 13+). 'ar', 'he', 'fa' or 'ur' switch the app to right-to-left layout."""
        self.adb.shell(f"cmd locale set-app-locales {self.package} --locales {locale or '\"\"'}")
        self._changed.add("locale")

    def trim_memory(self, level: str) -> None:
        """Send onTrimMemory. Background levels need the app in the background, so it is sent home first."""
        if level.startswith("RUNNING"):
            self.adb.shell(f"am send-trim-memory {self.package} {level}")
            return
        self.home()
        for _ in range(10):  # Android refuses a background level until the process has left the foreground
            time.sleep(1)
            out = self.adb.shell(f"am send-trim-memory {self.package} {level} 2>&1", check=False)
            if "foreground process" not in out:
                return
        raise AdbError(f"{self.package} never left the foreground: trim level {level} was not delivered")

    def recreate_activity(self, times: int) -> None:
        """Rotate there and back `times` times. Each flip destroys and recreates the Activity, so anything that holds
        on to an old one piles up; an even number of flips ends in the starting orientation."""
        for _ in range(times * 2):
            self.flip_rotation()
            time.sleep(1.5)

    def cpu_stress(self, threads: int) -> None:
        """Busy-loop `threads` CPU cores in the background, like other apps working on a mid-range phone."""
        cores = int(self.adb.shell("nproc", check=False).strip() or 4)
        threads = max(1, min(threads, cores))
        for _ in range(threads):
            self.adb.shell("setsid nohup sh -c 'exec yes > /dev/null' > /dev/null 2>&1 < /dev/null &")
        self._changed.add("cpu_stress")
        time.sleep(1)
        running = int(self.adb.shell("ps -A | grep -cw yes", check=False).strip() or 0)
        if running < threads:
            raise AdbError(f"cpu stress: only {running} of {threads} load processes running")

    # ---- measurements ----------------------------------------------------------------------------

    def memory(self) -> dict[str, int]:
        """Live Activity instances and Java heap (KB) after a forced garbage collection, so only objects something
        still references are counted. The GC needs a debuggable build, which is what droid-monkey installs."""
        out = self.adb.shell(f"am dumpheap -g {self.package} /data/local/tmp/droid-monkey.hprof", check=False)
        gc = "Error" not in out and "not debuggable" not in out
        time.sleep(2)
        self.adb.shell("rm -f /data/local/tmp/droid-monkey.hprof", check=False)
        info = self.adb.shell(f"dumpsys meminfo {self.package}", check=False)
        activities = re.search(r"Activities:\s+(\d+)", info)
        heap = re.search(r"Java Heap:\s+(\d+)", info)
        if not activities or not heap:
            raise AdbError(f"no memory info for {self.package}: is it running?")
        return {"activities": int(activities.group(1)), "java_heap_kb": int(heap.group(1)), "gc": int(gc)}

    def reset_frames(self) -> None:
        self.adb.shell(f"dumpsys gfxinfo {self.package} reset", check=False)

    def frames(self) -> dict[str, float]:
        """Rendering since the last reset: frozen frames (over 700 ms, the Play vitals threshold), janky share,
        slow UI-thread frames and the 99th percentile."""
        out = self.adb.shell(f"dumpsys gfxinfo {self.package}", check=False)

        def num(pattern: str) -> float:
            m = re.search(pattern, out)
            return float(m.group(1)) if m else 0.0

        hist = re.search(r"HISTOGRAM:(.*)", out)
        frozen = 0
        if hist:
            for ms, count in re.findall(r"(\d+)ms=(\d+)", hist.group(1)):
                if int(ms) >= 700:
                    frozen += int(count)
        return {"total": num(r"Total frames rendered: (\d+)"), "janky_pct": num(r"Janky frames: \d+ \(([\d.]+)%\)"),
                "frozen": frozen, "slow_ui_thread": num(r"Number Slow UI thread: (\d+)"),
                "p99_ms": num(r"99th percentile: (\d+)ms")}

    def start_time(self, kind: str) -> tuple[int, str]:
        """One timed launch, as Android measures it (`am start -W`: until the first frame is drawn).
        cold: the process is stopped first; hot: the app is sent Home and brought back. Returns (ms, launch state)."""
        if kind == "cold":
            self.force_stop()
            time.sleep(1)
        else:
            self.home()
            time.sleep(1.5)
        out = self.launch()
        ms, state = re.search(r"TotalTime: (\d+)", out), re.search(r"LaunchState: (\w+)", out)
        if not ms:
            raise AdbError(f"no launch time reported: {out.strip()[:200]}")
        launch_state = state.group(1) if state else "UNKNOWN"
        if kind == "cold" and launch_state not in ("COLD", "UNKNOWN"):
            raise AdbError(f"expected a cold start, Android reported {launch_state}")
        return int(ms.group(1)), launch_state

    def background(self, seconds: float) -> None:
        """Leave the app (Home) and come back after `seconds`, without killing it."""
        self.home()
        time.sleep(seconds)
        self.launch()

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
        undo = {
            "airplane": lambda: self.adb.shell("cmd connectivity airplane-mode disable"),
            "dark_mode": lambda: self.adb.shell("cmd uimode night no"),
            "font_scale": lambda: self.adb.shell("settings put system font_scale 1.0"),
            "locale": lambda: self.adb.shell(f'cmd locale set-app-locales {self.package} --locales ""'),
            "cpu_stress": lambda: self.adb.shell("pkill -x yes", check=False),
        }
        for key in sorted(self._changed):
            try:
                undo[key]()
            except AdbError as e:
                errors.append(f"{key}: {e}")
        self._changed.clear()
        if errors:
            raise AdbError("restore incomplete: " + "; ".join(errors))
