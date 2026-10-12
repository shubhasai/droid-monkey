"""The UX audit starts from facts measured in code: routes through the screen map, touch targets, labels."""

from droid_monkey import paths, ux
from droid_monkey.explorer import ActionInfo, ScreenInfo, ScreenMap

XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example" content-desc=""
 clickable="false" bounds="[0,0][1080,2400]">
 <node index="0" text="" resource-id="com.example:id/fab" class="android.widget.ImageButton" package="com.example"
  content-desc="" clickable="true" bounds="[900,2200][990,2290]"/>
 <node index="1" text="Search" resource-id="com.example:id/search" class="android.widget.Button" package="com.example"
  content-desc="" clickable="true" bounds="[0,100][1080,300]"/>
</node></hierarchy>"""


def test_shortest_route_through_the_map_beats_the_recorded_path(tmp_path):
    home = ScreenInfo("home", [], "Home", [], [], [ActionInfo("search", "Search", "g", effect="new_screen", to_screen="search")])
    search = ScreenInfo("search", ["tap a", "back", "tap b", "back", "tap search"], "Search", ["q"], [])
    smap = ScreenMap("com.example", {"home": home, "search": search})
    assert ux.shortest_taps(smap) == {"home": 0, "search": 1}

    shots = paths.screens_dir(tmp_path)
    shots.mkdir(parents=True)
    (shots / "home.xml").write_text(XML)
    facts = ux.measure(tmp_path, smap, density=420)
    assert facts["search"]["taps_recorded_path"] == 3 and facts["search"]["taps_from_launch_shortest"] == 1
    # the 90 px icon button is 34 dp at 420 dpi and has no label for a screen reader
    assert any(t.startswith("fab (34x34 dp)") for t in facts["home"]["touch_targets_under_48dp"])
    assert facts["home"]["unlabeled_buttons"] == ["fab"]
