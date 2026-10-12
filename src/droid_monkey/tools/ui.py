"""Read the screen through uiautomator: parsed nodes, element lookup, and editable-field values for any app."""

import os
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
    content: bool = False  # inside a list row or a web page: data, which says nothing about which screen this is

    @property
    def editable(self) -> bool:
        """A text field: EditText and its subclasses as they appear in dumps (SearchView's field is an
        AutoCompleteTextView, Material's a TextInputEditText)."""
        return any(k in self.cls for k in ("EditText", "AutoCompleteTextView"))

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
        """Find by resource-id, then text, then content-desc. `text:` / `desc:` force one kind (case-insensitive).
        `a|b|c` tries alternatives in order, so a step survives a UI change (e.g. a View dialog button becoming a
        Compose button with the same label)."""
        if "|" in selector:
            for alt in selector.split("|"):
                node = self.find(alt)
                if node:
                    return node
            return None
        if selector.startswith("field:"):  # the Nth input field on screen, for fields with no resource id
            fields = [n for n in self.nodes if n.editable and n.area > 0]
            i = int(selector[6:])
            return fields[i] if i < len(fields) else None
        if selector.startswith("text:"):
            want = selector[5:].casefold()
            return next((n for n in self.nodes if n.text.casefold() == want and n.area > 0), None)
        if selector.startswith("desc:"):
            want = selector[5:].casefold()
            return next((n for n in self.nodes if n.desc.casefold() == want and n.area > 0), None)
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
        """Which screen this is, from its structure rather than its content: resource ids, content descriptions
        (icon labels), input fields, and the fixed texts outside lists and web pages, such as the top bar title and
        button labels. Text inside list rows and web pages is data (article titles, feed cards, search results), so
        two articles or the feed on two days are the same screen. Typed values are ignored too, so a filled form
        and an empty one are the same screen. Apps without test tags have almost no resource ids, and class names
        alone make every screen look alike, so the fixed labels carry the identity there."""
        tokens = set()
        for n in self.app_nodes(package):
            if n.area == 0:
                continue
            if n.rid:
                tokens.add(f"id:{n.rid}")
            if n.desc and not n.content:
                tokens.add(f"desc:{n.desc[:40]}")
            if n.editable:
                tokens.add(f"edit:{n.rid or n.cls}")
            elif n.text and not n.content and (n.clickable or len(n.text) <= 30):
                tokens.add(f"text:{n.text[:30]}")
        return frozenset(tokens or {n.cls for n in self.app_nodes(package)})

    def content(self) -> tuple:
        """What is on screen, without positions: animations (carousels, shimmer, moving images) change bounds on
        every dump, so comparing whole dumps never settles on a live app."""
        return tuple(sorted((n.package, n.rid, n.cls, n.text[:60], n.desc[:60], n.editable)
                            for n in self.nodes if n.area > 0))

    def scrollable(self, package: str) -> Node | None:
        candidates = [n for n in self.app_nodes(package) if n.scrollable]
        return max(candidates, key=lambda n: n.area, default=None)

    def foreground_package(self) -> str:
        return next((n.package for n in self.nodes if n.package and n.package != "com.android.systemui"), "")


def _rows(el: ET.Element) -> list[ET.Element]:
    """The repeated rows of a scrolling container: children (looking through up to two single-child wrappers)
    of which at least two share a shape (class and resource id) and are items, i.e. tappable or holding other
    views. A RecyclerView's items, a LazyColumn's items; not a form's plain text labels."""
    for _ in range(3):
        all_kids = el.findall("node")
        kids = [k for k in all_kids if k.get("clickable") == "true" or k.find("node") is not None]
        shapes: dict[tuple, int] = {}
        for k in kids:
            shape = (k.get("class", ""), k.get("resource-id", ""))
            shapes[shape] = shapes.get(shape, 0) + 1
        rows = [k for k in kids if shapes[(k.get("class", ""), k.get("resource-id", ""))] >= 2]
        if rows or len(all_kids) != 1:
            return rows
        el = all_kids[0]
    return []


def parse(xml: str) -> Screen:
    root = ET.fromstring(xml)
    content: set[int] = set()  # ids of elements inside list rows or web pages
    for el in root.iter("node"):
        if "WebView" in el.get("class", ""):
            content |= {id(d) for d in el.iter("node") if d is not el}
        elif el.get("scrollable") == "true":
            for row in _rows(el):
                content |= {id(d) for d in row.iter("node")}
    nodes = []
    for el in root.iter("node"):
        m = _BOUNDS.match(el.get("bounds", ""))
        bounds = tuple(int(v) for v in m.groups()) if m else (0, 0, 0, 0)
        nodes.append(Node(
            rid=el.get("resource-id", ""), cls=el.get("class", ""), text=el.get("text", ""),
            desc=el.get("content-desc", ""), package=el.get("package", ""), bounds=bounds,  # type: ignore[arg-type]
            clickable=el.get("clickable") == "true", scrollable=el.get("scrollable") == "true",
            focused=el.get("focused") == "true", content=id(el) in content,
        ))
    return Screen(xml=xml, nodes=nodes)


_RESUMED = re.compile(r"(?:topResumedActivity|mResumedActivity|ResumedActivity)[:=]\s*ActivityRecord\{\S+ \S+ ([\w.]+)/([\w.$]+)")


def top_activity(adb: Adb) -> str:
    """The Activity in the foreground, as its short class name ('' if Android does not say)."""
    out = adb.shell("dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity|ResumedActivity' | head -3",
                    check=False)
    m = _RESUMED.search(out)
    if not m:
        return ""
    pkg, cls = m.groups()
    return (pkg + cls if cls.startswith(".") else cls).rsplit(".", 1)[-1]


# Fast screen reads: uiautomator2's on-device helper answers in ~30 ms, where `uiautomator dump` waits for the app
# to go idle and takes 2-3 s on a live app. Android allows one UI-automation client at a time, so the helper is
# shared per device, a stale holder is cleared once, and `release` frees it (classic dumps and instrumented tests
# need the slot). DROID_MONKEY_FAST_DUMP=0 turns it off.
_FAST: dict[str, object] = {}
_FAST_OFF: set[str] = set()


def _fast(adb: Adb):
    key = adb.serial or "default"
    if os.environ.get("DROID_MONKEY_FAST_DUMP") == "0" or key in _FAST_OFF:
        return None
    if key in _FAST:
        return _FAST[key]
    try:
        import logging
        import uiautomator2 as u2
        logging.getLogger("uiautomator2").setLevel(logging.ERROR)
        for attempt in range(2):
            try:
                _FAST[key] = u2.connect(adb.serial)
                return _FAST[key]
            except Exception as e:
                if attempt == 0 and "already registered" in str(e):  # a stuck `uiautomator dump` holds the slot
                    adb.shell("pkill -f 'uiautomator dump'; pkill -x uiautomator", check=False)
                    time.sleep(1)
                    continue
                raise
    except Exception:
        _FAST_OFF.add(key)  # no helper on this device: classic dumps from now on
    return None


def release(adb: Adb) -> None:
    """Stop the fast helper so the UI-automation slot is free (classic dumps, instrumented tests)."""
    d = _FAST.pop(adb.serial or "default", None)
    if d is not None:
        try:
            d.stop_uiautomator()
        except Exception:
            pass


def dump(adb: Adb, retries: int = 3) -> Screen:
    """The current screen: the fast helper when available, else one `uiautomator dump` (retried when the UI never
    goes idle)."""
    d = _fast(adb)
    if d is not None:
        try:
            return parse(d.dump_hierarchy())
        except Exception:
            release(adb)  # the helper died (device restarted?): the next read reconnects
            _FAST_OFF.discard(adb.serial or "default")
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
    seen_unnamed: list[tuple[int, str]] = []  # (absolute position in the scrolled form, text) of id-less fields

    def record(s: Screen) -> bool:
        added = False
        for node in s.editable_fields(package):
            if node.rid:
                if node.rid not in values:
                    values[node.rid] = node.text
                    added = True
                continue
            # Id-less fields: the same field seen again after a scroll has the same text, so identify by text
            # and order; new ones get the next index (EditText#0, #1, ...).
            sig = (len(seen_unnamed), node.text)
            if any(t == node.text for _, t in seen_unnamed) and node.text:
                continue
            seen_unnamed.append(sig)
            values[_field_key(node, len(seen_unnamed) - 1)] = node.text
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
