from droid_monkey.tools import ui

PKG = "com.ex"


def _xml(titles: list[str], toolbar: str = "Articles", web: str = "") -> str:
    rows = "".join(
        f'<node class="android.widget.LinearLayout" resource-id="{PKG}:id/row" package="{PKG}" bounds="[0,{200 + i * 100}][100,{290 + i * 100}]" clickable="true">'
        f'<node class="android.widget.TextView" text="{t}" package="{PKG}" bounds="[0,{200 + i * 100}][100,{250 + i * 100}]"/></node>'
        for i, t in enumerate(titles))
    webview = (f'<node class="android.webkit.WebView" package="{PKG}" bounds="[0,800][100,900]">'
               f'<node class="android.view.View" text="{web}" package="{PKG}" bounds="[0,800][100,850]"/></node>') if web else ""
    return (f'<hierarchy><node class="android.widget.FrameLayout" package="{PKG}" bounds="[0,0][100,1000]">'
            f'<node class="android.widget.TextView" text="{toolbar}" package="{PKG}" bounds="[0,0][100,100]"/>'
            f'<node class="android.widget.ImageButton" content-desc="Search" package="{PKG}" bounds="[0,100][50,150]" clickable="true"/>'
            f'<node class="androidx.recyclerview.widget.RecyclerView" resource-id="{PKG}:id/list" package="{PKG}" bounds="[0,200][100,800]" scrollable="true">'
            f'{rows}</node>{webview}</node></hierarchy>')


def test_list_rows_and_web_pages_are_content():
    s = ui.parse(_xml(["Apollo 11", "Mars"], web="Some article text"))
    assert {n.text for n in s.nodes if n.content} == {"", "Apollo 11", "Mars", "Some article text"}
    assert not next(n for n in s.nodes if n.text == "Articles").content


def test_same_screen_with_different_content_has_one_fingerprint():
    a = ui.parse(_xml(["Apollo 11", "Mars", "Moon"], web="Article about the Moon")).fingerprint(PKG)
    b = ui.parse(_xml(["Python", "Java"], web="Article about Java")).fingerprint(PKG)
    assert a == b
    assert "text:Articles" in a and "desc:Search" in a and f"id:{PKG}:id/row" in a


def test_different_fixed_labels_still_differ():
    a = ui.parse(_xml(["x", "y"], toolbar="Articles")).fingerprint(PKG)
    b = ui.parse(_xml(["x", "y"], toolbar="Saved")).fingerprint(PKG)
    assert a != b


def test_single_item_scroll_content_is_not_a_list():
    # a ScrollView with one form inside: its fields and labels are the screen, not data
    xml = (f'<hierarchy><node class="android.widget.ScrollView" package="{PKG}" bounds="[0,0][100,1000]" scrollable="true">'
           f'<node class="android.widget.LinearLayout" package="{PKG}" bounds="[0,0][100,1000]">'
           f'<node class="android.widget.TextView" text="Shipping address" package="{PKG}" bounds="[0,0][100,50]"/>'
           f'<node class="android.widget.EditText" resource-id="{PKG}:id/city" package="{PKG}" bounds="[0,60][100,110]"/>'
           f'</node></node></hierarchy>')
    fp = ui.parse(xml).fingerprint(PKG)
    assert "text:Shipping address" in fp and f"edit:{PKG}:id/city" in fp


def test_form_labels_are_not_list_rows():
    labels = "".join(f'<node class="android.widget.TextView" text="{t}" package="{PKG}" bounds="[0,{i * 60}][100,{i * 60 + 50}]"/>'
                     f'<node class="android.widget.EditText" package="{PKG}" bounds="[0,{i * 60 + 20}][100,{i * 60 + 55}]"/>'
                     for i, t in enumerate(["Name", "City"]))
    xml = (f'<hierarchy><node class="android.widget.ScrollView" package="{PKG}" bounds="[0,0][100,1000]" scrollable="true">'
           f'<node class="android.widget.LinearLayout" package="{PKG}" bounds="[0,0][100,1000]">{labels}</node></node></hierarchy>')
    fp = ui.parse(xml).fingerprint(PKG)
    assert {"text:Name", "text:City"} <= fp
