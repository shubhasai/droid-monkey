from droid_monkey.tools import leaks

REPORT = """Loading graph...
====================================
HEAP ANALYSIS RESULT
====================================
1 APPLICATION LEAKS

References underlined with "~~~" are likely causes.

12345 bytes retained by leaking objects
┬───
│ GC Root: System class
│
├─ org.wikipedia.WikipediaApp class
│    Leaking: NO (a class is never leaking)
│    ↓ static WikipediaApp.listeners
│                          ~~~~~~~~~
├─ java.util.ArrayList instance
│    Leaking: UNKNOWN
│    ↓ ArrayList[0]
│               ~~~
╰→ org.wikipedia.main.MainActivity instance
     Leaking: YES (Activity#mDestroyed is true)
====================================
0 LIBRARY LEAKS
====================================
METADATA

Build.VERSION.SDK_INT: 36
"""


def test_trim_and_leaking_classes():
    trace = leaks._trim(REPORT)
    assert trace.startswith("HEAP ANALYSIS RESULT") and "METADATA" not in trace and "SDK_INT" not in trace
    assert leaks.leaking_classes(trace) == ["org.wikipedia.main.MainActivity"]


def test_destroyed_from_leakcanary_log():
    class FakeAdb:
        def run(self, *a, **k):
            return ("D LeakCanary: Watching instance of androidx.lifecycle.ReportFragment (androidx.lifecycle.ReportFragment received Fragment#onDestroy() callback) with key a\n"
                    "D LeakCanary: Watching instance of org.wikipedia.main.MainActivity (org.wikipedia.main.MainActivity received Activity#onDestroy() callback) with key b\n"
                    "D LeakCanary: Watching instance of org.wikipedia.main.MainActivity (org.wikipedia.main.MainActivity received Activity#onDestroy() callback) with key c\n"
                    "D LeakCanary: Watching instance of androidx.compose.ui.platform.ComposeView (org.wikipedia.feed.HomeFragment received Fragment#onDestroyView() callback (references to its views should be cleared)) with key d\n")
    assert leaks.destroyed(FakeAdb()) == ["MainActivity (Activity#onDestroy) x2", "HomeFragment (Fragment#onDestroyView)"]
