"""An ANR is judged from Android's own record of it: the newest dropbox entry for the app, and its main thread."""

from droid_monkey.observe import ANR, classify
from droid_monkey.tools.logcat import Crash, parse_anr_trace, split_dropbox

DROPBOX = """Drop box contents: 3 entries
Searching for: data_app_anr

========================================
2026-10-12 03:00:00 data_app_anr (compressed text, 100 bytes)
Process: com.example
Subject: Input dispatching timed out (old)

"main" prio=5 tid=1 Sleeping
  at java.lang.Thread.sleep(Native method)
  at com.example.Old.click(Old.kt:1)

========================================
2026-10-12 04:13:25 data_app_anr (compressed text, 200 bytes)
Process: com.example
Subject: Input dispatching timed out (MainActivity is not responding. Waited 5000ms for MotionEvent)

"main" prio=5 tid=1 Sleeping
  | group="main" sCount=1
  at java.lang.Thread.sleep(Native method)
  at com.example.ui.ShopScreenKt$ShopScreen$1.invoke(ShopScreen.kt:56)
  at androidx.compose.foundation.ClickableNode.onClick(Clickable.kt:900)

"RenderThread" prio=7 tid=12 Native
  at libhwui.so
"""


def test_the_newest_anr_and_its_main_thread():
    entries = split_dropbox(DROPBOX)
    assert len(entries) == 2
    reason, frames = parse_anr_trace(entries[-1])
    assert reason.startswith("Input dispatching timed out (MainActivity")
    assert frames[1] == "com.example.ui.ShopScreenKt$ShopScreen$1.invoke(ShopScreen.kt:56)"
    assert not any("libhwui" in f for f in frames)   # other threads are not the main thread


def test_an_anr_is_its_own_verdict_and_a_crash_wins():
    anr = Crash(kind="anr", exception="ANR", message="Input dispatching timed out")
    crash = Crash(kind="crash", exception="java.lang.IllegalStateException", message="boom")
    assert classify([anr], {}, {}).outcome == ANR
    assert classify([anr, crash], {}, {}).outcome == "crashed"
