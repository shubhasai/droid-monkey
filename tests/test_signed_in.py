from types import SimpleNamespace

import pytest

from droid_monkey import config as config_mod
from droid_monkey import driver as driver_mod
from droid_monkey.driver import NavigationError, OutwardActionRefused, expand_secrets
from droid_monkey.tools import ui

PKG = "com.ex"


def test_secrets_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("DM_TEST_PASSWORD", "s3cret")
    assert expand_secrets("type password=${DM_TEST_PASSWORD}") == "type password=s3cret"
    monkeypatch.delenv("DM_TEST_USER", raising=False)
    monkeypatch.setattr("droid_monkey.tracing.load_keys", lambda: None)
    with pytest.raises(NavigationError, match="DM_TEST_USER"):
        expand_secrets("type username=${DM_TEST_USER}")
    assert expand_secrets("tap text:Log in") == "tap text:Log in"


def test_config_reads_entry_check(tmp_path):
    (tmp_path / "monkey.yaml").write_text('package: com.ex\nentry_steps: ["tap text:Log in"]\nentry_check: "text:Watchlist"\n')
    cfg = config_mod.load(tmp_path)
    assert cfg.entry_steps == ["tap text:Log in"] and cfg.entry_check == "text:Watchlist"


def _driver(entry_steps, live=True):
    d = driver_mod.AppDriver.__new__(driver_mod.AppDriver)
    d.cfg = SimpleNamespace(entry_steps=entry_steps, live_backend=live, allow_actions=[])
    return d


def _button(label):
    xml = (f'<hierarchy><node class="android.widget.Button" text="{label}" clickable="true" package="{PKG}" '
           f'bounds="[0,0][300,100]"/></hierarchy>')
    s = ui.parse(xml)
    return s.nodes[0], s


def test_signed_in_runs_keep_their_session():
    node, screen = _button("Log out")
    with pytest.raises(OutwardActionRefused, match="log out"):
        _driver(["tap text:Log in"], live=False)._guard(node, screen, trusted=False)
    _driver([], live=False)._guard(node, screen, trusted=False)          # not signed in by droid-monkey: allowed
    _driver(["tap text:Log in"])._guard(*_button("Watchlist"), trusted=False)


def test_live_backend_guard_still_applies_when_signed_in():
    node, screen = _button("Publish changes")
    with pytest.raises(OutwardActionRefused):
        _driver(["tap text:Log in"], live=True)._guard(node, screen, trusted=False)
