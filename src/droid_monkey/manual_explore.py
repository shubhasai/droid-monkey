"""`explore --mode manual`: you drive the app on the emulator, droid-monkey watches and builds the screen map.

droid-monkey relaunches the app from a clean state, then listens to the emulator's raw input events (`getevent`)
while reading the screen continuously (fast reads, ~30 ms). Each tap is matched to the element under the finger
on the screen as it was just before the touch and recorded as a replayable step (`tap <selector>`); Back becomes
`back`, a swipe becomes `scroll down|up`, and text typed into a field becomes `type <field>=<text>` just before the
next action. Every new screen is recorded with its path from launch, so attacks can replay it.

Steps that would act on the live backend (you logged in, you published) are recorded because you chose them, but
attacks will not replay them unless monkey.yaml allows those actions (see safety.py).
"""

import re
import subprocess
import threading
import time

from .driver import AppDriver, Context
from .explorer import ActionInfo, Explorer, ScreenMap, _group, _selector
from .safety import outward_reason
from .tools import ui

_EVENT = re.compile(r"(EV_ABS|EV_KEY|EV_SYN)\s+(\w+)\s+(\w+)")
TAP_SLOP = 40  # px: a touch that moved less than this is a tap


class ManualRecorder:
    def __init__(self, ctx: Context, save_path=None):
        self.ctx, self.cfg, self.adb = ctx, ctx.cfg, ctx.adb
        self.d = AppDriver(ctx, ctx.run_dir / "manual")
        self.helper = Explorer(ctx)  # screen registration and titles, shared with the other explorers
        self.map: ScreenMap = self.helper.map
        self.map.mode = "manual"
        self.save_path = save_path
        self.path: list[str] = []
        self.current: str | None = None
        self.arrived_fields: dict[str, str] = {}
        self.last_screen: ui.Screen | None = None
        self.events: list[tuple] = []          # (kind, data) from the input thread
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.outward_steps: list[str] = []
        self.started = time.time()
        w, h = self._display_size()
        self.scale = (w / 32768, h / 32768)

    def _display_size(self) -> tuple[int, int]:
        m = re.search(r"(\d+)x(\d+)", self.adb.shell("wm size"))
        return (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)

    # ---- input events ----------------------------------------------------------------------------

    def _listen(self) -> None:
        """Touches from the raw multi-touch protocol: a new tracking id starts one, its position is complete at the
        end of that frame (SYN_REPORT), and tracking id ffffffff ends it."""
        cmd = (["adb", "-s", self.adb.serial] if self.adb.serial else ["adb"]) + ["shell", "getevent", "-l"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        try:
            self.consume(proc.stdout)
        finally:
            proc.terminate()

    def consume(self, lines) -> None:
        x = y = None
        starting, down_at, down_screen = False, None, None
        for line in lines:
            if self.stop.is_set():
                break
            m = _EVENT.search(line)
            if not m:
                continue
            kind, code, value = m.groups()
            if code == "ABS_MT_TRACKING_ID":
                if value == "ffffffff":
                    if down_at is not None:
                        with self.lock:
                            self.events.append(("touch", (down_at, (x, y), down_screen)))
                    down_at = None
                else:
                    starting, down_screen = True, self.last_screen  # the screen as it was before the touch
            elif code == "ABS_MT_POSITION_X":
                x = int(value, 16) * self.scale[0]
            elif code == "ABS_MT_POSITION_Y":
                y = int(value, 16) * self.scale[1]
            elif code == "SYN_REPORT" and starting and x is not None and y is not None:
                starting, down_at = False, (x, y)
            elif code == "KEY_BACK" and value == "DOWN":
                with self.lock:
                    self.events.append(("back", None))

    # ---- recording -------------------------------------------------------------------------------

    def _fields(self, screen: ui.Screen) -> dict[str, str]:
        return {_selector(n, screen): n.text for n in screen.editable_fields(self.cfg.package)}

    def _typed_steps(self, screen: ui.Screen) -> list[str]:
        """Text the user typed into fields of the current screen since arriving there."""
        steps = []
        for sel, text in self._fields(screen).items():
            if text and text != self.arrived_fields.get(sel, "") and not sel.startswith("at:"):
                steps.append(f"type {sel}={text}")
                self.arrived_fields[sel] = text
        return steps

    def _record(self, step: str, before: ui.Screen | None, node=None) -> None:
        if before is not None:
            self.path += self._typed_steps(before)
        self.path.append(step)
        if node is not None and outward_reason(node, before, self.cfg.allow_actions):
            self.outward_steps.append(step)
        self._echo(f"  step {len(self.path):>3}: {step}")
        after, seconds, progress = self.d.settle(timeout=6)
        self.last_screen = after
        self._step_shot(step)
        if after.foreground_package() not in ("", self.cfg.package):
            return
        sid = self.helper.register(after, list(self.path))
        if sid != self.current:
            if self.current and node is not None:
                info = self.map.screens[self.current]
                info.actions.append(ActionInfo(selector=step.split(" ", 1)[1], label=self.helper._label(before, node),
                                               group=_group(node), effect="new_screen", to_screen=sid,
                                               settle_seconds=seconds, showed_progress=progress))
                self.map.edges.append({"from": self.current, "to": sid, "action": info.actions[-1].label})
            new = len(self.map.screens[sid].path) == len(self.path)
            self.current = sid
            self.arrived_fields = self._fields(after)
            self._echo(f"  {'new screen' if new else 'back on'} {sid}: {self.map.screens[sid].title[:60]}")
            self._save()

    def _handle(self, kind: str, data) -> None:
        if kind == "back":
            self._record("back", self.last_screen)
            return
        (x0, y0), (x1, y1), before = data
        before = before or self.last_screen
        if abs(x1 - x0) + abs(y1 - y0) > TAP_SLOP:
            if abs(y1 - y0) > abs(x1 - x0):
                self._record(f"scroll {'down' if y1 < y0 else 'up'}", before)
            return
        if before is None:
            return
        hit = [n for n in before.app_nodes(self.cfg.package)
               if n.area and n.bounds[0] <= x0 <= n.bounds[2] and n.bounds[1] <= y0 <= n.bounds[3]]
        if not hit:  # the keyboard, the status bar: typing is captured from the fields instead
            return
        clickable = [n for n in hit if n.clickable] or hit
        node = min(clickable, key=lambda n: n.area)
        if node.editable:  # focusing a field is not a step; what is typed into it is
            return
        self._record(f"tap {_selector(node, before)}", before, node)

    def _step_shot(self, step: str) -> None:
        """What the screen looked like once each step settled: steps/step-007-tap-search.png."""
        name = re.sub(r"[^a-z0-9]+", "-", step.lower())[:40].strip("-")
        try:
            out = self.d.dir / "steps" / f"step-{len(self.path):03d}-{name}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(ui.screenshot(self.adb))
        except Exception as e:
            self._echo(f"  (screenshot failed: {e})")

    def _record_video(self) -> None:
        """The whole session on video. Android's recorder stops after 170 s, so record back-to-back segments
        (session-01.mp4, session-02.mp4, ...) on the device and pull them at the end."""
        self.segments: list[str] = []
        while not self.stop.is_set():
            remote = f"/data/local/tmp/droid-monkey-session-{len(self.segments) + 1:02d}.mp4"
            self.segments.append(remote)
            proc = subprocess.Popen((["adb", "-s", self.adb.serial] if self.adb.serial else ["adb"])
                                    + ["shell", "screenrecord", "--bit-rate", "2000000", "--time-limit", "170", remote],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while proc.poll() is None and not self.stop.is_set():
                time.sleep(0.5)
            if proc.poll() is None:  # stopped by the user: end this segment cleanly so the file is playable
                self.adb.shell("pkill -INT screenrecord", check=False)
                proc.wait(timeout=10)

    def _pull_video(self) -> list:
        out = []
        time.sleep(1)
        for i, remote in enumerate(getattr(self, "segments", []), 1):
            local = self.d.dir / f"session-{i:02d}.mp4"
            self.adb.run("pull", remote, str(local), check=False, timeout=120)
            self.adb.shell(f"rm -f {remote}", check=False)
            if local.exists() and local.stat().st_size:
                out.append(local)
        return out

    def _save(self) -> None:
        if self.save_path:
            self.map.seconds = round(time.time() - self.started, 1)
            self.map.save(self.save_path)

    def _echo(self, line: str) -> None:
        print(line, flush=True)

    def run(self, wait_until_done) -> ScreenMap:
        """`wait_until_done()` blocks until the user says they are finished (e.g. presses Enter)."""
        self.d.fresh(clear_data=True)  # paths are recorded from a clean state, like the other explorers
        root, _, _ = self.d.settle()
        self.last_screen = root
        self.current = self.helper.register(root, [])
        self.arrived_fields = self._fields(root)
        self._save()
        listener = threading.Thread(target=self._listen, daemon=True)
        listener.start()
        video = threading.Thread(target=self._record_video, daemon=True)
        video.start()
        threading.Thread(target=lambda: (wait_until_done(), self.stop.set()), daemon=True).start()
        while not self.stop.is_set():
            with self.lock:
                pending, self.events = self.events, []
            for kind, data in pending:
                self._handle(kind, data)
            if not pending:
                try:
                    self.last_screen = ui.dump(self.adb)  # fast reads keep "the screen before the touch" fresh
                except Exception:
                    pass
                time.sleep(0.15)
        video.join(timeout=15)
        self.videos = self._pull_video()
        self.map.seconds = round(time.time() - self.started, 1)
        self._save()
        return self.map

