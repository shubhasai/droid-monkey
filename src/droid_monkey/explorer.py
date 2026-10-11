"""Scout: explore any installed app and build a screen map. No app-specific knowledge and no model.

From a fresh launch it taps every distinct clickable element on each screen it reaches, and records what
the tap did: a new screen, the same screen, an app dialog, a permission prompt, slow work (a progress
indicator or a long settle), leaving the app, or a crash. Every screen keeps the replayable path that
reaches it from launch, so attacks can return to it later.
"""

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .attacks.base import Attack, NavigationError
from .tools import logcat, ui
from .tools.adb import AdbError

PERMISSION_UI = "permissioncontroller"
DIALOG_BUTTONS = ("android:id/button1", "android:id/button2", "android:id/button3")
PROGRESS_CLASSES = ("ProgressBar",)


@dataclass
class ActionInfo:
    selector: str
    label: str
    group: str
    effect: str = "unexplored"  # new_screen | same_screen | dialog | permission | left_app | crash | not_found
    to_screen: str | None = None
    settle_seconds: float = 0.0
    showed_progress: bool = False
    dialog_dismiss: str | None = None
    permissions: list[str] = field(default_factory=list)
    crash: str = ""


@dataclass
class ScreenInfo:
    id: str
    path: list[str]
    title: str
    fields: list[str]
    fingerprint: list[str]
    actions: list[ActionInfo] = field(default_factory=list)

    @property
    def has_form(self) -> bool:
        return bool(self.fields)


@dataclass
class ScreenMap:
    package: str
    screens: dict[str, ScreenInfo] = field(default_factory=dict)
    crashes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    baseline_seconds: float = 0.0

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))

    @staticmethod
    def load(path: Path) -> "ScreenMap":
        raw = json.loads(path.read_text())
        screens = {
            sid: ScreenInfo(**{**s, "actions": [ActionInfo(**a) for a in s["actions"]]})
            for sid, s in raw["screens"].items()
        }
        return ScreenMap(package=raw["package"], screens=screens, crashes=raw.get("crashes", []),
                         seconds=raw.get("seconds", 0.0), baseline_seconds=raw.get("baseline_seconds", 0.0))


def _similar(a: frozenset[str], b: frozenset[str], threshold: float = 0.6) -> bool:
    if not a and not b:
        return True
    return len(a & b) / max(1, len(a | b)) >= threshold


def _selector(node: ui.Node) -> str:
    if node.rid:
        return node.rid
    if node.text:
        return f"text:{node.text}"
    if node.desc:
        return f"desc:{node.desc}"
    x, y = node.center
    return f"at:{x},{y}"


def _group(node: ui.Node) -> str:
    """Repeated list items count once: product_1..product_8 -> product_#; id-less rows share their geometry."""
    if node.rid:
        return re.sub(r"\d+", "#", node.rid)
    x1, y1, x2, y2 = node.bounds
    return f"{node.cls}|{x1}|{x2 - x1}|{y2 - y1}"


class Explorer(Attack):
    """Reuses the attack plumbing (navigation, form filling, verified launch) without injecting faults."""

    name = "explore"

    def __init__(self, ctx, max_screens: int = 12, max_actions: int = 10, budget_seconds: float = 900,
                 clear_data: bool = True):
        super().__init__(ctx)
        self.max_screens, self.max_actions, self.budget = max_screens, max_actions, budget_seconds
        self.clear_data = clear_data
        self.baseline = 0.0  # how long settle() takes on a screen that is not changing (dump overhead)
        self.map = ScreenMap(package=self.cfg.package)
        self.started = time.time()

    # ---- helpers ---------------------------------------------------------------------------------

    def fresh(self) -> None:
        self.device.force_stop()
        self.device.set_rotation(0)
        logcat.clear(self.adb)
        self.device.launch()
        self.device.set_rotation(0)
        for step in self.cfg.entry_steps:
            self.step(step)

    def settle(self, timeout: float = 12.0) -> tuple[ui.Screen, float, bool]:
        """Dump until two consecutive dumps match and no progress indicator is showing."""
        start = time.time()
        prev = ui.dump(self.adb)
        saw_progress = self._has_progress(prev)
        while time.time() - start < timeout:
            time.sleep(0.3)
            cur = ui.dump(self.adb)
            busy = self._has_progress(cur)
            saw_progress |= busy
            if cur.xml == prev.xml and not busy:
                return cur, round(time.time() - start, 1), saw_progress
            prev = cur
        return prev, round(time.time() - start, 1), saw_progress

    @staticmethod
    def _is_permission_prompt(screen: ui.Screen) -> bool:
        """The system grant dialog. Its package differs by image (com.android vs com.google.android)."""
        return any(PERMISSION_UI in n.package or PERMISSION_UI in n.rid for n in screen.nodes)

    def _has_progress(self, screen: ui.Screen) -> bool:
        return any(any(c in n.cls for c in PROGRESS_CLASSES) for n in screen.app_nodes(self.cfg.package))

    def clickables(self, screen: ui.Screen) -> list[ui.Node]:
        seen, out = set(), []
        for n in screen.app_nodes(self.cfg.package):
            if not n.clickable or n.editable or n.area == 0:
                continue
            g = _group(n)
            if g in seen:
                continue
            seen.add(g)
            out.append(n)
        return out[: self.max_actions]

    def _label(self, screen: ui.Screen, node: ui.Node) -> str:
        if node.text or node.desc:
            return node.text or node.desc
        x1, y1, x2, y2 = node.bounds
        inside = [n.text for n in screen.nodes if n.text and x1 <= n.center[0] <= x2 and y1 <= n.center[1] <= y2]
        return (" ".join(inside)[:60]) or node.rid or node.cls.rsplit(".", 1)[-1]

    def _title(self, screen: ui.Screen) -> str:
        texts = [n.text for n in screen.app_nodes(self.cfg.package) if n.text and not n.editable]
        return " / ".join(texts[:3])[:80]

    def register(self, screen: ui.Screen, path: list[str]) -> str:
        fp = screen.fingerprint(self.cfg.package)
        for sid, info in self.map.screens.items():
            if _similar(fp, frozenset(info.fingerprint)):
                return sid
        sid = "s" + hashlib.sha1("|".join(sorted(fp)).encode()).hexdigest()[:6]
        fields = [n.rid or f"EditText#{i}" for i, n in enumerate(screen.editable_fields(self.cfg.package))]
        self.map.screens[sid] = ScreenInfo(id=sid, path=path, title=self._title(screen), fields=fields, fingerprint=sorted(fp))
        return sid

    def go_to(self, info: ScreenInfo) -> ui.Screen:
        self.fresh()
        for step in info.path:
            self.step(step)
        if info.has_form:
            self.fill_form()
        screen, _, _ = self.settle()
        if not _similar(screen.fingerprint(self.cfg.package), frozenset(info.fingerprint)):
            raise NavigationError(f"replaying the path did not reach screen {info.id}")
        return screen

    # ---- exploration -----------------------------------------------------------------------------

    def explore(self) -> ScreenMap:
        if self.clear_data:
            self.device.clear_data()  # first-use state: permission prompts and onboarding show up again
        self.fresh()
        self.settle()
        root, self.baseline, _ = self.settle()  # already idle, so this measures pure dump overhead
        self.map.baseline_seconds = self.baseline
        queue = [self.register(root, [])]
        explored: set[str] = set()

        while queue and time.time() - self.started < self.budget:
            sid = queue.pop(0)
            if sid in explored:
                continue
            explored.add(sid)
            info = self.map.screens[sid]
            try:
                screen = self.go_to(info)
            except (NavigationError, AdbError) as e:
                self.notes.append(f"{sid}: {e}")
                continue
            info.actions = [ActionInfo(selector=_selector(n), label=self._label(screen, n), group=_group(n))
                            for n in self.clickables(screen)]
            for action in info.actions:
                if time.time() - self.started > self.budget:
                    break
                new_sid = self.try_action(info, action)
                if new_sid and new_sid not in explored and new_sid not in queue and len(self.map.screens) <= self.max_screens:
                    queue.append(new_sid)

        self.map.seconds = round(time.time() - self.started, 1)
        try:
            self.device.restore()
        except AdbError as e:
            self.notes.append(f"restore: {e}")
        return self.map

    def try_action(self, info: ScreenInfo, action: ActionInfo) -> str | None:
        try:
            screen = self.go_to(info)
        except (NavigationError, AdbError) as e:
            action.effect = "not_found"
            self.notes.append(f"{info.id} -> {action.label}: {e}")
            return None
        node = screen.find(action.selector) if not action.selector.startswith("at:") else None
        if node is None and not action.selector.startswith("at:"):
            action.effect = "not_found"
            return None
        granted_before = self.device.runtime_permissions()
        logcat.clear(self.adb)
        self.step(f"tap {action.selector}")
        after, seconds, progress = self.settle()
        action.settle_seconds = round(max(0.0, seconds - self.baseline), 1)  # time beyond dump overhead
        action.showed_progress = progress

        if crashes := self.crashes():
            action.effect, action.crash = "crash", crashes[0].signature(self.cfg.package)
            self.map.crashes.append(f"{info.id} -> {action.label}: {action.crash}")
            return None

        if self._is_permission_prompt(after):
            action.effect = "permission"
            self.accept_permission_dialog()
            after, _, _ = self.settle()
            granted = self.device.runtime_permissions()
            action.permissions = [p for p, g in granted.items() if g and not granted_before.get(p)]
            return None

        if any(after.find(b) for b in DIALOG_BUTTONS) and not any(screen.find(b) for b in DIALOG_BUTTONS):
            action.effect, action.dialog_dismiss = "dialog", "tap android:id/button1"
            self.step("tap android:id/button1")
            after, _, _ = self.settle()

        if after.foreground_package() not in ("", self.cfg.package):
            action.effect = "left_app"
            self.adb.shell("input keyevent 4")
            return None

        if _similar(after.fingerprint(self.cfg.package), screen.fingerprint(self.cfg.package)):
            if action.effect != "dialog":
                action.effect = "same_screen"
            return None

        path = list(info.path) + (["fill"] if info.has_form else []) + [f"tap {action.selector}"]
        if action.dialog_dismiss:
            path.append(action.dialog_dismiss)
        new_sid = self.register(after, path)
        action.to_screen = new_sid
        if action.effect != "dialog":
            action.effect = "new_screen"
        return new_sid
