"""The spec validator is the safety gate: only known building blocks, known screens, selectors and permissions."""

import pytest

from droid_monkey.specs import Known, SpecError, parse

KNOWN = Known(screens={"s1", "s2"}, selectors={"row_#", "submit"}, permissions={"android.permission.CAMERA"})

GOOD = """
name: rotate_during_load
fault_type: rotation
description: Rotate while the row's detail is loading.
screen: s1
steps:
  - tap: row_#
  - wait: 1
  - fault: rotate
  - settle: 8
expect: [no_crash]
"""


def errors(text: str) -> list[str]:
    with pytest.raises(SpecError) as e:
        parse(text, KNOWN)
    return e.value.errors


def test_valid_spec_parses():
    spec = parse(GOOD, KNOWN)
    assert spec.name == "rotate_during_load" and len(spec.steps) == 4


def test_unknown_fault_and_shell_are_rejected():
    errs = errors(GOOD.replace("fault: rotate", "fault: shell").replace("  - settle: 8\n", "  - run: rm -rf /\n  - settle: 8\n"))
    assert any("unknown fault 'shell'" in e for e in errs)
    assert any("unknown step 'run'" in e for e in errs)


def test_unknown_screen_selector_and_permission_are_rejected():
    text = GOOD.replace("screen: s1", "screen: s9").replace("tap: row_#", "tap: nope") \
        .replace("fault: rotate", "fault: revoke_permission\n    permission: android.permission.SEND_SMS")
    errs = errors(text)
    assert any(e.startswith("screen:") for e in errs)
    assert any("selector 'nope'" in e for e in errs)
    assert any("SEND_SMS" in e for e in errs)


def test_argument_ranges_are_checked():
    errs = errors(GOOD.replace("fault: rotate", "fault: font_scale\n    scale: 9"))
    assert any("between 0.85 and 2" in e for e in errs)


def test_spec_without_a_fault_is_not_an_attack():
    errs = errors(GOOD.replace("  - fault: rotate\n", ""))
    assert any("at least one fault" in e for e in errs)


def test_text_and_system_selectors_are_allowed():
    spec = parse(GOOD.replace("tap: row_#", "tap: text:Buy now").replace("  - wait: 1\n", "  - tap: android:id/button1\n"), KNOWN)
    assert spec.steps[0] == {"tap": "text:Buy now"}


def test_whole_number_arguments_are_checked():
    spec = GOOD.replace("  - fault: rotate\n", "  - fault: recreate_activity\n    times: 3\n  - fault: cpu_stress\n    threads: 4\n")
    assert len(parse(spec, KNOWN).steps) == 5
    errs = errors(spec.replace("times: 3", "times: 2.5").replace("threads: 4", "threads: 64"))
    assert any("times: expected a whole number" in e for e in errs)
    assert any("threads: expected a whole number between 1 and 8" in e for e in errs)


def test_scroll_step_and_measured_expectations():
    spec = parse(GOOD.replace("  - wait: 1\n", "  - scroll: down\n").replace("[no_crash]", "[no_crash, smooth, no_leak]"), KNOWN)
    assert {"scroll": "down"} in spec.steps and spec.expect == ["no_crash", "smooth", "no_leak"]
    assert any("scroll" in e for e in errors(GOOD.replace("  - wait: 1\n", "  - scroll: sideways\n")))
