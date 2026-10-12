from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw

from droid_monkey import attention, perf_impact
from droid_monkey.ux import Finding, check_observed

PKG = "com.ex"


def _screen(tmp_path: Path, cta_color: str) -> tuple[Path, Path]:
    img = Image.new("RGB", (1080, 2400), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 300, 1080, 1300), fill="#335599")           # a big hero image
    d.rectangle((90, 2100, 990, 2250), fill=cta_color)            # the primary button
    png, xml = tmp_path / "s.png", tmp_path / "s.xml"
    img.save(png)
    xml.write_text(
        f'<hierarchy><node class="android.widget.FrameLayout" package="{PKG}" bounds="[0,100][1080,2400]">'
        f'<node class="android.widget.ImageView" package="{PKG}" content-desc="Hero" bounds="[0,300][1080,1300]"/>'
        f'<node class="android.widget.TextView" text="Welcome" package="{PKG}" bounds="[90,1400][990,1500]"/>'
        f'<node class="android.widget.TextView" text="Skip" clickable="true" package="{PKG}" bounds="[40,2300][240,2380]"/>'
        f'<node class="android.widget.Button" text="Continue" clickable="true" package="{PKG}" bounds="[90,2100][990,2250]"/>'
        f'</node></hierarchy>')
    return png, xml


def test_attention_finds_the_primary_action_and_sums_to_one(tmp_path):
    png, xml = _screen(tmp_path, "#d11a2a")
    att, heat = attention.predict(png, xml, PKG, "s")
    assert abs(heat.sum() - 1) < 1e-6
    assert att.primary == "Continue" and att.primary_rank == 1
    assert {e.label for e in att.elements} == {"Continue", "Skip"}
    out = attention.overlay(png, heat, att, tmp_path / "o.jpg")
    assert out.exists()


def test_a_washed_out_primary_action_draws_less(tmp_path):
    strong, _ = attention.predict(*_screen(tmp_path, "#d11a2a"), PKG)
    faint, _ = attention.predict(*_screen(tmp_path, "#f4f4f4"), PKG)
    assert faint.primary_share < strong.primary_share


def test_nav_bar_items_are_not_the_primary_action():
    els = [attention.ElementShare(label=x, share=0.1, bounds=(i * 200, 2200, i * 200 + 180, 2300))
           for i, x in enumerate(["Home", "Save", "Search", "More"])]
    assert len(attention._bars(els)) == 4


def test_routes_and_bands(tmp_path):
    from droid_monkey.explorer import ActionInfo, ScreenInfo, ScreenMap
    smap = ScreenMap(package=PKG, screens={
        "home": ScreenInfo(id="home", path=[], title="Home", fields=[], fingerprint=[],
                           actions=[ActionInfo(selector="text:Open", label="Open", group="", effect="new_screen",
                                               to_screen="page", settle_seconds=1.4)]),
        "page": ScreenInfo(id="page", path=["tap text:Open"], title="Page", fields=[], fingerprint=[])},
        edges=[{"from": "page", "to": "home", "action": "Back"}])
    rs = perf_impact.routes(tmp_path, smap)
    assert rs["page"].wait_s == 1.4 and rs["page"].slow()[0].band == "breaks flow"
    assert perf_impact.band(0.05) == "instant" and perf_impact.band(12) == "loses attention"


def test_code_only_findings_are_suspected_and_low():
    f = check_observed(Finding(id="x", title="t", flow="f", screens=["menu"], problem="p",
                               evidence=["Code, Login.kt:12: resets state"], why="w", change="c", files=[],
                               severity="high"))
    assert not f.observed and f.severity == "low"
    g = check_observed(Finding(id="y", title="t", flow="f", screens=["home"], problem="p",
                               evidence=["home: the Next button is 32 dp"], why="w", change="c", files=[]))
    assert g.observed and g.severity == "medium"


def test_report_embeds_each_image_once_and_marks_suspected(tmp_path, monkeypatch):
    from droid_monkey import paths, ux_report
    base = tmp_path / ".droid-monkey"
    (base / "screens").mkdir(parents=True)
    (base / "attention").mkdir()
    png, _ = _screen(tmp_path, "#d11a2a")
    for d in ("screens/home.png", "attention/home.jpg"):
        Image.open(png).convert("RGB").save(base / d)
    monkeypatch.setattr(paths, "data_dir", lambda app_dir: base)
    seen = Finding(id="a", title="Primary action easy to miss", flow="f", screens=["home"], problem="p",
                   evidence=["home: predicted attention puts Next at 2%", "home: the step took 1.4s"], why="w",
                   change="c", files=["app/src/main/Home.kt"], severity="high")
    sus = check_observed(Finding(id="b", title="State lost", flow="f", screens=[], problem="p",
                                 evidence=["Code, Edit.kt:12: resets"], why="w", change="c", files=[]))
    att, _ = attention.predict(png, _screen(tmp_path, "#d11a2a")[1], PKG, "home")
    page = ux_report.render(tmp_path, PKG, [seen, sus], {"model": "m", "cost_usd": 0.5}, {"home": att},
                            {"routes": {}, "slow_list": [{"label": "Open", "to_screen": "home", "from_screen": "home",
                                                          "seconds": 1.4, "band": "breaks flow"}]})
    assert page.count("data:image/jpeg;base64,") == 2          # the screenshot and the heatmap, once each
    assert 'data-img="#img0"' in page and "Suspected in code, not observed (1)" in page
    assert "predicted attention" in page and "measured" in page and "1.4s" in page
