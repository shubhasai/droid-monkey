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

from .driver import AppDriver, Context, NavigationError
from .tools import logcat, ui
from .tools.adb import AdbError

DIALOG_BUTTONS = ("android:id/button1", "android:id/button2", "android:id/button3")


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
    description: str = ""
    files: list[str] = field(default_factory=list)  # source files behind this screen (Claude-driven exploration)

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
    edges: list[dict] = field(default_factory=list)      # {"from", "to", "action"}
    unreached: list[dict] = field(default_factory=list)  # screens known from the code but not reached, and why
    mode: str = "rules"                                  # rules | claude

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
                         seconds=raw.get("seconds", 0.0), baseline_seconds=raw.get("baseline_seconds", 0.0),
                         edges=raw.get("edges", []), unreached=raw.get("unreached", []), mode=raw.get("mode", "rules"))


def _similar(a: frozenset[str], b: frozenset[str], threshold: float = 0.6) -> bool:
    if not a and not b:
        return True
    return len(a & b) / max(1, len(a | b)) >= threshold


def _selector(node: ui.Node, screen: ui.Screen | None = None) -> str:
    if node.rid:
        return node.rid
    if node.editable and screen is not None:  # an input's text is its value, which changes: use its position
        fields = [n for n in screen.nodes if n.editable and n.area > 0]
        if node in fields:
            return f"field:{fields.index(node)}"
    if node.text:
        return f"text:{node.text}"
    if node.desc:
        return f"desc:{node.desc}"
    if screen is not None:  # Compose often puts the label on a child: an icon's description, a row's title
        x1, y1, x2, y2 = node.bounds
        inside = [n for n in screen.nodes if n is not node and n.area and n.area < node.area
                  and x1 <= n.center[0] <= x2 and y1 <= n.center[1] <= y2]
        desc = next((n.desc for n in inside if n.desc), "")
        if desc:
            return f"desc:{desc}"
        text = next((n.text for n in inside if n.text and not n.editable), "")
        if text:
            return f"text:{text}"
    x, y = node.center
    return f"at:{x},{y}"


def _group(node: ui.Node) -> str:
    """Repeated list items count once: item_1..item_8 -> item_#; id-less rows share their geometry."""
    if node.rid:
        return re.sub(r"\d+", "#", node.rid)
    x1, y1, x2, y2 = node.bounds
    return f"{node.cls}|{x1}|{x2 - x1}|{y2 - y1}"


class Explorer:
    """Breadth-first exploration through the AppDriver. Injects no faults."""

    def __init__(self, ctx: Context, max_screens: int = 12, max_actions: int = 10, budget_seconds: float = 900,
                 clear_data: bool = True):
        self.d = AppDriver(ctx, ctx.run_dir / "explore")
        self.adb, self.device, self.cfg, self.notes = ctx.adb, ctx.device, ctx.cfg, self.d.notes
        self.max_screens, self.max_actions, self.budget = max_screens, max_actions, budget_seconds
        self.clear_data = clear_data
        self.baseline = 0.0  # how long settle() takes on a screen that is not changing (dump overhead)
        self.map = ScreenMap(package=self.cfg.package)
        self.started = time.time()

    # ---- helpers ---------------------------------------------------------------------------------

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
        self._shoot(sid)
        return sid

    def _shoot(self, sid: str) -> None:
        """A picture of each screen as it was found, for the map and the report."""
        try:
            out = self.d.dir / "screens" / f"{sid}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(ui.screenshot(self.adb))
        except AdbError as e:
            self.notes.append(f"screenshot of {sid}: {e}")

    def go_to(self, info: ScreenInfo) -> ui.Screen:
        self.d.fresh(clear_data=True)  # paths are recorded from a clean data state
        for step in info.path:
            self.d.step(step)
        if info.has_form:
            self.d.fill_form()
        screen, _, _ = self.d.settle()
        if not _similar(screen.fingerprint(self.cfg.package), frozenset(info.fingerprint)):
            raise NavigationError(f"replaying the path did not reach screen {info.id}")
        return screen

    # ---- exploration -----------------------------------------------------------------------------

    def explore(self) -> ScreenMap:
        self.d.fresh(clear_data=self.clear_data)  # first-use state: permission prompts and onboarding show up again
        self.d.settle()
        root, self.baseline, _ = self.d.settle()  # already idle, so this measures pure dump overhead
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
            info.actions = [ActionInfo(selector=_selector(n, screen), label=self._label(screen, n), group=_group(n))
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
        self.d.step(f"tap {action.selector}")
        after, seconds, progress = self.d.settle()
        action.settle_seconds = round(max(0.0, seconds - self.baseline), 1)  # time beyond dump overhead
        action.showed_progress = progress

        if crashes := self.d.crashes():
            action.effect, action.crash = "crash", crashes[0].signature(self.cfg.package)
            self.map.crashes.append(f"{info.id} -> {action.label}: {action.crash}")
            return None

        if self.d.is_permission_prompt(after):
            action.effect = "permission"
            self.d.accept_permission_dialog()
            after, _, _ = self.d.settle()
            granted = self.device.runtime_permissions()
            action.permissions = [p for p, g in granted.items() if g and not granted_before.get(p)]
            return None

        if any(after.find(b) for b in DIALOG_BUTTONS) and not any(screen.find(b) for b in DIALOG_BUTTONS):
            ok = after.find("android:id/button1")
            label = f"|text:{ok.text.casefold()}" if ok and ok.text else ""
            action.effect, action.dialog_dismiss = "dialog", f"tap? android:id/button1{label}"
            self.d.step(action.dialog_dismiss)
            after, _, _ = self.d.settle()

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
