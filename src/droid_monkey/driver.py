"""AppDriver: drive the app under test. Launch it fresh, walk to a screen, fill forms, wait for the screen to settle,
handle permission prompts, read crashes, and save evidence. Used by the spec runner and the explorer; it injects
no faults itself."""

import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig, Target
from .tools import logcat, ui
from .tools.adb import Adb, AdbError
from .tools.device import Device

# Sample values for form filling, matched against a field's resource-id (lowercased).
DEFAULT_FILL = [
    ("email", "ravi@example.com"),
    ("mail", "ravi@example.com"),
    ("phone", "9876543210"),
    ("mobile", "9876543210"),
    ("pin", "560001"),
    ("zip", "560001"),
    ("postal", "560001"),
    ("city", "Bengaluru"),
    ("address", "12 MG Road"),
    ("street", "12 MG Road"),
    ("name", "Ravi Kumar"),
]
FALLBACK_FILL = "Test 123"

PERMISSION_UI = "permissioncontroller"  # com.android.* or com.google.android.* depending on the image
PERMISSION_DENY_IDS = ["com.android.permissioncontroller:id/permission_deny_button",
                       "com.android.permissioncontroller:id/permission_deny_and_dont_ask_again_button"]
PERMISSION_ALLOW_IDS = [
    "com.android.permissioncontroller:id/permission_allow_foreground_only_button",
    "com.android.permissioncontroller:id/permission_allow_button",
    "com.android.permissioncontroller:id/permission_allow_one_time_button",
]


class NavigationError(AdbError):
    pass


@dataclass
class Context:
    adb: Adb
    device: Device
    cfg: AppConfig
    run_dir: Path
    targets: dict[str, Target] = field(default_factory=dict)  # screen id -> path, from the explorer's map


class AppDriver:
    def __init__(self, ctx: Context, evidence_dir: Path):
        self.ctx = ctx
        self.adb, self.device, self.cfg = ctx.adb, ctx.device, ctx.cfg
        self.package = ctx.cfg.package
        self.dir = evidence_dir  # created on first write, so runs that save nothing leave no empty folders
        self.notes: list[str] = []

    # ---- getting somewhere -----------------------------------------------------------------------

    def fresh(self, clear_data: bool = False) -> None:
        """Fresh process in portrait, launched like a launcher tap, past any entry steps (e.g. a login)."""
        self.device.force_stop()
        if clear_data:
            self.device.clear_data()
        self.device.set_rotation(0)
        logcat.clear(self.adb)
        self.device.launch()
        self.device.set_rotation(0)  # Android 16 may restore the app's last orientation on launch
        for step in self.cfg.entry_steps:
            self.step(step)

    def target(self, screen_id: str) -> Target:
        found = self.ctx.targets.get(screen_id)
        if found is None:
            raise NavigationError(f"unknown screen '{screen_id}': run `droid-monkey explore` first")
        return found

    def go_to(self, target: Target) -> None:
        for step in target.steps:
            self.step(step)
        if target.fill:
            self.fill_form()

    def step(self, step: str) -> None:
        """One replayable step.

        `tap <selector>`, `tap? <selector>` (only if present), `wait <selector>`, `type <selector>=<text>`,
        `scroll down|up`, `fill`, `back`, `allow_permission`, `deny_permission`, `sleep <s>`.
        Selectors: a resource-id, `text:<text>`, `desc:<content-desc>`, or `at:<x>,<y>` for elements with no handle.
        """
        verb, _, arg = step.strip().partition(" ")
        if verb in ("tap", "tap?") and arg.startswith("at:"):
            x, y = arg[3:].split(",")
            self.adb.shell(f"input tap {x} {y}")
        elif verb in ("tap", "tap?"):
            node = ui.wait_for(self.adb, arg, timeout=3 if verb == "tap?" else 12)
            if node is None:
                if verb == "tap?":
                    return
                raise NavigationError(f"step '{step}': '{arg}' never appeared")
            ui.tap(self.adb, node)
        elif verb == "wait":
            if ui.wait_for(self.adb, arg, timeout=12) is None:
                raise NavigationError(f"step '{step}': '{arg}' never appeared")
        elif verb == "type":
            selector, _, text = arg.partition("=")
            node = ui.wait_for(self.adb, selector, timeout=12)
            if node is None:
                raise NavigationError(f"step '{step}': '{selector}' never appeared")
            ui.tap(self.adb, node)
            time.sleep(0.3)
            ui.type_text(self.adb, text)
            ui.hide_keyboard(self.adb)
        elif verb == "scroll":
            self.scroll(arg or "down")
        elif verb == "allow_permission":
            if not self.accept_permission_dialog():
                raise NavigationError("no permission prompt to allow")
        elif verb == "deny_permission":
            if not self.press_first(PERMISSION_DENY_IDS):
                raise NavigationError("no permission prompt to deny")
        elif verb == "fill":
            self.fill_form()
        elif verb == "back":
            self.adb.shell("input keyevent 4")
        elif verb == "sleep":
            time.sleep(float(arg))
        else:
            raise NavigationError(f"unknown step '{step}'")

    # ---- forms -----------------------------------------------------------------------------------

    def fill_value(self, key: str) -> str:
        k = key.lower()
        for pattern, value in self.cfg.fill_values.items():
            if pattern.lower() in k:
                return value
        for pattern, value in DEFAULT_FILL:
            if pattern in k:
                return value
        return FALLBACK_FILL

    def fill_form(self, max_pages: int = 6) -> None:
        """Type a sample value into every empty editable field, scrolling as needed. Works without test tags."""
        done: set = set()
        for _ in range(max_pages):
            progressed = False
            while True:
                screen = ui.dump(self.adb)
                pending = [n for n in screen.editable_fields(self.package)
                           if (n.rid or n.bounds) not in done and not n.text]
                visible = [n for n in pending if n.bounds[1] >= 0 and n.bounds[3] > n.bounds[1] + 20]
                if not visible:
                    break
                node = visible[0]
                ui.tap(self.adb, node)
                time.sleep(0.3)
                ui.type_text(self.adb, self.fill_value(node.rid or node.cls))
                ui.hide_keyboard(self.adb)
                done.add(node.rid or node.bounds)
                progressed = True
            scroller = ui.dump(self.adb).scrollable(self.package)
            if scroller is None:
                break
            before = ui.dump(self.adb).xml
            ui._swipe(self.adb, scroller.bounds, up=True)
            if ui.dump(self.adb).xml == before and not progressed:
                break

    def fields(self) -> dict[str, str]:
        return ui.collect_fields(self.adb, self.package)

    # ---- watching --------------------------------------------------------------------------------

    def settle(self, timeout: float = 12.0) -> tuple[ui.Screen, float, bool]:
        """Dump until two consecutive dumps match and no progress indicator shows. Returns (screen, seconds, saw_progress)."""
        start = time.time()
        prev = ui.dump(self.adb)
        saw_progress = self.has_progress(prev)
        while time.time() - start < timeout:
            time.sleep(0.3)
            cur = ui.dump(self.adb)
            busy = self.has_progress(cur)
            saw_progress |= busy
            if cur.xml == prev.xml and not busy:
                return cur, round(time.time() - start, 1), saw_progress
            prev = cur
        return prev, round(time.time() - start, 1), saw_progress

    def has_progress(self, screen: ui.Screen) -> bool:
        return any("ProgressBar" in n.cls for n in screen.app_nodes(self.package))

    @staticmethod
    def is_permission_prompt(screen: ui.Screen) -> bool:
        return any(PERMISSION_UI in n.package or PERMISSION_UI in n.rid for n in screen.nodes)

    def accept_permission_dialog(self, timeout: float = 4.0) -> bool:
        return self.press_first(PERMISSION_ALLOW_IDS, timeout)

    def press_first(self, ids: list[str], timeout: float = 4.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            screen = ui.dump(self.adb)
            for rid in ids:
                node = screen.find(rid)
                if node:
                    ui.tap(self.adb, node)
                    return True
            time.sleep(0.3)
        return False

    def scroll(self, direction: str = "down") -> None:
        """Swipe the screen's main scrollable container (or the middle of the screen if there is none)."""
        screen = ui.dump(self.adb)
        box = screen.scrollable(self.package)
        if box is None:
            root = max(screen.app_nodes(self.package), key=lambda n: n.area, default=None)
            if root is None:
                return
            box = root
        ui._swipe(self.adb, box.bounds, up=(direction == "down"))

    def crashes(self) -> list[logcat.Crash]:
        return logcat.read_crashes(self.adb, self.package)

    def snapshot(self, label: str) -> ui.Screen:
        """Screen dump and screenshot saved as evidence."""
        screen = ui.dump(self.adb)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{label}.xml").write_text(screen.xml)
        try:
            (self.dir / f"{label}.png").write_bytes(ui.screenshot(self.adb))
        except AdbError as e:
            self.notes.append(f"screenshot {label}: {e}")
        return screen
