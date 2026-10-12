import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from PIL import Image

from droid_monkey import live


def _folder(tmp_path):
    app = tmp_path / "app"
    root = app / ".droid-monkey"
    (root / "screens").mkdir(parents=True)
    (root / "runs" / "attack-20261012-101010" / "rotation").mkdir(parents=True)
    (app / "monkey.yaml").write_text("package: com.ex\nvariant: debug\n")
    Image.new("RGB", (1080, 2400), "white").save(root / "screens" / "home.png")
    (root / "screen-map.json").write_text(json.dumps({"package": "com.ex", "screens": {"home": {
        "id": "home", "path": [], "title": "Home", "fields": [], "fingerprint": [], "actions": []}}}))
    (root / "ANALYSIS.md").write_text("# Analysis\n\n## What the app does\nA **test** app with `code`.\n\n- one\n- two\n")
    (root / "runs" / "attack-20261012-101010" / "rotation" / "verdict.json").write_text(json.dumps(
        {"attack": "rotation", "outcome": "crashed", "reason": "boom", "screen": "home", "seconds": 3}))
    return app, root


def test_version_changes_with_the_folder(tmp_path):
    app, root = _folder(tmp_path)
    v1 = live.version(root)
    time.sleep(0.01)
    (root / "new.txt").write_text("x")
    assert live.version(root) != v1


def test_pages_render_from_the_folder(tmp_path):
    app, root = _folder(tmp_path)
    assert "com.ex" in live.page_overview(root, app) and "1 broke" in live.page_overview(root, app)
    assert "Home" in live.page_screens(root, app) and "/thumb/screens/home.png" in live.page_screens(root, app)
    assert "crashed" in live.page_run(root, app, "attack-20261012-101010")
    assert "<b>test</b>" in live.page_analysis(root, app)


def test_markdown():
    out = live.markdown("## Title\nSome **bold** and `code`.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n1. first\n2. second\n")
    assert "<h3>Title</h3>" in out and "<b>bold</b>" in out and "<code>code</code>" in out
    assert "<th>a</th>" in out and "<td>2</td>" in out and "<ol><li>first</li><li>second</li></ol>" in out


def test_server_serves_inside_the_folder_only(tmp_path):
    app, root = _folder(tmp_path)
    (app / "secret.txt").write_text("no")
    handler = type("H", (live._Handler,), {"root": root, "app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert urllib.request.urlopen(base + "/").status == 200
        assert urllib.request.urlopen(base + "/thumb/screens/home.png").headers["Content-Type"] == "image/jpeg"
        assert json.load(urllib.request.urlopen(base + "/api/version"))["version"]
        for bad in ("/files/../secret.txt", "/files/..%2Fsecret.txt", "/thumb/../secret.txt"):
            try:
                urllib.request.urlopen(base + bad)
                raise AssertionError(bad)
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        httpd.shutdown()


def test_commands_page_guides_a_new_app(tmp_path):
    app = tmp_path / "app"
    (app / ".droid-monkey").mkdir(parents=True)
    (app / "monkey.yaml").write_text("package: com.ex\n")
    page = live.page_commands(app / ".droid-monkey", app, None)   # nothing analysed, mapped or run yet
    assert 'SUGGESTED="analyze"' in page
    assert "Needs a screen map first" in page and "Needs the user flows first" in page
    assert "Map the app&#x27;s screens" in page or "Map the app's screens" in page
    assert page.count('class="run" disabled') >= 5     # every step that needs a screen map waits for it
    assert "Nothing to choose yet." in page            # no runs to report on yet


def test_commands_page_offers_attacks_to_pick(tmp_path):
    app, root = _folder(tmp_path)
    (root / "attacks").mkdir()
    (root / "attacks" / "flow_x.yaml").write_text("name: flow_x\n")
    page = live.page_commands(root, app, None)
    assert "Built-in: rotation" in page and "Saved: flow_x" in page and 'SUGGESTED="analyze"' in page
