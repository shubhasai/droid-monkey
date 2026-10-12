"""AppDriver: drive the app under test. Launch it fresh, walk to a screen, fill forms, wait for the screen to settle,
handle permission prompts, read crashes, and save evidence. Used by the spec runner and the explorer; it injects
no faults itself."""

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig, Target
from .tools import logcat, ui
from .tools.adb import Adb, AdbError
from .tools.device import Device

# Sample values for form filling, matched against a field's resource-id (lowercased).
DEFAULT_FILL = [
    ("search", "Android"),
    ("query", "Android"),
    ("find", "Android"),
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


# Buttons that close a keyboard's own one-time sheets (Gboard's "Try out your stylus", tips, surveys). These belong to
# the input method, not the app: left open they cover the app and every later check sees the keyboard's sheet.
DISMISS_LABELS = {"cancel", "not now", "no thanks", "skip", "dismiss", "close", "got it", "maybe later"}
# Controls that close an app's own interruption (a promo, survey, what's-new, rating prompt) without acting on it.
# Used only when a replay's next target is missing: the interruption is then what is in the way.
INTERRUPTION_CLOSE = re.compile(r"^(close|dismiss|not now|no thanks|maybe later|later|skip|got it|cancel|x|✕|×|"
                                r"close (dialog|sheet|banner|promo))$", re.I)


class NavigationError(AdbError):
    pass


class OutwardActionRefused(NavigationError):
    """A tap droid-monkey will not make: it would act on the app's live backend (see safety.py)."""


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
        for step in self.cfg.entry_steps:  # written by the developer (e.g. a test-account login): trusted
            self.step(step, trusted=True)
            self.settle(timeout=self.NAV_SETTLE)

    def target(self, screen_id: str) -> Target:
        found = self.ctx.targets.get(screen_id)
        if found is None:
            raise NavigationError(f"unknown screen '{screen_id}': run `droid-monkey explore` first")
        return found

    NAV_SETTLE = 6.0  # s: how long a navigation step may take to finish its transition

    def go_to(self, target: Target) -> None:
        """Walk a recorded path. Each step waits for the screen to settle, as it did when the path was recorded:
        a tap fired mid-transition (a pager still sliding, an activity still opening) lands on the wrong page."""
        for step in target.steps:
            self.step(step)
            self.settle(timeout=self.NAV_SETTLE)
        if target.fill:
            self.fill_form()

    def check_arrival(self, target: Target) -> None:
        """The replayed path ended on the screen it was mapped to; if something covers it (a promo, a survey),
        close that once and look again. Raises NavigationError when it is still a different screen, so an attack
        is never run, and judged, on the wrong one."""
        want = frozenset(target.fingerprint)
        if not any(t.startswith(("id:", "desc:", "text:", "edit:")) for t in want):
            return  # a map from an older droid-monkey (class names only): nothing to compare; re-explore to check
        from .explorer import _similar
        for attempt in range(2):
            screen, _, _ = self.settle(timeout=self.NAV_SETTLE)
            if _similar(screen.fingerprint(self.package), want):
                return
            if attempt == 0 and not self.close_interruption(screen):
                break
        raise NavigationError(f"replaying the path to '{target.name}' ended on a different screen "
                              f"(something covers it, or the app changed): not attacking the wrong screen")

    def _guard(self, node, screen, trusted: bool) -> None:
        """Refuse a tap that would act on the app's live backend (see safety.py)."""
        from .safety import outward_reason
        if trusted or not self.cfg.live_backend:
            return
        reason = outward_reason(node, screen, self.cfg.allow_actions)
        if reason:
            raise OutwardActionRefused(f"refused to tap {reason}: it would act on the app's live backend (an account, a post, "
                                "a payment). Set live_backend: false or allow_actions in monkey.yaml for a test backend.")

    def _find(self, selector: str, timeout: float):
        """Wait for an element; returns (node, the screen it was found on) or (None, None). If it has not shown
        after a few seconds and the app shows an interruption (a promo, survey, what's-new sheet), close it once."""
        start = time.time()
        deadline, closed = start + timeout, False
        while True:
            screen = ui.dump(self.adb)
            node = screen.find(selector)
            if node or time.time() >= deadline:
                return node, (screen if node else None)
            if not closed and time.time() - start > 3:
                closed = self.close_interruption(screen)
            time.sleep(0.3)

    def close_interruption(self, screen: ui.Screen) -> bool:
        """Tap the app's own close / not-now control. Only called when a replay's target is missing, so whatever
        covers the screen is in the way; it never acts on the interruption (no 'Explore', 'Donate', 'Rate')."""
        from .safety import labels
        for n in screen.app_nodes(self.package):
            if not (n.clickable and n.area):
                continue
            label = next((x.strip() for x in labels(n, screen) if INTERRUPTION_CLOSE.match(x.strip())), "")
            if label:  # its own text, or the label on its icon (Compose puts it on a child)
                ui.tap(self.adb, n)
                self.notes.append(f"closed an interruption ('{label}') that covered the next step's target")
                time.sleep(0.8)
                return True
        return False

    def step(self, step: str, trusted: bool = False) -> None:
        """One replayable step.

        `tap <selector>`, `tap? <selector>` (only if present), `wait <selector>`, `type <selector>=<text>`,
        `scroll down|up`, `fill`, `back`, `allow_permission`, `deny_permission`, `sleep <s>`.
        Selectors: a resource-id, `text:<text>`, `desc:<content-desc>`, or `at:<x>,<y>` for elements with no handle.
        """
        verb, _, arg = step.strip().partition(" ")
        if verb in ("tap", "tap?") and arg.startswith("at:"):
            self.settle(timeout=5)  # no element to wait for: at least let animations finish
            x, y = arg[3:].split(",")
            screen = ui.dump(self.adb)
            hit = [n for n in screen.app_nodes(self.package) if n.area and n.bounds[0] <= int(x) <= n.bounds[2]
                   and n.bounds[1] <= int(y) <= n.bounds[3]]
            if hit:
                self._guard(min(hit, key=lambda n: n.area), screen, trusted)
            self.adb.shell(f"input tap {x} {y}")
        elif verb in ("tap", "tap?"):
            node, screen = self._find(arg, timeout=8 if verb == "tap?" else 12)
            if node is None:
                if verb == "tap?":
                    return
                raise NavigationError(f"step '{step}': '{arg}' never appeared")
            self._guard(node, screen, trusted)
            ui.tap(self.adb, node)
        elif verb == "wait":
            if ui.wait_for(self.adb, arg, timeout=12) is None:
                raise NavigationError(f"step '{step}': '{arg}' never appeared")
        elif verb == "type":
            selector, _, text = arg.partition("=")
            node = ui.wait_for(self.adb, selector, timeout=12)
            if node is None:
                raise NavigationError(f"step '{step}': '{selector}' never appeared")
            self._focus(node)
            ui.type_text(self.adb, text)
            ui.hide_keyboard(self.adb)
            self.dismiss_interruption(ui.dump(self.adb))
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
        elif verb == "enter":  # the keyboard's Enter/Search key: how Android search boxes submit
            self.adb.shell("input keyevent 66")
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

    EDIT_SUFFIX = " edited"

    def fill_form(self, max_pages: int = 6) -> None:
        """Make the user's unsaved input: type a sample value into every empty field, and append to every field
        that is already filled (e.g. an edit form loaded from storage), so the attack tests unsaved changes rather
        than data the app simply reloads. Scrolls as needed; works without test tags."""
        done: set = set()
        for _ in range(max_pages):
            progressed = False
            while True:
                screen = ui.dump(self.adb)
                fields = screen.editable_fields(self.package)
                pending = [n for n in fields if (n.rid or n.bounds) not in done]
                visible = [n for n in pending if n.bounds[1] >= 0 and n.bounds[3] > n.bounds[1] + 20]
                if not visible:
                    break
                node = visible[0]
                self._focus(node)
                if node.text:
                    self.adb.shell("input keyevent 123")  # KEYCODE_MOVE_END: append to the existing value
                    ui.type_text(self.adb, self.EDIT_SUFFIX)
                else:
                    ui.type_text(self.adb, self.fill_value(node.rid or node.cls))
                ui.hide_keyboard(self.adb)
                self.dismiss_interruption(ui.dump(self.adb))
                done.add(node.rid or node.bounds)
                progressed = True
            scroller = ui.dump(self.adb).scrollable(self.package)
            if scroller is None:
                break
            before = ui.dump(self.adb).xml
            ui._swipe(self.adb, scroller.bounds, up=True)
            if ui.dump(self.adb).xml == before and not progressed:
                break

    def _focus(self, node) -> None:
        """Tap a field so typing goes into it. Focusing a field is what opens a keyboard's own sheets (Gboard's
        stylus tutorial); close one and tap the field again, or the text would go to the sheet."""
        ui.tap(self.adb, node)
        time.sleep(0.4)
        if self.dismiss_interruption(ui.dump(self.adb)):
            ui.tap(self.adb, node)
            time.sleep(0.3)

    def fields(self) -> dict[str, str]:
        return ui.collect_fields(self.adb, self.package)

    # ---- watching --------------------------------------------------------------------------------

    def dismiss_interruption(self, screen: ui.Screen) -> bool:
        """Close a keyboard's own sheet covering the app (see DISMISS_LABELS). Returns True if one was closed."""
        for n in screen.nodes:
            if ("inputmethod" in n.package and n.clickable and n.area
                    and (n.text or n.desc).strip().lower() in DISMISS_LABELS):
                ui.tap(self.adb, n)
                self.notes.append(f"closed a keyboard sheet ({n.package}: '{n.text or n.desc}')")
                time.sleep(0.5)
                return True
        return False

    def settle(self, timeout: float = 12.0) -> tuple[ui.Screen, float, bool]:
        """Dump until two consecutive dumps show the same content and no progress indicator shows (positions may keep
        moving: animations). Returns (screen, seconds, saw_progress)."""
        start = time.time()
        prev = ui.dump(self.adb)
        if self.dismiss_interruption(prev):
            prev = ui.dump(self.adb)
        saw_progress = self.has_progress(prev)
        while time.time() - start < timeout:
            time.sleep(0.3)
            cur = ui.dump(self.adb)
            if self.dismiss_interruption(cur):  # it can appear mid-wait, e.g. a keyboard opening on a restored field
                cur = ui.dump(self.adb)
            busy = self.has_progress(cur)
            saw_progress |= busy
            if cur.content() == prev.content() and not busy:
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
        """Screen dump and screenshot saved as evidence (of the app, not of a keyboard sheet covering it)."""
        screen = ui.dump(self.adb)
        if self.dismiss_interruption(screen):
            screen = ui.dump(self.adb)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{label}.xml").write_text(screen.xml)
        try:
            (self.dir / f"{label}.png").write_bytes(ui.screenshot(self.adb))
        except AdbError as e:
            self.notes.append(f"screenshot {label}: {e}")
        return screen
