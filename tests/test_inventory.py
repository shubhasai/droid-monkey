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


def test_multiline_class_headers_skip_parameter_types():
    text = ("class TasksFragment(\n    private val repo: Repo,\n    val id: Int = 0,\n) : Fragment() {}\n"
            "class Holder(val x: Int) : RecyclerView.ViewHolder(x)\n"
            "abstract class BaseThing : Fragment()\n"
            "class Vm @Inject constructor(\n  val s: SavedStateHandle\n) : ViewModel()\n"
            "public class OldFragment extends DialogFragment implements Foo {}\n")
    assert list(inventory.classes(text)) == [("TasksFragment", "Fragment", False), ("Holder", "ViewHolder", False),
                                             ("BaseThing", "Fragment", True), ("Vm", "ViewModel", False),
                                             ("OldFragment", "DialogFragment", False)]


def test_nav_graph_xml_and_safe_args(tmp_path):
    repo = _repo(tmp_path)
    main = repo / "app" / "src" / "main"
    (main / "res" / "navigation").mkdir()
    (main / "res" / "navigation" / "nav.xml").write_text(
        '<navigation xmlns:android="http://schemas.android.com/apk/res/android" '
        'xmlns:app="http://schemas.android.com/apk/res-auto">\n'
        '<fragment android:id="@+id/list" android:name="com.ex.app.ListFragment">\n'
        '  <action android:id="@+id/action_list_to_detail" app:destination="@id/detail"/></fragment>\n'
        '<fragment android:id="@+id/detail" android:name="com.ex.app.DetailFragment" android:label="Details"/>\n'
        '</navigation>')
    (main / "java" / "ListFragment.kt").write_text(
        "class ListFragment : Fragment() {\n  fun open() {\n    binding.button.text = \"Show details\"\n"
        "    findNavController().navigate(ListFragmentDirections.actionListToDetail(3))\n  }\n}\n")
    (main / "java" / "DetailFragment.kt").write_text("class DetailFragment : Fragment()\n")
    detail = {e.name: e for e in inventory.build(repo, "com.ex.app")}["DetailFragment"]
    assert detail.opened_from[0] == {"file": "app/src/main/res/navigation/nav.xml", "line": 4, "labels": ["Details"]}
    code = detail.opened_from[1]
    assert code["file"].endswith("ListFragment.kt") and code["labels"] == ["Show details"]  # text written in code


def test_compose_destination_of_any_name(tmp_path):
    repo = _repo(tmp_path)
    java = repo / "app" / "src" / "main" / "java"
    (java / "Nav.kt").write_text(
        "@Composable fun AppNav() {\n  NavHost(nav, startDestination = HOME) {\n"
        "    composable(HOME) { AppDrawer { Home(onOpen = { nav.navigate(\"profile\") }) } }\n"
        "    composable(\"profile\") { ProfileRoute() }\n  }\n}\n")
    (java / "Home.kt").write_text("@Composable\nfun Home(onOpen: () -> Unit) {\n  Button(onOpen) { Text(\"Open profile\") }\n}\n"
                                  "@Composable fun AppDrawer(content: @Composable () -> Unit) {}\n")
    (java / "Profile.kt").write_text("@Composable\ninternal fun ProfileRoute() {}\n")
    entries = {e.name: e for e in inventory.build(repo, "com.ex.app")}
    assert entries["Home"].kind == "screen" and entries["Home"].note == "route HOME"
    assert entries["ProfileRoute"].kind == "screen"
    assert "AppDrawer" not in entries  # a wrapper, not the destination
    assert entries["profile"].opened_from[0]["file"].endswith("Nav.kt")
