"""Run any validated attack spec: replay its start screen, then execute its steps with the building blocks."""

import time

from ..config import Scenario, Target
from ..observe import NO_FEEDBACK, SURVIVED, Verdict, classify
from ..specs import Spec
from ..tools import ui
from .base import Attack, NavigationError


class SpecAttack(Attack):
    def __init__(self, ctx, spec: Spec):
        self.name = spec.name
        super().__init__(ctx)
        self.spec = spec

    def execute(self, scenario: Scenario):
        spec = self.spec
        start = self.ctx.targets.get(spec.screen)
        if start is None:
            raise NavigationError(f"screen {spec.screen} is not in the screen map; run `droid-monkey explore`")
        # Specs fill forms explicitly with a `fill` step, so the start screen is only navigated to.
        self.ctx.targets[f"spec:{spec.name}"] = Target(name=spec.screen, steps=list(start.steps), fill=False)
        self.prepare(Scenario(target=f"spec:{spec.name}"), clear_data=spec.clear_data)
        start_screen = self.snapshot("before")

        before: dict[str, str] = {}
        texts_before: set[str] | None = None
        for i, step in enumerate(spec.steps, 1):
            if "fault" in step:
                if "keeps_fields" in spec.expect and not before:
                    before = self.fields()
                if "shows_feedback" in spec.expect and texts_before is None:
                    texts_before = self.visible_texts(ui.dump(self.adb))
                self.notes.append(f"step {i}: fault {step['fault']}")
                self.apply_fault(step)
            else:
                (key, value), = step.items()
                self.apply_action(key, value)

        crashes = self.crashes()
        end_screen = self.snapshot("after")
        after = self.fields() if (before and not crashes) else {}
        restored = True
        if "returns_to_screen" in spec.expect:
            a, b = start_screen.fingerprint(self.cfg.package), end_screen.fingerprint(self.cfg.package)
            restored = not a or len(a & b) / max(1, len(a | b)) >= 0.5
        verdict = classify(crashes, before, after, screen_restored=restored)

        if verdict.outcome == SURVIVED and texts_before is not None:
            new = self.visible_texts(end_screen) - texts_before
            if not new:
                verdict = Verdict(NO_FEEDBACK, "after the trigger the app showed nothing new: no error, retry or message")
        return verdict, before, after

    def visible_texts(self, screen: ui.Screen) -> set[str]:
        return {n.text or n.desc for n in screen.app_nodes(self.cfg.package) if (n.text or n.desc) and not n.editable}

    def apply_action(self, key: str, value) -> None:
        if key == "tap":
            self.step(f"tap {value}")
        elif key == "fill" and value:
            self.fill_form()
        elif key == "wait":
            time.sleep(float(value))
        elif key == "settle":
            self.settle_screen(float(value))
        elif key == "accept_permission" and value:
            self.accept_permission_dialog()
        elif key == "back" and value:
            self.adb.shell("input keyevent 4")
        elif key == "home" and value:
            self.device.home()
        elif key == "restart_to_screen" and value:
            self.device.force_stop()
            self.device.launch()
            self.device.set_rotation(0)
            for step in self.cfg.entry_steps:
                self.step(step)
            self.navigate(self.ctx.targets[f"spec:{self.spec.name}"])

    def apply_fault(self, step: dict) -> None:
        d, name = self.device, step["fault"]
        if name == "rotate":
            d.flip_rotation()
        elif name == "network_off":
            d.set_network(False)
        elif name == "network_on":
            d.set_network(True)
        elif name == "airplane_on":
            d.set_airplane(True)
        elif name == "airplane_off":
            d.set_airplane(False)
        elif name == "kill_in_background":
            d.kill_in_background()
        elif name == "relaunch":
            d.launch()
        elif name == "background":
            d.background(float(step["seconds"]))
        elif name == "revoke_permission":
            d.revoke(step["permission"])
        elif name == "dark_mode":
            d.set_dark_mode(bool(step["enabled"]))
        elif name == "font_scale":
            d.set_font_scale(float(step["scale"]))
        elif name == "locale":
            d.set_app_locale(step["locale"])
        elif name == "trim_memory":
            d.trim_memory(step["level"])
        else:  # the validator makes this unreachable
            raise NavigationError(f"unknown fault {name}")
