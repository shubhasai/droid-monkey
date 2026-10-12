"""`explore --mode fast`: a model-free explorer built for real apps.

Compared with the original rules explorer it reads the screen fast (uiautomator2), explores depth first so a tap
that opens a new screen is explored from where the app already is (relaunching only when Back cannot return),
tries the first few elements of each screen (explore_breadth), fills input fields and submits search boxes, records
the links between screens, and treats a scrolled list as the same screen. Every tap goes through the live-backend
guard.
"""

import time

from .driver import NavigationError
from .explorer import ActionInfo, Explorer, ScreenInfo, _group, _selector, _similar
from .tools import ui
from .tools.adb import AdbError

class FastExplorer(Explorer):
    def __init__(self, ctx, top_k: int | None = None, max_screens: int = 40,
                 budget_seconds: float = 1500, max_depth: int = 10, save_path=None):
        super().__init__(ctx, max_screens=max_screens, max_actions=255, budget_seconds=budget_seconds)
        self.top_k = top_k or ctx.cfg.explore_breadth
        self.max_depth, self.save_path = max_depth, save_path
        self.chrome: dict[str, frozenset] = {}  # screen id -> its fixed parts (outside the scrolling list)
        self.replays = 0  # relaunches needed to get back to a screen (what made breadth-first exploring loop)

    # Stay on the screen; only replay when the app is elsewhere (Back first, it is cheaper).
    def go_to(self, info: ScreenInfo) -> ui.Screen:
        screen = ui.dump(self.adb)
        if _similar(screen.fingerprint(self.cfg.package), frozenset(info.fingerprint)):
            return screen
        self.adb.shell("input keyevent 4")
        screen, _, _ = self.d.settle(timeout=4)
        if _similar(screen.fingerprint(self.cfg.package), frozenset(info.fingerprint)):
            return screen
        self.replays += 1
        return super().go_to(info)

    def _chrome(self, screen: ui.Screen) -> frozenset:
        """A screen's fixed parts: what lies outside its main scrolling container (toolbar, tabs, buttons). A
        scrolled list shows other rows but is the same screen, in any app."""
        box = screen.scrollable(self.cfg.package)
        inside = (lambda n: box is not None and box.bounds[0] <= n.center[0] <= box.bounds[2]
                  and box.bounds[1] <= n.center[1] <= box.bounds[3])
        outside = ui.Screen(xml="", nodes=[n for n in screen.nodes if not inside(n)])
        ids = {f"id:{n.rid}" for n in screen.app_nodes(self.cfg.package) if n.rid}
        return outside.fingerprint(self.cfg.package) | frozenset(ids)

    def register(self, screen: ui.Screen, path: list[str]) -> str:
        chrome = self._chrome(screen)
        for sid, known in self.chrome.items():
            if chrome and _similar(chrome, known, threshold=0.8):
                return sid
        sid = super().register(screen, path)
        self.chrome.setdefault(sid, chrome)
        return sid

    def submit_input(self, info: ScreenInfo, screen: ui.Screen) -> str | None:
        """Type sample values into the screen's input fields (the same values attacks use, or monkey.yaml
        fill_values) and, for a search box, press the keyboard's Search key. Login and sign-up forms get filled
        too, but their submit buttons stay behind the live-backend guard; Enter is only pressed for search."""
        fields = screen.editable_fields(self.cfg.package)
        if not fields:
            return None
        steps, search = [], False
        for i, node in enumerate(fields[:4]):
            key = f"{node.rid} {node.text} {node.desc}".lower()
            if "password" in key:
                continue
            sel = _selector(node, screen)
            value = self.d.fill_value(key)
            steps.append(f"type {sel}={value}")
            search |= any(w in key for w in ("search", "query", "find"))
        if search:
            steps.append("enter")
        if not steps:
            return None
        try:
            for step in steps:
                self.d.step(step)
        except NavigationError as e:
            self.notes.append(f"{info.id}: typing failed ({str(e)[:100]})")
            return None
        after, seconds, progress = self.d.settle()
        if _similar(after.fingerprint(self.cfg.package), screen.fingerprint(self.cfg.package)):
            return None
        new_sid = self.register(after, list(info.path) + steps)
        if new_sid != info.id:
            label = "search: " + steps[0].split("=", 1)[1] if search else "fill the form"
            info.actions.append(ActionInfo(selector=steps[-1], label=label, group="input", effect="new_screen",
                                           to_screen=new_sid, settle_seconds=round(seconds, 1), showed_progress=progress))
            self.map.edges.append({"from": info.id, "to": new_sid, "action": label})
            return new_sid
        return None

    def candidates(self, info: ScreenInfo, screen: ui.Screen) -> list[ActionInfo]:
        nodes = self.clickables(screen)
        return [ActionInfo(selector=_selector(n, screen), label=self._label(screen, n), group=_group(n))
                for n in nodes][: self.top_k]

    def explore(self):
        """Depth first: a tap that opens a new screen is explored right away, from where the app already is, so the
        app is only relaunched and the path replayed when Back cannot return to a screen."""
        self.d.fresh(clear_data=self.clear_data)
        self.d.settle()
        root, self.baseline, _ = self.d.settle()
        self.map.baseline_seconds = self.baseline
        self.map.mode = "fast"
        self.explored: set[str] = set()
        self.replays = 0
        self._visit(self.register(root, []), depth=0)
        self.map.seconds = round(time.time() - self.started, 1)
        self.notes.append(f"{len(self.map.screens)} screens, {self.replays} relaunches to replay a path")
        self._save()
        try:
            self.device.restore()
        except AdbError as e:
            self.notes.append(f"restore: {e}")
        return self.map

    def _save(self) -> None:
        if self.save_path:
            self.map.seconds = round(time.time() - self.started, 1)
            self.map.save(self.save_path)

    def _visit(self, sid: str, depth: int) -> None:
        if sid in self.explored or depth > self.max_depth or len(self.explored) >= self.max_screens \
                or time.time() - self.started > self.budget:
            return
        self.explored.add(sid)
        info = self.map.screens[sid]
        try:
            screen = self.go_to(info)
        except (NavigationError, AdbError) as e:
            self.notes.append(f"{sid}: {e}")
            return
        info.actions = self.candidates(info, screen)
        self._save()
        typed = self.submit_input(info, screen)  # search boxes and forms lead somewhere only once filled in
        if typed and typed not in self.explored:
            self._visit(typed, depth + 1)
        for action in info.actions:
            if action.effect == "blocked" or time.time() - self.started > self.budget:
                continue
            try:
                new_sid = self.try_action(info, action)
            except NavigationError as e:  # includes a tap the live-backend guard refused
                action.effect = "blocked" if "live backend" in str(e) else "not_found"
                self.notes.append(f"{info.id} -> {action.label[:40]}: {str(e)[:120]}")
                continue
            except AdbError as e:
                self.notes.append(f"{info.id} -> {action.label[:40]}: {str(e)[:120]}")
                continue
            if new_sid and new_sid not in self.explored:
                self._visit(new_sid, depth + 1)  # the app is on the new screen now: no relaunch needed
