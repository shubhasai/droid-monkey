from droid_monkey import explorer
from droid_monkey.tools import ui

PKG = "com.ex"


def _feed(titles, extra_button=True):
    rows = "".join(
        f'<node class="android.view.View" clickable="true" package="{PKG}" bounds="[0,{200 + i * 300}][1080,{480 + i * 300}]">'
        f'<node class="android.widget.TextView" text="{t}" package="{PKG}" bounds="[20,{210 + i * 300}][900,{260 + i * 300}]"/>'
        + (f'<node class="android.widget.Button" content-desc="Save" clickable="true" package="{PKG}" '
           f'bounds="[950,{210 + i * 300}][1060,{300 + i * 300}]"/>' if extra_button else "")
        + '</node>' for i, t in enumerate(titles))
    return ui.parse(f'<hierarchy><node class="android.widget.FrameLayout" package="{PKG}" bounds="[0,0][1080,2400]">'
                    f'<node class="androidx.compose.ui.platform.ComposeView" scrollable="true" package="{PKG}" '
                    f'bounds="[0,100][1080,2400]">{rows}</node></node></hierarchy>')


def test_a_data_row_is_selected_by_position_and_survives_new_content():
    explorer.UI_LABELS.clear()
    today = _feed(["Gotland Grand National", "Poecilia vandepolli", "Mars"])
    row = next(n for n in today.nodes if n.clickable and n.row == "#0:1")
    save = next(n for n in today.nodes if n.desc == "Save" and n.row == "#0:1")
    assert explorer._selector(row, today) == "item:#0:1"
    assert explorer._selector(save, today) == "item:#0:1:c1"
    tomorrow = _feed(["Apollo 11", "Ada Lovelace", "Jupiter"])     # the feed changed overnight
    assert tomorrow.find("item:#0:1").bounds == row.bounds
    assert tomorrow.find("item:#0:1:c1").desc == "Save"
    assert tomorrow.find("text:Poecilia vandepolli") is None          # what a text selector would have done


def test_rows_of_fixed_labels_keep_their_text():
    explorer.UI_LABELS.clear()
    explorer.UI_LABELS.update({"home feed", "app theme", "about the wikipedia app"})
    settings = _feed(["Home feed", "App theme", "About the Wikipedia app"], extra_button=False)
    row = next(n for n in settings.nodes if n.clickable and n.row == "#0:2")
    assert explorer._selector(row, settings).startswith("text:About")  # inner text, as before
    explorer.UI_LABELS.clear()


def test_unknown_or_out_of_range_rows():
    screen = _feed(["a", "b"])
    assert screen.find("item:#0:5") is None and screen.find("item:nope:0").bounds  # one list: the key can drift
    assert screen.find("item:garbage") is None
