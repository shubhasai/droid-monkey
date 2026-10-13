"""Render the docs' terminal GIFs from droid-monkey's real CLI art (theme.py), with no screen recorder.

    uv run python scripts/render_cli_gifs.py     # writes docs/img/cli-init.gif, cli-run.gif and cli-faces.gif

The art and wording come from theme.py and the CLI itself, so the GIFs stay in step with the terminal. `init` runs
the real command with the machine checks stubbed. The run GIF replays the MonkeyMart recovery run
(run-20261012-180330), with its log lines shortened to fit.
"""

import io
import os
import random
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import click
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from droid_monkey import cli, init_cmd, paths, theme  # noqa: E402

OUT = ROOT / "docs" / "img"
FONT_SIZE, PAD, BAR = 15, 18, 34
BG, FG, CHROME = (13, 17, 23), (201, 209, 217), (22, 27, 34)
PALETTE = {31: (255, 123, 114), 32: (63, 185, 80), 33: (210, 153, 34), 34: (88, 166, 255), 35: (188, 140, 255),
           36: (57, 197, 207), 37: (177, 186, 196), 90: (110, 118, 129)}
BRIGHT = {31: (255, 161, 152), 32: (86, 211, 100), 33: (240, 200, 80), 34: (121, 192, 255), 35: (210, 168, 255),
          36: (86, 212, 221), 37: (240, 246, 252)}
REGULAR, BOLD = (ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", FONT_SIZE, index=i) for i in (0, 1))
CW, CH = round(REGULAR.getlength("M")), round(FONT_SIZE * 1.4)


def spans(line: str):
    """Split one ANSI-styled line into (text, colour, bold, underline) runs."""
    code, bold, under, out = None, False, False, []
    for part in re.split(r"(\x1b\[[0-9;]*m)", line):
        m = re.fullmatch(r"\x1b\[([0-9;]*)m", part)
        if not m:
            if part:
                color = (BRIGHT if bold else PALETTE).get(code, PALETTE.get(code, FG))
                out.append((part, color, bold, under))
            continue
        for c in [int(c) for c in (m.group(1) or "0").split(";") if c]:
            if c == 0:
                code, bold, under = None, False, False
            elif c == 1:
                bold = True
            elif c == 4:
                under = True
            elif c in PALETTE:
                code = c
    return out


@contextmanager
def tty(cols: int):
    """theme.py only draws its art on a terminal: pretend to be one this wide."""
    with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((cols, 30))), \
            mock.patch.object(sys.stdout, "isatty", return_value=True):
        yield


def capture(fn, *args, cols: int = 100, **kw) -> list[str]:
    buf = io.StringIO()
    with tty(cols), mock.patch.object(click, "echo", lambda m="", **_: buf.write(f"{m}\n")):
        fn(*args, **kw)
    return buf.getvalue().rstrip("\n").split("\n")


class Film:
    def __init__(self, title: str, cols: int, rows: int):
        self.title, self.cols, self.rows = title, cols, rows
        self.size = (PAD * 2 + CW * cols, BAR + PAD * 2 + CH * rows)
        self.screen, self.frames, self.durations = [], [], []

    def draw(self, lines: list[str]) -> Image.Image:
        w, h = self.size
        img = Image.new("RGB", self.size, BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, w, BAR], fill=CHROME)
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([14 + i * 20, 11, 26 + i * 20, 23], fill=c)
        d.text((w / 2, BAR / 2), self.title, font=REGULAR, fill=(139, 148, 158), anchor="mm")
        for row, line in enumerate(lines[-self.rows:]):
            x, y = PAD, BAR + PAD + row * CH
            for text, color, bold, under in spans(line):
                d.text((x, y), text, font=BOLD if bold else REGULAR, fill=color)
                if under:
                    d.line([x, y + CH - 3, x + CW * len(text), y + CH - 3], fill=color)
                x += CW * len(text)
        return img

    def shot(self, ms: int, extra: list[str] = ()):
        self.frames.append(self.draw(self.screen + list(extra)))
        self.durations.append(ms)

    def print(self, lines: list[str], ms: int = 450):
        self.screen += lines
        self.shot(ms)

    def type(self, prompt: str, text: str, ms_after: int = 500):
        for i in range(len(text)):
            self.frames.append(self.draw(self.screen + [prompt + text[:i] + theme.style("█", fg="white")]))
            self.durations.append(50)
        self.screen.append(prompt + text)
        self.shot(ms_after)

    def work(self, message: str, seconds: tuple[int, int], frames: int = 12):
        """theme.working()'s monkey: blinking eyes and a bouncing banana, the elapsed time ticking."""
        for n in range(frames):
            elapsed = seconds[0] + (seconds[1] - seconds[0]) * n / max(1, frames - 1)
            self.shot(250, ["  " + line for line in theme._spin_frame(n, message, elapsed)])

    def save(self, name: str):
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / name
        frames = [f.convert("P", palette=Image.ADAPTIVE, colors=64) for f in self.frames]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=self.durations, loop=0, optimize=True)
        print(f"{path.relative_to(ROOT)}  {len(frames)} frames, {path.stat().st_size / 1e6:.2f} MB")


def prompt(repo: str) -> str:
    return theme.style(f"~/{repo}", fg="cyan") + theme.style(" $ ", fg="bright_black")


# ---- init: one thing missing, fixed, then all set ------------------------------------------------

CHECKS = [("adb", True, ""), ("emulator running", True, ""), ("gradlew", True, ""), ("claude (Claude Code)", True, ""),
          ("entire", True, ""), ("entire graph plugin", False, "entire plugin install graph"),
          ("Entire enabled in this repo", True, ""), ("DROID_MONKEY_API_KEY", True, ""),
          ("NEATLOGS_API_KEY (optional)", True, "")]


def init_gif():
    f = Film("droid-monkey init  ·  MonkeyMart", cols=96, rows=31)
    p = prompt("MonkeyMart")
    detected = SimpleNamespace(package="com.shubhasai.monkeymart", module="app", variant="debug", avd="Pixel_10")
    app = Path("/Users/you/MonkeyMart")
    random.seed(4)
    f.shot(500, [p])
    f.type(p, "droid-monkey init")
    with mock.patch.object(init_cmd, "detect", return_value=detected), \
            mock.patch.object(init_cmd, "write_config"), mock.patch.object(paths, "data_dir"), \
            mock.patch.object(init_cmd, "checks", return_value=CHECKS), \
            mock.patch.object(Path, "exists", return_value=False):
        out = capture(cli.init.callback, app_dir=app, force=False, cols=96)
    first = out.index(next(l for l in out if "checking the jungle" in l))
    f.print(out[:first], 900)
    for line in out[first:first + 2 + len(CHECKS)]:
        f.print([line], 160)
    f.print(out[first + 2 + len(CHECKS):], 2600)
    f.screen = []
    f.type(p, "entire plugin install graph", 300)
    f.print([theme.style("  installed graph", fg="bright_black")], 700)
    f.type(p, "droid-monkey init")
    random.seed(4)
    with mock.patch.object(init_cmd, "detect", return_value=detected), \
            mock.patch.object(init_cmd, "write_config"), mock.patch.object(paths, "data_dir"), \
            mock.patch.object(init_cmd, "checks", return_value=[(n, True, h) for n, _, h in CHECKS]), \
            mock.patch.object(Path, "exists", return_value=True):  # monkey.yaml is there now
        out = capture(cli.init.callback, app_dir=app, force=False, cols=96)
    f.print(out, 4000)
    f.save("cli-init.gif")


# ---- run: the recovery run on MonkeyMart ---------------------------------------------------------

ATTACK = "process_death_on_detailscreen"
ASK = "  " + theme.style("Fix it? [yes/no/all] ", fg="magenta", bold=True)


def run_gif():
    f = Film("droid-monkey run  ·  MonkeyMart", cols=100, rows=30)
    p = prompt("MonkeyMart")
    say = lambda line, ms=450: f.print(capture(theme.loop_echo, line), ms)
    f.shot(500, [p])
    f.type(p, f"droid-monkey run --attacks {ATTACK}")
    random.seed(2)
    f.print(capture(theme.banner, "run"), 1400)
    say("setup: branch droid-monkey/run-20261012-180330, built and installed in 2s")
    say(f"\n== attack 1/1: {ATTACK}", 300)
    f.work("Killing the app in the background on DetailScreen...", (0, 20))
    say(f"attack {ATTACK}: lost_state (the app came back on a different screen than the user left)", 1800)
    f.work("Diagnosing: reading the code and the evidence...", (0, 24))
    say("diagnose: navigation state lives in one rememberSaveable String (MainActivity.kt:64)", 600)
    f.type(ASK, "yes", 300)
    f.work("Claude Code is fixing MainActivity.kt...", (0, 35))
    say("fix: claude changed MainActivity.kt, NavigationStateTest.kt in 35s ($0.42)")
    f.work("Rebuilding and replaying the identical attack...", (0, 21), frames=10)
    say("verify: not fixed: replay still fails: lost_state", 1300)
    f.work("Revising the diagnosis...", (0, 42), frames=10)
    say("diagnose: the product lives only in plain remember (DetailScreen.kt:77)", 600)
    f.type(ASK, "yes", 300)
    f.work("Claude Code is fixing DetailScreen.kt...", (0, 68), frames=10)
    say("fix: claude changed MainActivity.kt, DetailScreen.kt, NavigationStateTest.kt in 68s")
    f.work("Rebuilding and replaying the identical attack...", (0, 17), frames=8)
    say("verify: FIXED: replay survived and NavigationStateTest passed", 600)
    say("commit: 92dfd2f2 (Entire checkpoint 01M4R1WTVRBW3HKGJT8MV5SHC3)", 500)
    f.print(capture(theme.monkey_says, "fixed", f"FIXED  {ATTACK}",
                    ["2 attempts, 4 min, proven on the device", "resilience " + theme.banana_meter(100)]), 4500)
    f.save("cli-run.gif")


# ---- the faces: one per verdict ------------------------------------------------------------------

FACES = [
    ("crashed", "CRASHED  rotation", ["IllegalStateException", "at DetailScreen.kt:68"]),
    ("lost_state", "LOST STATE  process_death", ["the half-typed task was gone", "at AddEditTaskViewModel.kt:59"]),
    ("leaked", "LEAKED  low_memory", ["1 -> 7 live Activities after a forced GC"]),
    ("froze", "FROZE  performance", ["input handler asleep for 1500 of 1501 ms"]),
    ("slow_start", "SLOW START  slow_startup", ["cold start median 5946 ms"]),
    ("no_feedback", "NO FEEDBACK  network_loss", ["the submit failed silently"]),
    ("fixed", "FIXED  process_death", ["replay survived, regression test passed"]),
]


def faces_gif():
    f = Film("one face per verdict", cols=72, rows=10)
    for outcome, title, lines in FACES:
        f.screen = capture(theme.monkey_says, outcome, title, lines, cols=72)
        f.shot(1600)
    f.save("cli-faces.gif")


if __name__ == "__main__":
    init_gif()
    run_gif()
    faces_gif()
