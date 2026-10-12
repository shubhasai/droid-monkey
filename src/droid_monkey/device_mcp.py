"""MCP server that lets Claude Code drive the emulator and build the screen graph.

Started by `droid-monkey explore` (Claude mode) through `claude -p --mcp-config`. The model decides where to go;
this server executes every action through the AppDriver and records the ground truth itself: the exact replayable
path to each screen, its fingerprint and input fields, what each tap did (timing, progress, dialogs, permission
prompts, crashes), and the edges between screens. Files the model names for a screen are checked to exist.
"""

import argparse
import re
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import config as config_mod
from . import paths
from .driver import AppDriver, Context, NavigationError
from .explorer import ActionInfo, ScreenInfo, ScreenMap, _group, _selector, _similar
from .tools import emulator, logcat, ui
from .tools.adb import Adb, AdbError
from .tools.device import Device

DIALOG_BUTTONS = ("android:id/button1", "android:id/button2", "android:id/button3")


def replay_step(node: ui.Node, selector: str) -> str:
    """The step stored in paths: the selector plus the visible label as a fallback; dialog buttons are optional,
    because a fix may change or remove a dialog."""
    sel = selector
    if node.text and not selector.startswith("text:"):
        sel += f"|text:{node.text.casefold()}"
    verb = "tap?" if selector.startswith("android:id/button") else "tap"
    return f"{verb} {sel}"


class GraphRecorder:
    def __init__(self, app_dir: Path, source_dir: Path, out: Path):
        cfg = config_mod.load(app_dir)
        serial = emulator.running_emulator()
        adb = Adb(serial) if serial else emulator.boot(cfg.avd or emulator.list_avds()[0])
        run_dir = out.parent
        self.ctx = Context(adb=adb, device=Device(adb, cfg.package), cfg=cfg, run_dir=run_dir)
        self.d = AppDriver(self.ctx, run_dir / "explore")
        self.package, self.source_dir, self.out = cfg.package, source_dir, out
        self.map = ScreenMap(package=cfg.package, mode="claude")
        # The previous map, so a re-explored screen keeps its id and saved specs keep working.
        prev_path = paths.map_path(app_dir)
        self.previous = ScreenMap.load(prev_path) if prev_path.exists() else None
        self.path: list[str] = []
        self.elements: list[ui.Node] = []
        self.current: str | None = None
        self.arrival: dict[str, int] = {}   # screen id -> index in self.path where we last arrived there
        self.detours: set[int] = set()      # path indexes of permission prompts and their answers
        self.pending: tuple[str, ActionInfo] | None = None  # (from screen, action) waiting for record_screen
        self.awaiting_permission: tuple[str | None, ActionInfo, dict] | None = None
        self.interactive: dict[str, set[str]] = {}  # screen id -> selectors of its interactive elements
        self.tried: dict[str, set[str]] = {}
        self.baseline = 0.0
        self.started = time.time()

    # ---- helpers ---------------------------------------------------------------------------------

    def save(self) -> None:
        self.map.seconds = round(time.time() - self.started, 1)
        self.map.baseline_seconds = self.baseline
        self.map.save(self.out)

    def match(self, screen: ui.Screen) -> str | None:
        fp = screen.fingerprint(self.package)
        for sid, info in self.map.screens.items():
            if _similar(fp, frozenset(info.fingerprint)):
                return sid
        return None

    def _label(self, screen: ui.Screen, node: ui.Node) -> str:
        if node.text or node.desc:
            return node.text or node.desc
        x1, y1, x2, y2 = node.bounds
        inside = [n.text for n in screen.nodes if n.text and x1 <= n.center[0] <= x2 and y1 <= n.center[1] <= y2]
        return (" ".join(inside)[:60]) or node.rid or node.cls.rsplit(".", 1)[-1]

    def describe(self, screen: ui.Screen, header: str = "") -> str:
        """Compact screen description for the model, with numbered elements it can act on."""
        sid = self.match(screen)
        fg = screen.foreground_package()
        lines = [header] if header else []
        if self.d.is_permission_prompt(screen):
            msg = next((n.text for n in screen.nodes if "permission_message" in n.rid), "")
            lines.append(f"SYSTEM PERMISSION PROMPT: {msg!r}. Call allow_permission or deny_permission.")
        elif fg and fg != self.package:
            lines.append(f"ANOTHER APP IS IN FRONT ({fg}). Use back or launch_fresh.")
        lines.append(f"screen: {'recorded as ' + sid if sid else 'NOT RECORDED YET (call record_screen if it is a new screen)'}")
        lines.append(f"path from launch: {len(self.path)} steps")
        self.elements = []
        tried = self.tried.get(sid or "", set())
        for n in screen.nodes:
            if n.area == 0 or not (n.clickable or n.editable or n.scrollable):
                continue
            if n.package not in (self.package, "android") and not self.d.is_permission_prompt(screen):
                continue
            self.elements.append(n)
            kind = "edit" if n.editable else ("scroll" if n.scrollable and not n.clickable else "tap")
            sel = _selector(n, screen)
            state = f" value={n.text!r}" if n.editable else ""
            mark = " (tried)" if sel in tried else ""
            lines.append(f"  #{len(self.elements)} [{kind}] {sel} {self._label(screen, n)[:50]!r}{state}{mark}")
        texts = [n.text for n in screen.app_nodes(self.package) if n.text and not n.editable][:15]
        if texts:
            lines.append("visible text: " + " | ".join(t[:40] for t in texts))
        return "\n".join(lines)

    def resolve(self, target: str) -> ui.Node:
        target = target.strip()
        if re.fullmatch(r"#?\d+", target):
            i = int(target.lstrip("#"))
            if not 1 <= i <= len(self.elements):
                raise NavigationError(f"no element #{i}; call screen() to see the numbered elements")
            return self.elements[i - 1]
        node = ui.dump(self.ctx.adb).find(target)
        if node is None:
            raise NavigationError(f"'{target}' is not on screen; call screen() to see the numbered elements")
        return node

    def run_step(self, step: str) -> None:
        self.d.step(step)
        self.path.append(step)

    def set_current(self, sid: str | None) -> None:
        if sid and sid != self.current:
            self.arrival[sid] = len(self.path)
        self.current = sid

    def path_to(self, parent: str | None) -> list[str]:
        """Shortest replayable path: the parent's path plus the steps taken since arriving at the parent,
        minus permission-prompt detours (which only replay once, on a first-time user)."""
        if parent is None or parent not in self.map.screens:
            return [p for i, p in enumerate(self.path) if i not in self.detours]
        start = self.arrival.get(parent, 0)
        since = [p for i, p in enumerate(self.path) if i >= start and i not in self.detours]
        return list(self.map.screens[parent].path) + since

    # ---- tools -----------------------------------------------------------------------------------

    def launch_fresh(self, clear_data: bool = True) -> str:
        # Always from cleared app data: a recorded path must not depend on data created earlier in the session.
        self.d.fresh(clear_data=True)
        self.path, self.pending, self.awaiting_permission = [], None, None
        self.detours, self.arrival, self.current = set(), {}, None
        if not self.baseline:
            self.d.settle()
            _, self.baseline, _ = self.d.settle()  # idle screen: measures pure screen-read overhead
        screen, _, _ = self.d.settle()
        self.set_current(self.match(screen))
        return self.describe(screen, "fresh launch (app data cleared)")

    def screen(self) -> str:
        s = ui.dump(self.ctx.adb)
        self.set_current(self.match(s) or self.current)
        return self.describe(s)

    def tap(self, target: str) -> str:
        node = self.resolve(target)
        before = ui.dump(self.ctx.adb)
        from_sid = self.match(before)
        selector = _selector(node, before)
        action = ActionInfo(selector=selector, label=self._label(before, node), group=_group(node))
        granted_before = self.ctx.device.runtime_permissions()
        logcat.clear(self.ctx.adb)
        from .driver import OutwardActionRefused
        try:
            self.run_step(replay_step(node, selector))
        except OutwardActionRefused as e:
            action.effect = "blocked"  # recorded, so no attack ever uses it as a trigger
            if from_sid:
                self.tried.setdefault(from_sid, set()).add(selector)
                info = self.map.screens[from_sid]
                info.actions = [a for a in info.actions if a.selector != selector] + [action]
                self.save()
            return (f"NOT TAPPED: droid-monkey {e} Do not try to reach this action another way. Record this screen "
                    "if it is new, then explore elsewhere.")
        after, seconds, progress = self.d.settle()
        action.settle_seconds = round(max(0.0, seconds - self.baseline), 1)
        action.showed_progress = progress
        if from_sid:
            self.tried.setdefault(from_sid, set()).add(selector)
            info = self.map.screens[from_sid]
            info.actions = [a for a in info.actions if a.selector != selector] + [action]

        header = f"tapped {selector!r}"
        if action.showed_progress or action.settle_seconds >= 1.5:
            header += f" (started {action.settle_seconds:.1f}s of work)"
        crashes = self.d.crashes()
        if crashes:
            action.effect, action.crash = "crash", crashes[0].signature(self.package)
            self.map.crashes.append(f"{from_sid or '?'} -> {action.label}: {action.crash}")
            self.set_current(None)
            self.save()
            return f"{header}\nTHE APP CRASHED: {action.crash}\nIt is recorded. Call launch_fresh to continue exploring."
        if self.d.is_permission_prompt(after):
            action.effect = "permission"
            self.awaiting_permission = (from_sid, action, granted_before)
            self.detours.add(len(self.path) - 1)
        elif any(after.find(b) for b in DIALOG_BUTTONS) and not any(before.find(b) for b in DIALOG_BUTTONS):
            action.effect = "dialog"
        elif after.foreground_package() not in ("", self.package):
            action.effect = "left_app"

        to_sid = self.match(after)
        if action.effect == "unexplored":
            action.effect = "same_screen" if (to_sid and to_sid == from_sid) else "new_screen"
        if to_sid and from_sid and to_sid != from_sid:
            action.to_screen = to_sid
            self._edge(from_sid, to_sid, action.label)
        elif not to_sid and from_sid:
            self.pending = (from_sid, action)
        self.set_current(to_sid)
        self.save()
        return self.describe(after, header)

    def type_text(self, target: str, text: str) -> str:
        node = self.resolve(target)
        sel = _selector(node, ui.dump(self.ctx.adb))
        self.run_step(f"type {sel}={text}")
        return self.describe(ui.dump(self.ctx.adb), f"typed into {sel!r}")

    def scroll(self, direction: str = "down") -> str:
        self.run_step(f"scroll {'up' if direction == 'up' else 'down'}")
        time.sleep(0.5)
        return self.describe(ui.dump(self.ctx.adb), f"scrolled {direction}")

    def back(self) -> str:
        self.run_step("back")
        screen, _, _ = self.d.settle()
        self.set_current(self.match(screen))
        return self.describe(screen, "pressed back")

    def _answer_permission(self, allow: bool) -> str:
        self.run_step("allow_permission" if allow else "deny_permission")
        self.detours.add(len(self.path) - 1)
        screen, _, _ = self.d.settle()
        if self.awaiting_permission:
            from_sid, action, granted_before = self.awaiting_permission
            granted = self.ctx.device.runtime_permissions()
            if allow:
                action.permissions = [p for p, g in granted.items() if g and not granted_before.get(p)]
            self.awaiting_permission = None
            self.save()
        self.set_current(self.match(screen) or self.current)
        return self.describe(screen, "permission " + ("allowed" if allow else "denied"))

    def record_screen(self, name: str, description: str, files: list[str]) -> str:
        screen = ui.dump(self.ctx.adb)
        if self.d.is_permission_prompt(screen):
            return ("not recorded: this is the system permission prompt, not an app screen. Answer it with "
                    "allow_permission or deny_permission; the tap that opened it is recorded on the app screen.")
        fg = screen.foreground_package()
        if fg and fg != self.package:
            return f"not recorded: {fg} is another app, not part of this app. Use back or launch_fresh."
        existing = self.match(screen)
        if existing:
            self.set_current(existing)
            return f"this screen is already recorded as '{existing}'"
        sid = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or f"screen_{len(self.map.screens) + 1}"
        fp = screen.fingerprint(self.package)
        if self.previous:  # stable ids: the same screen as last time keeps last time's id
            for old_id, old in self.previous.screens.items():
                if old_id not in self.map.screens and _similar(fp, frozenset(old.fingerprint)):
                    sid = old_id
                    break
        while sid in self.map.screens:
            sid += "_2"
        found = [f for f in files if (self.source_dir / f).is_file()]
        missing = [f for f in files if f not in found]
        fields = [n.rid or f"EditText#{i}" for i, n in enumerate(screen.editable_fields(self.package))]
        parent = self.pending[0] if self.pending else None
        self.map.screens[sid] = ScreenInfo(
            id=sid, path=self.path_to(parent), title=name, fields=fields,
            fingerprint=sorted(screen.fingerprint(self.package)), description=description, files=found,
        )
        self.interactive[sid] = {_selector(n, screen) for n in screen.app_nodes(self.package)
                                 if n.area and (n.clickable or n.editable)}
        if self.pending:
            from_sid, action = self.pending
            action.to_screen = sid
            self._edge(from_sid, sid, action.label)
            self.pending = None
        self.current = sid
        self.arrival[sid] = len(self.path)
        self.save()
        try:
            from .paths import save_screen
            save_screen(self.ctx.adb, self.ctx.cfg.app_dir, sid, screen.xml)
        except Exception:  # evidence is best effort; the map is what matters here
            pass
        note = f" Files not found in the source: {missing}." if missing else ""
        return f"recorded '{sid}' (path {len(self.path)} steps, {len(fields)} input fields, {len(found)} files).{note}"

    def graph_status(self) -> str:
        lines = [f"{len(self.map.screens)} screens, {len(self.map.edges)} edges, {len(self.map.crashes)} crashes, "
                 f"{time.time() - self.started:.0f}s elapsed"]
        for sid, info in self.map.screens.items():
            untried = sorted(self.interactive.get(sid, set()) - self.tried.get(sid, set()))
            lines.append(f"  {sid}: {len(info.actions)} actions tried; untried: {untried[:12]}")
        for e in self.map.edges:
            lines.append(f"  edge {e['from']} --[{e['action'][:30]}]--> {e['to']}")
        return "\n".join(lines)

    def finish(self, unreached: list[str], notes: str) -> str:
        self.map.unreached = [{"screen": u.split(":", 1)[0].strip(), "reason": u.split(":", 1)[1].strip() if ":" in u else ""}
                              for u in unreached]
        try:
            self.ctx.device.restore()
        except AdbError as e:
            notes += f" (restore: {e})"
        self.save()
        return f"saved {len(self.map.screens)} screens and {len(self.map.edges)} edges. {notes}"

    def _edge(self, a: str, b: str, label: str) -> None:
        edge = {"from": a, "to": b, "action": label}
        if edge not in self.map.edges:
            self.map.edges.append(edge)


def serve(app_dir: Path, source_dir: Path, out: Path) -> None:
    rec = GraphRecorder(app_dir, source_dir, out)
    server = MCPServer("device", instructions="Drive the Android app under test and record its screen graph.")

    def safe(fn):
        def inner(*a, **k):
            try:
                return fn(*a, **k)
            except (NavigationError, AdbError) as e:
                return f"error: {e}"
        inner.__name__, inner.__doc__ = fn.__name__, fn.__doc__
        return inner

    @server.tool(description="Force-stop the app, clear its data and relaunch it like a launcher tap. Resets the "
                             "path; anything a screen needs (e.g. an item to open) must be created again on the way. "
                             "Returns the screen with numbered elements.")
    def launch_fresh() -> str:
        return safe(rec.launch_fresh)()

    @server.tool(description="Describe the current screen: numbered interactive elements, values, visible text, "
                             "and whether it is already recorded.")
    def screen() -> str:
        return safe(rec.screen)()

    @server.tool(description="Tap an element by its number from the last screen description (e.g. '#3') or by "
                             "selector. Reports timing, dialogs, permission prompts, crashes and the new screen.")
    def tap(target: str) -> str:
        return safe(rec.tap)(target)

    @server.tool(description="Type text into an input field (by number or selector). Use values that pass the "
                             "app's validation.")
    def type_text(target: str, text: str) -> str:
        return safe(rec.type_text)(target, text)

    @server.tool(description="Scroll the main list or page: direction 'down' or 'up'.")
    def scroll(direction: str = "down") -> str:
        return safe(rec.scroll)(direction)

    @server.tool(description="Press Android Back.")
    def back() -> str:
        return safe(rec.back)()

    @server.tool(description="Allow the system permission prompt that is showing.")
    def allow_permission() -> str:
        return safe(rec._answer_permission)(True)

    @server.tool(description="Deny the system permission prompt that is showing.")
    def deny_permission() -> str:
        return safe(rec._answer_permission)(False)

    @server.tool(description="Record the current screen as a node: a short name (e.g. 'Checkout form'), one "
                             "sentence on what it is for, and the source files that implement it (paths relative "
                             "to the repo root: the screen, its ViewModel, repositories it calls).")
    def record_screen(name: str, description: str, files: list[str]) -> str:
        return safe(rec.record_screen)(name, description, files)

    @server.tool(description="Screens recorded so far, edges, and untried elements per screen.")
    def graph_status() -> str:
        return safe(rec.graph_status)()

    @server.tool(description="Finish exploring. unreached: screens that exist in the code but were not reached, "
                             "each as 'Name: reason'. notes: anything a developer should know.")
    def finish(unreached: list[str], notes: str) -> str:
        return safe(rec.finish)(unreached, notes)

    server.run("stdio")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--app-dir", required=True, type=Path)
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    a = p.parse_args(argv)
    serve(a.app_dir, a.source, a.out)


if __name__ == "__main__":
    main(sys.argv[1:])
