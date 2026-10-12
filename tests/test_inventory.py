from droid_monkey import inventory

MANIFEST = """<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.ex.app">
<application>
 <activity android:name=".MainActivity" android:exported="true">
  <intent-filter><action android:name="android.intent.action.MAIN"/>
   <category android:name="android.intent.category.LAUNCHER"/></intent-filter></activity>
 <activity android:name=".AboutActivity">
  <intent-filter><category android:name="android.intent.category.BROWSABLE"/>
   <data android:scheme="exapp" android:host="about"/></intent-filter></activity>
</application></manifest>"""


def _repo(tmp_path):
    main = tmp_path / "app" / "src" / "main"
    (main / "res" / "values").mkdir(parents=True)
    (main / "res" / "xml").mkdir(parents=True)
    (main / "java").mkdir(parents=True)
    (main / "AndroidManifest.xml").write_text(MANIFEST)
    (main / "res" / "values" / "strings.xml").write_text(
        '<resources><string name="about_title">About this app</string><string name="key_about">about</string>'
        '<string name="checkout">Checkout</string></resources>')
    (main / "res" / "xml" / "prefs.xml").write_text(
        '<PreferenceScreen xmlns:app="http://schemas.android.com/apk/res-auto">'
        '<Preference app:key="@string/key_about" app:title="@string/about_title"/></PreferenceScreen>')
    (main / "java" / "MainActivity.kt").write_text(
        "class MainActivity : AppCompatActivity() {\n"
        "  fun prefs() {\n    findPreference(R.string.key_about).setOnClick {\n"
        "      startActivity(Intent(this, AboutActivity::class.java))\n    }\n  }\n"
        "  @Composable fun Root() {\n    Button(onClick = {}) { Text(stringResource(R.string.checkout)) }\n"
        "    CheckoutScreen()\n  }\n}\n")
    (main / "java" / "AboutActivity.kt").write_text("class AboutActivity : AppCompatActivity()\n")
    (main / "java" / "CheckoutScreen.kt").write_text("@Composable\nfun CheckoutScreen() {}\n")
    return tmp_path


def test_inventory(tmp_path):
    entries = {e.name: e for e in inventory.build(_repo(tmp_path), "com.ex.app")}
    assert entries["MainActivity"].launcher
    about = entries["AboutActivity"]
    assert about.deep_links == ["exapp://about"]
    assert about.opened_from[0]["file"].endswith("MainActivity.kt")
    assert "About this app" in about.opened_from[0]["labels"]  # preference key -> the preference's title
    checkout = entries["CheckoutScreen"]
    assert checkout.kind == "screen" and "Checkout" in checkout.opened_from[0]["labels"]
    brief = inventory.brief(list(entries.values()))
    assert "AboutActivity [activity]" in brief and "near labels ['About this app']" in brief
