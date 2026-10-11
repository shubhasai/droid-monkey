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
    assert f == {"total": 330, "janky_pct": 2.12, "frozen": 3, "slow_ui_thread": 3, "p99_ms": 77}


def test_memory_forces_a_gc_then_reads_activities_and_heap(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    adb = FakeAdb({"meminfo": MEMINFO, "dumpheap": ""})
    assert Device(adb, "com.example").memory() == {"activities": 3, "java_heap_kb": 14876, "gc": 1}
    assert adb.calls[0].startswith("am dumpheap -g com.example")
