from droid_monkey import hunt
from droid_monkey.explorer import ScreenMap, ScreenInfo
from droid_monkey.tools import ui

PKG = "com.shubhasai.monkeymart"

LOG = """\
10-12 14:20:01.100  4321  4321 I ActivityTaskManager: Displayed com.shubhasai.monkeymart/.MainActivity
10-12 14:20:02.200  4321  4321 E Delivery: estimate failed
10-12 14:20:02.200  4321  4321 E Delivery: java.lang.NumberFormatException: For input string: ""
10-12 14:20:02.200  4321  4321 E Delivery: \tat java.lang.Integer.parseInt(Integer.java:627)
10-12 14:20:02.200  4321  4321 E Delivery: \tat com.shubhasai.monkeymart.data.DeliveryEstimator.days(DeliveryEstimator.kt:14)
10-12 14:20:02.200  4321  4321 E Delivery: \tat com.shubhasai.monkeymart.ui.CheckoutScreenKt.CheckoutScreen(CheckoutScreen.kt:58)
10-12 14:20:02.300  4321  4321 I Choreographer: Skipped 72 frames!  The application may be doing too much work on its main thread.
10-12 14:20:02.400  999   999  E OtherApp: java.lang.IllegalStateException: not ours
10-12 14:20:02.400  999   999  E OtherApp: \tat com.other.Thing.run(Thing.kt:1)
10-12 14:20:02.500  4321  4377 W System.err: java.io.IOException: framework only
10-12 14:20:02.500  4321  4377 W System.err: \tat okhttp3.Call.execute(Call.kt:3)
"""


def test_logged_exception_jank_and_noise():
    issues = hunt.scan_logs(LOG, PKG, {"4321"})
    kinds = [i.kind for i in issues]
    assert kinds.count("logged_exception") == 1  # the other app's and the framework-only traces are not the app's bug
    exc = next(i for i in issues if i.kind == "logged_exception")
    assert "NumberFormatException" in exc.title and "DeliveryEstimator.kt:14" in exc.title and exc.severity == "high"
    assert any("Delivery" in e for e in exc.evidence)
    jank = next(i for i in issues if i.kind == "jank")
    assert "72 frames" in jank.title and jank.severity == "medium"


def _screen(*texts, editable=()):
    nodes = "".join(
        f'<node text="{t}" resource-id="t{n}" class="{"android.widget.EditText" if t in editable else "android.widget.TextView"}" '
        f'package="{PKG}" content-desc="" clickable="false" scrollable="false" focused="false" bounds="[0,{n * 100}][500,{n * 100 + 90}]"/>'
        for n, t in enumerate(texts))
    return ui.parse(f'<hierarchy rotation="0"><node class="android.widget.FrameLayout" package="{PKG}" '
                    f'bounds="[0,0][1080,2400]" text="" resource-id="" content-desc="">{nodes}</node></hierarchy>')


def test_screen_checks():
    issues = hunt.check_screen(_screen("Arrives in null days", "Total -₹54", "Nullable fields", "Hello %s", "null",
                                       editable=("null",)), PKG)
    titles = " | ".join(i.title for i in issues)
    assert "'null'" in titles and "'%s'" in titles and "negative amount" in titles.lower()
    assert sum(1 for i in issues if i.kind == "placeholder_text") == 2  # not "Nullable", not the user's own input
    assert hunt.check_screen(_screen("Banana Bunch", "₹60"), PKG) == []


def test_quotes_must_be_on_the_cited_step():
    texts = ["Total ₹48", "Banana Bunch"]
    assert hunt._quoted("total  ₹48", texts)
    assert not hunt._quoted("Total ₹54", texts) and not hunt._quoted("", texts)


def test_journeys_cover_every_screen_once():
    smap = ScreenMap(package=PKG, screens={
        "shop": ScreenInfo(id="shop", path=[], title="Shop", fields=[], fingerprint=[]),
        "detail": ScreenInfo(id="detail", path=["tap p1"], title="Detail", fields=[], fingerprint=[]),
        "checkout": ScreenInfo(id="checkout", path=["tap p1", "tap buy"], title="Checkout", fields=[], fingerprint=[]),
        "settings": ScreenInfo(id="settings", path=["tap menu"], title="Settings", fields=[], fingerprint=[]),
    })
    assert sorted(leaf for leaf, _ in hunt.journeys(smap)) == ["checkout", "settings"]


def _rows(*rows, cls="android.widget.TextView"):
    """Each row is a list of texts laid out side by side at the same height."""
    nodes = "".join(
        f'<node text="{t}" resource-id="r{r}c{c}" class="{cls}" package="{PKG}" content-desc="" clickable="false" '
        f'scrollable="false" focused="false" bounds="[{c * 500},{r * 100}][{c * 500 + 480},{r * 100 + 90}]"/>'
        for r, row in enumerate(rows) for c, t in enumerate(row))
    return ui.parse(f'<hierarchy rotation="0"><node class="android.widget.FrameLayout" package="{PKG}" '
                    f'bounds="[0,0][1080,2400]" text="" resource-id="" content-desc="">{nodes}</node></hierarchy>')


def test_discounts_are_not_negative_money_bugs():
    ok = hunt.check_screen(_rows(["Discount", "−₹50"], ["Coupon SAVE10 −$5"], ["Total", "₹450"]), PKG)
    assert not [i for i in ok if i.kind == "negative_amount"]
    bad = hunt.check_screen(_rows(["Total", "-₹54"]), PKG)
    assert [i.kind for i in bad if i.kind == "negative_amount"] == ["negative_amount"]


def test_content_words_are_not_placeholders():
    # an article list (repeated tappable rows in a scrolling list) mentioning "Null hypothesis"
    rows = "".join(f'<node class="android.widget.LinearLayout" clickable="true" package="{PKG}" bounds="[0,{i * 200}][1080,{i * 200 + 190}]">'
                   f'<node class="android.widget.TextView" text="{t}" package="{PKG}" bounds="[0,{i * 200}][1080,{i * 200 + 90}]"/></node>'
                   for i, t in enumerate(["Null hypothesis", "NaN (number)"]))
    screen = ui.parse(f'<hierarchy><node class="androidx.recyclerview.widget.RecyclerView" scrollable="true" package="{PKG}" '
                      f'bounds="[0,0][1080,2400]">{rows}</node></hierarchy>')
    assert not [i for i in hunt.check_screen(screen, PKG) if i.kind == "placeholder_text"]


def test_a_map_or_video_is_not_a_blank_screen():
    map_screen = ui.parse(f'<hierarchy><node class="android.widget.FrameLayout" package="{PKG}" bounds="[0,0][1080,2400]">'
                          f'<node class="org.maplibre.android.maps.MapView" package="{PKG}" bounds="[0,0][1080,2400]"/></node></hierarchy>')
    assert not [i for i in hunt.check_screen(map_screen, PKG) if i.kind == "blank_screen"]
    empty = ui.parse(f'<hierarchy><node class="android.widget.FrameLayout" package="{PKG}" bounds="[0,0][1080,2400]">'
                     f'<node class="android.view.View" package="{PKG}" bounds="[0,0][1080,2400]"/></node></hierarchy>')
    assert [i.kind for i in hunt.check_screen(empty, PKG)] == ["blank_screen"]


def test_launch_jank_needs_a_longer_freeze():
    log = "10-12 14:20:02.300  4321  4321 I Choreographer: Skipped 45 frames!  The application may be doing too much work.\n"
    assert [i.kind for i in hunt.scan_logs(log, PKG, {"4321"})] == ["jank"]
    assert hunt.scan_logs(log, PKG, {"4321"}, jank_frames=hunt.LAUNCH_JANK_FRAMES) == []


def test_hunt_is_offered_on_the_dashboard():
    from droid_monkey import jobs
    assert any("hunt" in names for _, names in jobs.GROUPS) and "hunt" in jobs.commands()
