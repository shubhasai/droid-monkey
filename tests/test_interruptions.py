from types import SimpleNamespace

from droid_monkey import driver as driver_mod
from droid_monkey.tools import ui

XML = ('<hierarchy><node class="android.widget.FrameLayout" package="org.example" bounds="[0,0][100,1000]">'
       '<node class="android.widget.Button" text="Cancel" package="org.example" clickable="true" bounds="[0,0][50,50]"/></node>'
       '<node class="android.widget.FrameLayout" package="com.google.android.inputmethod.latin" bounds="[0,500][100,1000]">'
       '<node class="android.widget.TextView" text="Try out your stylus" package="com.google.android.inputmethod.latin" bounds="[0,500][100,550]"/>'
       '<node class="android.widget.Button" text="Cancel" package="com.google.android.inputmethod.latin" clickable="true" bounds="[0,900][50,950]"/>'
       '</node></hierarchy>')


def test_closes_keyboard_sheet_not_the_apps_own_cancel(monkeypatch):
    tapped = []
    monkeypatch.setattr(ui, "tap", lambda adb, n: tapped.append(n))
    monkeypatch.setattr(driver_mod.time, "sleep", lambda s: None)
    d = driver_mod.AppDriver.__new__(driver_mod.AppDriver)
    d.adb, d.notes = None, []
    assert d.dismiss_interruption(ui.parse(XML))
    assert [n.package for n in tapped] == ["com.google.android.inputmethod.latin"]
    tapped.clear()
    app_only = XML.split('<node class="android.widget.FrameLayout" package="com.google')[0] + "</hierarchy>"
    assert not d.dismiss_interruption(ui.parse(app_only)) and not tapped
