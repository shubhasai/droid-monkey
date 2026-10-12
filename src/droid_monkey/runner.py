"""Run one attack spec (built-in or custom): walk to its start screen, execute its steps with the building blocks,
judge the result in code, save evidence, and always restore the device so one attack never poisons the next."""

import json
import time
from dataclasses import asdict, dataclass, field

from .config import Target
from .driver import AppDriver, Context, NavigationError
from .observe import FROZE, LEAKED, NO_FEEDBACK, SLOW_START, SURVIVED, Verdict, classify
from .specs import Spec
from .tools import ui
from .tools.adb import AdbError


@dataclass
class AttackResult:
    attack: str
    screen: str
    outcome: str
    reason: str
    exception: str = ""
    crash_signature: str = ""
    app_frames: list[str] = field(default_factory=list)
    lost_fields: dict[str, list[str]] = field(default_factory=dict)
    before_fields: dict[str, str] = field(default_factory=dict)
    after_fields: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    metrics: dict[str, dict] = field(default_factory=dict)  # memory / frames / Perfetto analysis, when measured
    evidence_dir: str = ""
    package: str = ""
    seconds: float = 0.0


class SpecRunner:
    def __init__(self, ctx: Context, spec: Spec, label: str | None = None):
        """`label` keeps a replay's evidence apart from the attack's: <run>/<attack>/<label>/."""
        self.ctx, self.spec = ctx, spec
        self.driver = AppDriver(ctx, ctx.run_dir / spec.name / label if label else ctx.run_dir / spec.name)
        self.device = ctx.device

    def run(self) -> AttackResult:
        start = time.time()
        try:
            self.metrics: dict[str, dict] = {}
            verdict, before, after = self._execute()
        finally:
            try:
                self.device.restore()
            except AdbError as e:
                self.driver.notes.append(f"restore: {e}")
        result = AttackResult(
            attack=self.spec.name, screen=self.spec.screen, outcome=verdict.outcome, reason=verdict.reason,
            lost_fields={k: list(v) for k, v in verdict.lost_fields.items()},
            before_fields=before, after_fields=after, notes=self.driver.notes, metrics=self.metrics,
            evidence_dir=str(self.driver.dir), seconds=round(time.time() - start, 1), package=self.ctx.cfg.package,
        )
        package = self.ctx.cfg.package
        self.driver.dir.mkdir(parents=True, exist_ok=True)
        if verdict.crash:
            result.exception = verdict.crash.exception
            result.crash_signature = verdict.crash.signature(package)
            result.app_frames = verdict.crash.app_frames(package)[:8]
            (self.driver.dir / "logcat.txt").write_text(verdict.crash.raw)
        (self.driver.dir / "verdict.json").write_text(json.dumps(asdict(result), indent=2))
        return result

    def _start_screen(self) -> Target:
        # Specs fill forms explicitly with a `fill` step, so the start screen is only navigated to.
        t = self.driver.target(self.spec.screen)
        return Target(name=t.name, steps=list(t.steps), fill=False, fingerprint=list(t.fingerprint))

    def _execute(self) -> tuple[Verdict, dict[str, str], dict[str, str]]:
        d, spec = self.driver, self.spec
        self.device.restore()
        d.fresh(clear_data=True)  # same clean data state the screen's path was recorded from
        d.go_to(self._start_screen())
        d.check_arrival(self._start_screen())  # never attack a promo or a dialog that covered the screen
        start_screen = d.snapshot("before")
        screen_rec = self.device.record_start()
        if not screen_rec:
            d.notes.append("screen recording did not start")
        if "no_leak" in spec.expect:
            self.metrics["memory_before"] = self.device.memory()
        if "smooth" in spec.expect:
            self.device.reset_frames()  # navigation to the start screen is not judged
        trace_rec = self._start_trace() if spec.trace else None

        before: dict[str, str] = {}
        fields_screen: frozenset[str] | None = None
        texts_before: set[str] | None = None
        left_screen: frozenset[str] | None = None
        fault_shot = False
        for i, step in enumerate(spec.steps, 1):
            if "fault" in step:
                if not fault_shot:  # what the user had on screen when the fault hit (e.g. the filled form)
                    fault_shot = True
                    d.dismiss_interruption(ui.dump(d.adb))
                    try:
                        d.dir.mkdir(parents=True, exist_ok=True)
                        (d.dir / "fault.png").write_bytes(ui.screenshot(d.adb))
                    except AdbError as e:
                        d.notes.append(f"screenshot at the fault: {e}")
                if left_screen is None:  # the screen the user was on when the first fault hit
                    d.dismiss_interruption(ui.dump(d.adb))
                    left_screen = ui.dump(d.adb).fingerprint(d.package)
                if "keeps_fields" in spec.expect and not before:
                    before = d.fields()
                    fields_screen = ui.dump(d.adb).fingerprint(d.package)
                if "shows_feedback" in spec.expect and texts_before is None:
                    texts_before = self._visible_texts(ui.dump(d.adb))
                d.notes.append(f"step {i}: fault {step['fault']}")
                self._fault(step)
            else:
                (key, value), = step.items()
                self._action(key, value)

        trace_file = self._stop_trace(trace_rec) if trace_rec else None
        crashes = d.crashes()
        end_screen = d.snapshot("after")
        if screen_rec:
            try:
                if not self.device.record_stop(d.dir / "recording.mp4"):
                    d.notes.append("screen recording stopped early: nothing to save")
            except AdbError as e:
                d.notes.append(f"screen recording not saved: {e}")

        def same(a: frozenset[str] | None, b: frozenset[str]) -> bool:
            return not a or len(a & b) / max(1, len(a | b)) >= 0.5

        end_fp = end_screen.fingerprint(d.package)
        left = left_screen if left_screen is not None else start_screen.fingerprint(d.package)
        restored = same(left, end_fp) if "returns_to_screen" in spec.expect else True
        if before and not crashes and "returns_to_screen" not in spec.expect and not same(fields_screen, end_fp):
            # The trigger moved the app to another screen (e.g. Save went back to the list): that is navigation,
            # not lost input, so the fields are not compared.
            d.notes.append("the app left the form after the trigger; fields not compared")
            before = {}
        after = d.fields() if (before and not crashes) else {}
        verdict = classify(crashes, before, after, screen_restored=restored)

        if verdict.outcome == SURVIVED and texts_before is not None:
            if not (self._visible_texts(end_screen) - texts_before):
                verdict = Verdict(NO_FEEDBACK, "after the trigger the app showed nothing new: no error, retry or message")
        if "smooth" in spec.expect:  # measured even after a crash or ANR: the chart shows how bad it got
            frozen = self._judge_frames()
            if verdict.outcome == SURVIVED:
                verdict = frozen or Verdict(SURVIVED, verdict.reason + ", no frozen frames")
        if verdict.outcome == SURVIVED and "fast_start" in spec.expect:
            verdict = self._judge_startup() or Verdict(SURVIVED, verdict.reason + ", starts within Play vitals limits")
        if verdict.outcome == SURVIVED and "no_leak" in spec.expect:
            verdict = self._judge_memory() or Verdict(SURVIVED, verdict.reason + ", no leaked Activities")
        if trace_file:
            self._analyse_trace(trace_file)
        return verdict, before, after

    def _start_trace(self):
        from .tools import perfetto
        try:
            return perfetto.start(self.driver.adb, self.driver.package, self.driver.dir / "perfetto-config.pbtxt")
        except AdbError as e:  # tracing is evidence, never a reason to fail the attack
            self.driver.notes.append(f"perfetto: not recorded ({e})")
            return None

    def _stop_trace(self, recording):
        try:
            return recording.stop(self.driver.dir / "trace.perfetto-trace")
        except AdbError as e:
            self.driver.notes.append(f"perfetto: trace not saved ({e})")
            return None

    def _analyse_trace(self, trace_file) -> None:
        from .tools import perfetto
        try:
            analysis = perfetto.analyse(trace_file, self.driver.package)
        except Exception as e:  # trace_processor missing or the trace unreadable: keep the raw trace
            self.driver.notes.append(f"perfetto: analysis failed ({str(e)[:200]})")
            return
        self.metrics["trace"] = analysis.to_dict()
        summary = analysis.summary()
        (self.driver.dir / "trace-analysis.txt").write_text(summary + f"\n\ntrace file: {trace_file}\n"
                                                            "open it at https://ui.perfetto.dev\n")
        self.driver.notes.append("perfetto: " + summary.replace("\n", "\n  "))

    def _judge_frames(self) -> Verdict | None:
        f = self.metrics["frames"] = self.device.frames()
        self.driver.notes.append(f"frames: {f['total']:.0f} rendered, {f['frozen']} frozen (>700 ms), "
                                 f"{f['janky_pct']:.0f}% janky, {f['slow_ui_thread']:.0f} slow UI-thread, p99 {f['p99_ms']:.0f} ms")
        if f["frozen"]:
            return Verdict(FROZE, f"{f['frozen']} frozen frame(s) over 700 ms and {f['slow_ui_thread']:.0f} slow UI-thread "
                                  f"frames (p99 {f['p99_ms']:.0f} ms): something blocks the main thread")
        return None

    # Google Play vitals: a cold start of 5 s or more, or a hot start of 1.5 s or more, is excessive.
    START_LIMITS_MS = {"cold": 5000, "hot": 1500}

    def _judge_startup(self) -> Verdict | None:
        from statistics import median
        runs = self.metrics.get("startup", {})
        slow = []
        for kind, limit in self.START_LIMITS_MS.items():
            times = [r["ms"] for r in runs.get(kind, [])]
            if not times:
                continue
            med = median(times)
            self.driver.notes.append(f"{kind} start: median {med:.0f} ms over {len(times)} runs {times} (limit {limit} ms)")
            if med >= limit:
                slow.append(f"{kind} start median {med:.0f} ms (limit {limit} ms, runs {times})")
        if slow:
            return Verdict(SLOW_START, "; ".join(slow) + ": work on the launch path delays the first frame")
        return None

    def _judge_memory(self) -> Verdict | None:
        before = self.metrics["memory_before"]
        after = self.metrics["memory_after"] = self.device.memory()
        grew = after["java_heap_kb"] - before["java_heap_kb"]
        self.driver.notes.append(f"memory after GC: {before['activities']} -> {after['activities']} Activities, "
                                 f"Java heap {before['java_heap_kb'] // 1024} -> {after['java_heap_kb'] // 1024} MB"
                                 + ("" if after["gc"] else " (GC not forced: build is not debuggable)"))
        if after["activities"] > before["activities"]:
            self._find_leak()
            return Verdict(LEAKED, f"{after['activities'] - before['activities']} destroyed Activity instance(s) still alive "
                                   f"after a forced GC ({before['activities']} -> {after['activities']}): something keeps a "
                                   "reference to an old Activity")
        if grew > 16 * 1024 and grew > before["java_heap_kb"] / 2:
            return Verdict(LEAKED, f"Java heap grew {grew // 1024} MB ({before['java_heap_kb'] // 1024} -> "
                                   f"{after['java_heap_kb'] // 1024} MB) after a forced GC: memory is not released")
        return None

    def _find_leak(self) -> None:
        """What holds the leaked screen: Shark's reference chain from a heap dump (see tools/leaks.py)."""
        from dataclasses import asdict
        from .tools import leaks
        rep = leaks.analyse(self.driver.adb, self.driver.package, self.driver.dir)
        self.metrics["leak"] = asdict(rep)
        if rep.trace:
            (self.driver.dir / "leak-trace.txt").write_text(rep.trace + "\n")
            self.driver.notes.append("leak trace (Shark): " + (", ".join(rep.leaking) or "see leak-trace.txt"))
        if rep.note:
            self.driver.notes.append(f"leak: {rep.note}")

    def _visible_texts(self, screen: ui.Screen) -> set[str]:
        return {n.text or n.desc for n in screen.app_nodes(self.driver.package) if (n.text or n.desc) and not n.editable}

    def _action(self, key: str, value) -> None:
        d = self.driver
        if key == "tap":
            d.step(f"tap {value}")
        elif key == "fill" and value:
            d.fill_form()
        elif key == "wait":
            time.sleep(float(value))
        elif key == "settle":
            d.settle(float(value))
        elif key == "accept_permission" and value:
            d.accept_permission_dialog()
        elif key == "back" and value:
            d.adb.shell("input keyevent 4")
        elif key == "scroll":
            d.scroll(value)
        elif key == "poke" and value:
            self.device.poke()
        elif key == "home" and value:
            self.device.home()
        elif key == "restart_to_screen" and value:
            d.fresh()
            d.go_to(self._start_screen())

    def _fault(self, step: dict) -> None:
        dev, name = self.device, step["fault"]
        if name == "rotate":
            dev.flip_rotation()
        elif name == "network_off":
            dev.set_network(False)
        elif name == "network_on":
            dev.set_network(True)
        elif name == "airplane_on":
            dev.set_airplane(True)
        elif name == "airplane_off":
            dev.set_airplane(False)
        elif name == "kill_in_background":
            dev.kill_in_background()
        elif name == "relaunch":
            dev.launch()
        elif name == "background":
            dev.background(float(step["seconds"]))
        elif name == "revoke_permission":
            dev.revoke(step["permission"])
        elif name == "dark_mode":
            dev.set_dark_mode(bool(step["enabled"]))
        elif name == "font_scale":
            dev.set_font_scale(float(step["scale"]))
        elif name == "locale":
            dev.set_app_locale(step["locale"])
        elif name == "trim_memory":
            dev.trim_memory(step["level"])
        elif name == "recreate_activity":
            dev.recreate_activity(int(step["times"]))
        elif name in ("cold_start", "hot_start"):
            kind = name.split("_")[0]
            for _ in range(int(step["runs"])):
                ms, state = dev.start_time(kind)
                self.metrics.setdefault("startup", {}).setdefault(kind, []).append({"ms": ms, "state": state})
        elif name == "cpu_stress":
            dev.cpu_stress(int(step["threads"]))
        else:  # the validator makes this unreachable
            raise NavigationError(f"unknown fault {name}")
