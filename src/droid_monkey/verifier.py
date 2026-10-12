"""Verifier: rebuild, reinstall, replay the identical attack spec, run the new regression test. Code only.

An attack counts as fixed only when the replay survives AND the new test passes.
"""

from dataclasses import dataclass

from . import tracing
from .driver import Context
from .runner import AttackResult, SpecRunner
from .specs import Spec
from .tools import gradle


@dataclass
class VerifyResult:
    fixed: bool
    reason: str
    build_ok: bool
    build_seconds: float = 0.0
    build_log: str = ""
    harness: bool = False      # the device, not the fix, failed (e.g. no room to install the test)
    replay: AttackResult | None = None
    test: gradle.TestResult | None = None


@tracing.span(kind="TOOL", name="verify")
def verify(ctx: Context, spec: Spec, test_files: list[str], attempt: int = 1) -> VerifyResult:
    cfg = ctx.cfg
    build = gradle.build(cfg.app_dir, cfg.module, cfg.variant, java_home=cfg.java_home)
    if not build.ok or not build.apk:
        return VerifyResult(False, "the fix does not build", False, build.seconds, build.log_tail)
    gradle.install(ctx.adb, build.apk, package=cfg.package, source_dir=cfg.app_dir)

    replay = SpecRunner(ctx, spec, label=f"replay-{attempt}").run()  # the failure's own evidence stays as it was
    replay_ok = replay.outcome == "survived"

    test = None
    if test_files:
        test_file = cfg.app_dir / test_files[0]
        instrumented = "/androidTest/" in test_files[0]
        for attempt in range(2):
            if instrumented:
                ctx.device.make_room()
                from .tools import ui
                ui.release(ctx.adb)  # instrumented tests need the UI-automation slot
            test = gradle.run_test(cfg.app_dir, cfg.module, cfg.variant, test_file, java_home=cfg.java_home)
            if test.kind == "instrumented":
                gradle.install(ctx.adb, build.apk, package=cfg.package, source_dir=cfg.app_dir)  # connected tests uninstall the app when they finish
            if test.ok or not gradle.is_install_failure(test.log_tail):
                break
        if test and not test.ok and gradle.is_install_failure(test.log_tail):
            free = ctx.device.free_mb()
            return VerifyResult(False, f"the device could not install the test ({free} MB free on the emulator): "
                                       "free storage on the emulator, then resume the run", True, build.seconds, "",
                                harness=True, replay=replay, test=test)
    test_ok = bool(test and test.ok)

    if replay_ok and test_ok:
        reason = f"replay survived and {test.test_class} passed"
    elif not replay_ok:
        reason = f"replay still fails: {replay.outcome}: {replay.exception or replay.reason}"
        if replay.crash_signature:
            reason += f" at {replay.crash_signature}"
    elif test is None:
        reason = "replay survived but the fix added no regression test"
    else:
        reason = f"replay survived but {test.test_class} failed:\n{test.log_tail[-1500:]}"
    return VerifyResult(replay_ok and test_ok, reason, True, build.seconds, "", replay=replay, test=test)


def build_failure_note(v: VerifyResult) -> str:
    return f"The project no longer builds:\n{v.build_log[-2000:]}"
