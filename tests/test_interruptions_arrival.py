from pathlib import Path

import pytest

from droid_monkey import driver as driver_mod
from droid_monkey.config import Target
from droid_monkey.driver import NavigationError
from droid_monkey.tools import ui

PKG = "com.ex"
PROMO = (f'<hierarchy><node class="android.widget.FrameLayout" package="{PKG}" bounds="[0,0][1080,2400]">'
         f'<node class="android.view.View" clickable="true" package="{PKG}" bounds="[12,164][138,290]">'
         f'<node class="android.view.View" content-desc="Close" package="{PKG}" bounds="[43,195][106,258]"/></node>'
         f'<node class="android.widget.TextView" text="Our Year in Review is here" package="{PKG}" bounds="[100,1100][980,1300]"/>'
         f'<node class="android.view.View" clickable="true" package="{PKG}" bounds="[42,2194][1038,2320]">'
         f'<node class="android.widget.TextView" text="Explore" package="{PKG}" bounds="[400,2230][680,2290]"/></node>'
         f'</node></hierarchy>')
ARTICLE = (f'<hierarchy><node class="android.widget.FrameLayout" package="{PKG}" bounds="[0,0][1080,2400]">'
           f'<node class="android.widget.ImageButton" content-desc="Search" clickable="true" package="{PKG}" bounds="[900,100][1060,250]"/>'
           f'<node class="android.widget.TextView" text="Contents" package="{PKG}" bounds="[40,300][400,380]"/>'
           f'<node class="android.widget.ImageButton" content-desc="More options" clickable="true" package="{PKG}" bounds="[40,100][200,250]"/>'
           f'</node></hierarchy>')


def _driver(monkeypatch, screens):
    seq = iter(screens)
    current = {"s": ui.parse(next(seq))}
    tapped = []

    def tap(adb, n):
        tapped.append((n.text or n.desc or "?", n.bounds))
        current["s"] = ui.parse(next(seq))
    monkeypatch.setattr(driver_mod.ui, "tap", tap)
    monkeypatch.setattr(driver_mod.ui, "dump", lambda adb: current["s"])
    monkeypatch.setattr(driver_mod.time, "sleep", lambda s: None)
    d = driver_mod.AppDriver.__new__(driver_mod.AppDriver)
    d.adb, d.package, d.notes = None, PKG, []
    return d, tapped


def test_the_promo_is_closed_never_explored(monkeypatch):
    d, tapped = _driver(monkeypatch, [PROMO, ARTICLE])
    assert d.close_interruption(ui.parse(PROMO))
    assert tapped == [("?", (12, 164, 138, 290))]          # the X (labelled on its child), not 'Explore'


def test_arrival_closes_a_covering_promo_then_passes(monkeypatch):
    d, tapped = _driver(monkeypatch, [PROMO, ARTICLE])
    want = sorted(ui.parse(ARTICLE).fingerprint(PKG))
    d.check_arrival(Target(name="article", steps=[], fingerprint=want))
    assert len(tapped) == 1


def test_arrival_refuses_the_wrong_screen(monkeypatch):
    other = ARTICLE.replace("Contents", "Settings").replace("More options", "Navigate up").replace("Search", "Help")
    d, _ = _driver(monkeypatch, [other])
    with pytest.raises(NavigationError):
        d.check_arrival(Target(name="article", steps=[], fingerprint=sorted(ui.parse(ARTICLE).fingerprint(PKG))))


def test_maps_in_the_old_fingerprint_format_are_not_checked(monkeypatch):
    d, _ = _driver(monkeypatch, [PROMO])
    d.check_arrival(Target(name="x", steps=[], fingerprint=["android.widget.Button", "android:id/content"]))
