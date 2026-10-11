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
    replay: AttackResult | None = None
    test: gradle.TestResult | None = None


@tracing.span(kind="TOOL", name="verify")
def verify(ctx: Context, spec: Spec, test_files: list[str]) -> VerifyResult:
    cfg = ctx.cfg
    build = gradle.build(cfg.app_dir, cfg.module, cfg.variant)
    if not build.ok or not build.apk:
        return VerifyResult(False, "the fix does not build", False, build.seconds, build.log_tail)
    gradle.install(ctx.adb, build.apk)

    replay = SpecRunner(ctx, spec).run()
    replay_ok = replay.outcome == "survived"

    test = None
    if test_files:
        test = gradle.run_test(cfg.app_dir, cfg.module, cfg.variant, cfg.app_dir / test_files[0])
        if test.kind == "instrumented":
            gradle.install(ctx.adb, build.apk)  # connected tests uninstall the app when they finish
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
    return VerifyResult(replay_ok and test_ok, reason, True, build.seconds, "", replay, test)


def build_failure_note(v: VerifyResult) -> str:
    return f"The project no longer builds:\n{v.build_log[-2000:]}"
