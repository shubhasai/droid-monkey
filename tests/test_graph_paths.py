"""Paths recorded by the Claude-driven explorer must replay: parent path + steps since, minus permission detours."""

from droid_monkey.device_mcp import GraphRecorder
from droid_monkey.explorer import ScreenInfo, ScreenMap


def recorder() -> GraphRecorder:
    rec = object.__new__(GraphRecorder)  # no device needed for path bookkeeping
    rec.map = ScreenMap("com.example")
    rec.map.screens["form"] = ScreenInfo("form", ["tap open"], "Form", ["name"], ["name"])
    rec.path, rec.detours, rec.current, rec.arrival = [], set(), None, {}
    return rec


def test_path_drops_permission_detours_and_earlier_wandering():
    rec = recorder()
    rec.path = ["tap open", "tap help", "back"]  # wandering before arriving at the form
    rec.set_current("form")
    rec.path += ["tap scan", "deny_permission", "tap scan", "allow_permission", "type name=Test", "tap submit"]
    rec.detours = {3, 4, 5, 6}
    assert rec.path_to("form") == ["tap open", "type name=Test", "tap submit"]


def test_path_without_a_known_parent_keeps_everything_but_detours():
    rec = recorder()
    rec.path, rec.detours = ["tap a", "tap camera", "allow_permission", "tap b"], {1, 2}
    assert rec.path_to(None) == ["tap a", "tap b"]


def test_moving_to_an_unrecorded_screen_keeps_the_steps_from_the_parent():
    # The bug seen on the device: tap -> unrecorded screen -> record_screen produced an empty path.
    rec = recorder()
    rec.path = ["tap open"]
    rec.set_current("form")
    rec.path.append("tap submit")
    rec.set_current(None)  # the new screen is not recorded yet
    assert rec.path_to("form") == ["tap open", "tap submit"]
