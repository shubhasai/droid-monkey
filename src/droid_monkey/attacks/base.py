"""Attack lifecycle shared by every fault: prepare -> snapshot -> inject -> observe -> restore (always)."""

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import AppConfig, Scenario, Target
from ..observe import Verdict
from ..tools import logcat, ui
from ..tools.adb import Adb, AdbError
from ..tools.device import Device

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

PERMISSION_ALLOW_IDS = [
    "com.android.permissioncontroller:id/permission_allow_foreground_only_button",
    "com.android.permissioncontroller:id/permission_allow_button",
    "com.android.permissioncontroller:id/permission_allow_one_time_button",
]


class NavigationError(AdbError):
    pass


@dataclass
class AttackResult:
    attack: str
    target: str
    action: str | None
    outcome: str
    reason: str
    exception: str = ""
    crash_signature: str = ""
    app_frames: list[str] = field(default_factory=list)
    lost_fields: dict[str, list[str]] = field(default_factory=dict)
    before_fields: dict[str, str] = field(default_factory=dict)
    after_fields: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    evidence_dir: str = ""
    seconds: float = 0.0


@dataclass
class Context:
    adb: Adb
    device: Device
    cfg: AppConfig
    run_dir: Path
    targets: dict[str, Target] = field(default_factory=dict)  # screens from the explorer's map


class Attack:
    name = ""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.adb, self.device, self.cfg = ctx.adb, ctx.device, ctx.cfg
        self.dir = ctx.run_dir / self.name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.notes: list[str] = []

    # ---- the template every attack follows -------------------------------------------------------

    def run(self, scenario: Scenario) -> AttackResult:
        start = time.time()
        try:
            verdict, before, after = self.execute(scenario)
        finally:
            try:
                self.device.restore()
            except AdbError as e:
                self.notes.append(f"restore: {e}")
        result = AttackResult(
            attack=self.name, target=scenario.target, action=scenario.action,
            outcome=verdict.outcome, reason=verdict.reason,
            lost_fields={k: list(v) for k, v in verdict.lost_fields.items()},
            before_fields=before, after_fields=after, notes=self.notes,
            evidence_dir=str(self.dir), seconds=round(time.time() - start, 1),
        )
        if verdict.crash:
            result.exception = verdict.crash.exception
            result.crash_signature = verdict.crash.signature(self.cfg.package)
            result.app_frames = verdict.crash.app_frames(self.cfg.package)[:8]
            (self.dir / "logcat.txt").write_text(verdict.crash.raw)
        (self.dir / "verdict.json").write_text(json.dumps(asdict(result), indent=2))
        return result

    def execute(self, scenario: Scenario) -> tuple[Verdict, dict[str, str], dict[str, str]]:
        raise NotImplementedError

    # ---- shared steps ----------------------------------------------------------------------------

    def target(self, scenario: Scenario) -> Target:
        found = self.ctx.targets.get(scenario.target) or self.cfg.targets.get(scenario.target)
        if found is None:
            raise NavigationError(f"unknown target screen '{scenario.target}': run `droid-monkey explore` first")
        return found

    def prepare(self, scenario: Scenario, clear_data: bool = False) -> Target:
        """Clean device, fresh process, then walk to the target screen."""
        self.device.restore()
        self.device.force_stop()
        if clear_data:
            self.device.clear_data()
        self.device.set_rotation(0)
        logcat.clear(self.adb)
        self.device.launch()
        self.device.set_rotation(0)  # Android 16 may restore the app's last orientation on launch
        for step in self.cfg.entry_steps:
            self.step(step)
        target = self.target(scenario)
        self.navigate(target)
        return target

    def navigate(self, target: Target) -> None:
        for step in target.steps:
            self.step(step)
        if target.fill:
            self.fill_form()

    def step(self, step: str) -> None:
        """One step: `tap <selector>`, `tap? <selector>` (if present), `wait <selector>`, `fill`, `back`, `sleep <s>`.

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
        elif verb == "fill":
            self.fill_form()
        elif verb == "back":
            self.adb.shell("input keyevent 4")
        elif verb == "sleep":
            time.sleep(float(arg))
        else:
            raise NavigationError(f"unknown step '{step}'")

    def fire(self, action: str | None) -> None:
        """Perform the scenario's trigger (a step, usually a tap) right before or after the fault."""
        if action:
            self.step(action)

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
        done: set[tuple] = set()
        for _ in range(max_pages):
            progressed = False
            while True:
                screen = ui.dump(self.adb)
                pending = [n for n in screen.editable_fields(self.cfg.package)
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
            scroller = ui.dump(self.adb).scrollable(self.cfg.package)
            if scroller is None:
                break
            before = ui.dump(self.adb).xml
            ui._swipe(self.adb, scroller.bounds, up=True)
            if ui.dump(self.adb).xml == before and not progressed:
                break

    def snapshot(self, label: str) -> ui.Screen:
        screen = ui.dump(self.adb)
        (self.dir / f"{label}.xml").write_text(screen.xml)
        try:
            (self.dir / f"{label}.png").write_bytes(ui.screenshot(self.adb))
        except AdbError as e:
            self.notes.append(f"screenshot {label}: {e}")
        return screen

    def fields(self) -> dict[str, str]:
        return ui.collect_fields(self.adb, self.cfg.package)

    def accept_permission_dialog(self, timeout: float = 4.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            screen = ui.dump(self.adb)
            for rid in PERMISSION_ALLOW_IDS:
                node = screen.find(rid)
                if node:
                    ui.tap(self.adb, node)
                    return True
            time.sleep(0.3)
        return False

    def crashes(self) -> list[logcat.Crash]:
        return logcat.read_crashes(self.adb, self.cfg.package)

    @staticmethod
    def slug(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
