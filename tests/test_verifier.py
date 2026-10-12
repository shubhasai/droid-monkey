"""A passing replay and test is a fix, not a device failure."""

from types import SimpleNamespace

from droid_monkey import verifier


def _ctx(tmp_path):
    cfg = SimpleNamespace(app_dir=tmp_path, module="app", variant="debug", java_home=None, package="p")
    return SimpleNamespace(cfg=cfg, adb=None, device=None)


def _run(monkeypatch, tmp_path, outcome, test_ok):
    monkeypatch.setattr(verifier.gradle, "build", lambda *a, **k: SimpleNamespace(ok=True, apk="a.apk", seconds=1.0, log_tail=""))
    monkeypatch.setattr(verifier.gradle, "install", lambda *a, **k: None)
    monkeypatch.setattr(verifier.gradle, "run_test", lambda *a, **k: SimpleNamespace(
        ok=test_ok, kind="jvm", test_class="FooTest", log_tail="boom"))
    replay = SimpleNamespace(outcome=outcome, exception="", reason="r", crash_signature="")
    monkeypatch.setattr(verifier, "SpecRunner", lambda *a, **k: SimpleNamespace(run=lambda: replay))
    return verifier.verify(_ctx(tmp_path), spec=None, test_files=["app/src/test/FooTest.kt"])


def test_passing_fix_is_fixed_not_harness(monkeypatch, tmp_path):
    v = _run(monkeypatch, tmp_path, "survived", True)
    assert v.fixed and not v.harness
    assert v.replay.outcome == "survived" and v.test.ok


def test_failing_fix_is_not_harness(monkeypatch, tmp_path):
    v = _run(monkeypatch, tmp_path, "crashed", True)
    assert not v.fixed and not v.harness
