"""Read the screen through uiautomator: parsed nodes, element lookup, and editable-field values for any app."""

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .adb import Adb, AdbError

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass(frozen=True)
class Node:
    rid: str
    cls: str
    text: str
    desc: str
    package: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    scrollable: bool
    focused: bool

    @property
    def editable(self) -> bool:
        return "EditText" in self.cls

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def area(self) -> int:
        x1, y1, x2, y2 = self.bounds
        return max(0, x2 - x1) * max(0, y2 - y1)


@dataclass
class Screen:
    xml: str
    nodes: list[Node]

    def find(self, selector: str) -> Node | None:
        """Find by resource-id, then exact text, then content-desc. `text:` / `desc:` prefixes force one kind."""
        if selector.startswith("text:"):
            return next((n for n in self.nodes if n.text == selector[5:]), None)
        if selector.startswith("desc:"):
            return next((n for n in self.nodes if n.desc == selector[5:]), None)
        for match in (lambda n: n.rid == selector, lambda n: n.text == selector, lambda n: n.desc == selector):
            node = next((n for n in self.nodes if match(n) and n.area > 0), None)
            if node:
                return node
        return None

    def app_nodes(self, package: str) -> list[Node]:
        return [n for n in self.nodes if n.package == package]

    def editable_fields(self, package: str) -> list[Node]:
        return [n for n in self.app_nodes(package) if n.editable and n.area > 0]

    def fingerprint(self, package: str) -> frozenset[str]:
        """Which screen this is: the app's resource ids (or classes), ignoring their text."""
        return frozenset(n.rid or n.cls for n in self.app_nodes(package))

    def scrollable(self, package: str) -> Node | None:
        candidates = [n for n in self.app_nodes(package) if n.scrollable]
        return max(candidates, key=lambda n: n.area, default=None)

    def foreground_package(self) -> str:
        return next((n.package for n in self.nodes if n.package and n.package != "com.android.systemui"), "")


def parse(xml: str) -> Screen:
    root = ET.fromstring(xml)
    nodes = []
    for el in root.iter("node"):
        m = _BOUNDS.match(el.get("bounds", ""))
        bounds = tuple(int(v) for v in m.groups()) if m else (0, 0, 0, 0)
        nodes.append(Node(
            rid=el.get("resource-id", ""), cls=el.get("class", ""), text=el.get("text", ""),
            desc=el.get("content-desc", ""), package=el.get("package", ""), bounds=bounds,  # type: ignore[arg-type]
            clickable=el.get("clickable") == "true", scrollable=el.get("scrollable") == "true",
            focused=el.get("focused") == "true",
        ))
    return Screen(xml=xml, nodes=nodes)


def dump(adb: Adb, retries: int = 3) -> Screen:
    """One uiautomator dump (~1-2 s). Retries when the UI never goes idle (animations)."""
    last = ""
    for _ in range(retries):
        out = adb.run("exec-out", "uiautomator", "dump", "/dev/tty", check=False, timeout=30)
        start, end = out.find("<?xml"), out.rfind("</hierarchy>")
        if start != -1 and end != -1:
            return parse(out[start:end + len("</hierarchy>")])
        last = out.strip()
        time.sleep(0.5)
    raise AdbError(f"uiautomator dump failed: {last[:200]}")


def wait_for(adb: Adb, selector: str, timeout: float = 10.0) -> Node | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        node = dump(adb).find(selector)
        if node:
            return node
        time.sleep(0.3)
    return None


def tap(adb: Adb, node: Node) -> None:
    x, y = node.center
    adb.shell(f"input tap {x} {y}")


def type_text(adb: Adb, text: str) -> None:
    escaped = text.replace(" ", "%s").replace("'", "").replace("&", "").replace(";", "")
    adb.shell(f"input text '{escaped}'")


def hide_keyboard(adb: Adb) -> None:
    adb.shell("input keyevent 111")  # KEYCODE_ESCAPE


def screenshot(adb: Adb) -> bytes:
    return adb.run_bytes("exec-out", "screencap", "-p", timeout=20)


def _field_key(node: Node, index: int) -> str:
    return node.rid or f"{node.cls.rsplit('.', 1)[-1]}#{index}"


def _swipe(adb: Adb, box: tuple[int, int, int, int], up: bool) -> None:
    x1, y1, x2, y2 = box
    x = (x1 + x2) // 2
    top, bottom = y1 + (y2 - y1) // 5, y2 - (y2 - y1) // 5
    a, b = (bottom, top) if up else (top, bottom)
    adb.shell(f"input swipe {x} {a} {x} {b} 300")


def collect_fields(adb: Adb, package: str, max_pages: int = 6) -> dict[str, str]:
    """Every editable field's value on the current screen, scrolling through the screen's main scroller.

    Fields scrolled off-screen are missing from a dump, which would otherwise look like lost state.
    Fields without a resource-id are keyed by class and order of appearance.
    """
    screen = dump(adb)
    scroller = screen.scrollable(package)
    values: dict[str, str] = {}
    unnamed = 0

    def record(s: Screen) -> bool:
        nonlocal unnamed
        added = False
        for node in s.editable_fields(package):
            key = node.rid
            if not key:
                key = _field_key(node, unnamed)
                unnamed += 1
            if key not in values:
                values[key] = node.text
                added = True
        return added

    if scroller is None:
        record(screen)
        return values

    for _ in range(max_pages):  # back to the top first
        _swipe(adb, scroller.bounds, up=False)
        nxt = dump(adb)
        if nxt.xml == screen.xml:
            break
        screen = nxt
    record(screen)
    for _ in range(max_pages):
        _swipe(adb, scroller.bounds, up=True)
        nxt = dump(adb)
        if nxt.xml == screen.xml:
            break
        screen = nxt
        record(screen)
    return values
