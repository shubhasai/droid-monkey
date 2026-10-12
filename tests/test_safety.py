"""droid-monkey must never act on a live backend: no sign-up, login, publish, post, pay. These are the buttons the
Wikipedia explorer actually met, plus the ones it must still be free to tap."""

from droid_monkey.safety import outward_reason
from droid_monkey.tools.ui import Node, Screen


def node(text="", desc="", rid="", cls="android.widget.Button", bounds=(0, 0, 100, 50)):
    return Node(rid=rid, cls=cls, text=text, desc=desc, package="org.wikipedia.dev", bounds=bounds, clickable=True,
                scrollable=False, focused=False)


def test_wikipedia_create_account_next_is_refused_by_its_resource_id():
    assert outward_reason(node("Next", rid="org.wikipedia.dev:id/create_account_submit_button"), None, [])


def test_publish_login_and_join_are_refused():
    for label in ("Publish", "Log in / Join Wikipedia", "Sign up", "Place order", "Send", "Donate"):
        assert outward_reason(node(label), None, []), label


def test_a_label_inside_a_container_counts():
    row = node(bounds=(0, 0, 500, 100))
    inner = node("Create an account", cls="android.widget.TextView", bounds=(10, 10, 300, 60))
    assert outward_reason(row, Screen(xml="", nodes=[row, inner]), [])


def test_ordinary_navigation_and_inputs_are_allowed():
    for label in ("Next", "Save", "Explore", "Add language", "Search Wikipedia", "Continue without email"):
        assert outward_reason(node(label), None, []) is None, label
    assert outward_reason(node("Password", cls="android.widget.EditText", rid="x:id/login_password"), None, []) is None


def test_allow_actions_opt_in_for_a_test_backend():
    assert outward_reason(node("Place order"), None, ["Place order"]) is None
