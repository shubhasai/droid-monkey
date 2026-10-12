"""Memory and frame numbers come from dumpsys text; the verdicts depend on parsing them exactly."""

from droid_monkey.tools.device import Device

GFXINFO = """Total frames rendered: 330
Janky frames: 7 (2.12%)
99th percentile: 77ms
Number Slow UI thread: 3
HISTOGRAM: 5ms=1 16ms=300 650ms=2 700ms=1 1500ms=2 4950ms=0
"""

MEMINFO = """ App Summary
           Java Heap:    14876                          28520
 Objects
         AppContexts:        7           Activities:        3
"""


class FakeAdb:
    def __init__(self, outputs: dict[str, str]):
        self.outputs, self.calls = outputs, []

    def shell(self, cmd: str, check: bool = True, **kw) -> str:
        self.calls.append(cmd)
        return next((out for key, out in self.outputs.items() if key in cmd), "")


def test_frozen_frames_count_every_bucket_from_700ms():
    f = Device(FakeAdb({"gfxinfo": GFXINFO}), "com.example").frames()
    assert {k: v for k, v in f.items() if k != "histogram"} == {"total": 330, "janky_pct": 2.12, "frozen": 3,
                                                                "slow_ui_thread": 3, "p99_ms": 77}
    assert f["histogram"] == [[5, 1], [16, 300], [650, 2], [700, 1], [1500, 2]]


def test_memory_forces_a_gc_then_reads_activities_and_heap(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    adb = FakeAdb({"meminfo": MEMINFO, "dumpheap": ""})
    assert Device(adb, "com.example").memory() == {"activities": 3, "java_heap_kb": 14876, "gc": 1}
    assert adb.calls[0].startswith("am dumpheap -g com.example")


def test_launch_time_and_state_come_from_am_start(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    out = "Status: ok\nLaunchState: COLD\nActivity: com.example/.Main\nTotalTime: 5946\nWaitTime: 5950\nComplete\n"
    adb = FakeAdb({"am start": out, "resolve-activity": "com.example/.Main"})
    assert Device(adb, "com.example").start_time("cold") == (5946, "COLD")
    assert "am force-stop com.example" in adb.calls


def test_a_hot_launch_is_not_accepted_as_cold(monkeypatch):
    import pytest
    from droid_monkey.tools.adb import AdbError
    monkeypatch.setattr("time.sleep", lambda s: None)
    adb = FakeAdb({"am start": "LaunchState: HOT\nTotalTime: 50\n", "resolve-activity": "com.example/.Main"})
    with pytest.raises(AdbError):
        Device(adb, "com.example").start_time("cold")


def test_debug_libraries_launcher_icons_are_skipped():
    from droid_monkey.tools.device import pick_launcher
    out = """2 activities found:
  Activity #0:
    org.wikipedia.dev/leakcanary.internal.activity.LeakLauncherActivity
  Activity #1:
    org.wikipedia.dev/org.wikipedia.DefaultIcon
"""
    assert pick_launcher("org.wikipedia.dev", out) == "org.wikipedia.dev/org.wikipedia.DefaultIcon"
