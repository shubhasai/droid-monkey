"""Parsers and the verdict, tested on real output captured from the emulator (no device needed)."""

import re
from pathlib import Path

from droid_monkey.observe import CRASHED, LOST_STATE, SURVIVED, classify
from droid_monkey.tools import logcat, ui

FIX = Path(__file__).parent / "fixtures"
PKG = "com.example.app"  # tests never depend on the demo app's package except via fixtures


def app_pkg(xml: str) -> str:
    return re.search(r'package="([\w.]+)"', xml.split("<node", 2)[2]).group(1)


def test_editable_fields_found_with_test_tags():
    xml = (FIX / "checkout_filled.xml").read_text()
    screen = ui.parse(xml)
    fields = screen.editable_fields(app_pkg(xml))
    assert len(fields) == 6
    assert all(f.rid for f in fields)
    assert sum(1 for f in fields if f.text) == 6


def test_editable_fields_found_without_test_tags():
    xml = re.sub(r'resource-id="(?!android:)[^"]*"', 'resource-id=""', (FIX / "checkout_filled.xml").read_text())
    screen = ui.parse(xml)
    fields = screen.editable_fields(app_pkg(xml))
    assert len(fields) == 6 and not any(f.rid for f in fields)
    assert screen.find("text:Ravi Kumar") is not None


def test_fingerprint_ignores_field_values():
    filled = ui.parse((FIX / "checkout_filled.xml").read_text())
    empty = ui.parse((FIX / "checkout_empty.xml").read_text())
    pkg = app_pkg((FIX / "checkout_filled.xml").read_text())
    assert filled.fingerprint(pkg) == empty.fingerprint(pkg)


def test_parse_real_crash():
    text = (FIX / "crash_security.txt").read_text()
    pkg = re.search(r"Process: ([\w.]+)", text).group(1)
    [crash] = logcat.parse_crashes(text, pkg)
    assert crash.exception == "java.lang.SecurityException"
    assert crash.app_frames(pkg)[0].startswith(pkg)
    assert crash.signature(pkg).startswith("java.lang.SecurityException @ ")


def test_parse_ignores_other_apps_and_logcat_prefixes():
    raw = (
        "10-11 16:33:01.563 14378 14378 E AndroidRuntime: FATAL EXCEPTION: main\n"
        "10-11 16:33:01.563 14378 14378 E AndroidRuntime: Process: com.other.app, PID: 14378\n"
        "10-11 16:33:01.563 14378 14378 E AndroidRuntime: java.lang.NullPointerException: boom\n"
        "10-11 16:34:01.000 15000 15000 E AndroidRuntime: FATAL EXCEPTION: main\n"
        f"10-11 16:34:01.000 15000 15000 E AndroidRuntime: Process: {PKG}, PID: 15000\n"
        "10-11 16:34:01.000 15000 15000 E AndroidRuntime: java.io.IOException: no network\n"
        f"10-11 16:34:01.000 15000 15000 E AndroidRuntime: \tat {PKG}.data.Repo.submit(Repo.kt:27)\n"
    )
    [crash] = logcat.parse_crashes(raw, PKG)
    assert crash.exception == "java.io.IOException" and crash.message == "no network"
    assert crash.frames == [f"{PKG}.data.Repo.submit(Repo.kt:27)"]


def test_verdicts():
    before = {"a": "x", "b": "y", "c": ""}
    assert classify([], before, dict(before)).outcome == SURVIVED
    assert classify([], before, {"a": "x", "b": ""}).outcome == LOST_STATE
    assert classify([], {}, {}, screen_restored=False).outcome == LOST_STATE
    crash = logcat.Crash(kind="crash", exception="java.io.IOException", message="m")
    assert classify([crash], before, before).outcome == CRASHED


def test_selector_alternatives_and_case_insensitive_text():
    xml = (FIX / "checkout_filled.xml").read_text()
    screen = ui.parse(xml)
    assert screen.find("android:id/button1|text:ravi kumar") is not None   # first alternative absent, second matches
    assert screen.find("text:RAVI KUMAR") is not None
    assert screen.find("nope|also_nope") is None
