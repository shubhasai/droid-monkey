"""Crash and ANR detection from logcat, parsed into structured crashes."""

import re
from dataclasses import dataclass, field

from .adb import Adb

_PROCESS = re.compile(r"Process: ([\w.:]+), PID: (\d+)")
_LINE_PREFIX = re.compile(r"^\S+ \S+\s+\d+\s+\d+ [VDIWEF] [^:]+: ?")


@dataclass
class Crash:
    kind: str  # "crash" | "anr"
    exception: str = ""
    message: str = ""
    frames: list[str] = field(default_factory=list)
    caused_by: list[str] = field(default_factory=list)
    raw: str = ""

    def app_frames(self, package: str) -> list[str]:
        return [f for f in self.frames if package in f]

    def signature(self, package: str) -> str:
        top = next(iter(self.app_frames(package)), self.frames[0] if self.frames else "")
        return f"{self.exception} @ {top}"


def clear(adb: Adb) -> None:
    adb.run("logcat", "-b", "all", "-c")


def _strip(line: str) -> str:
    return _LINE_PREFIX.sub("", line)


def parse_crashes(text: str, package: str) -> list[Crash]:
    """Parse `logcat -b crash` output into the crashes that belong to `package`."""
    crashes: list[Crash] = []
    blocks = re.split(r"(?=^.*FATAL EXCEPTION)", text, flags=re.M)
    for block in blocks:
        if "FATAL EXCEPTION" not in block:
            continue
        m = _PROCESS.search(block)
        if not m or m.group(1).split(":")[0] != package:
            continue
        lines = [_strip(line) for line in block.splitlines()]
        body = [line for line in lines if line.strip()]
        crash = Crash(kind="crash", raw="\n".join(body))
        after_process = False
        for line in body:
            s = line.strip()
            if s.startswith("Process:"):
                after_process = True
                continue
            if after_process and not crash.exception:
                head, _, msg = s.partition(": ")
                crash.exception, crash.message = head, msg
            elif s.startswith("at "):
                crash.frames.append(s[3:])
            elif s.startswith("Caused by:"):
                crash.caused_by.append(s[len("Caused by: "):])
        crashes.append(crash)
    return crashes


def read_crashes(adb: Adb, package: str) -> list[Crash]:
    crashes = parse_crashes(adb.run("logcat", "-d", "-b", "crash"), package)
    anr = adb.run("logcat", "-d", "-b", "main,system,events")
    for line in anr.splitlines():
        if (f"ANR in {package}" in line) or ("am_anr" in line and package in line):
            crash = Crash(kind="anr", exception="ANR", message=_strip(line), raw=line)
            _add_anr_trace(adb, package, crash)
            crashes.append(crash)
            break
    return crashes


def _add_anr_trace(adb: Adb, package: str, crash: Crash) -> None:
    """Android saves every ANR, with the stack of each thread, in the dropbox; shell can read it without root.
    The main thread's frames say exactly what was blocking it."""
    out = adb.run("shell", "dumpsys dropbox --print data_app_anr", check=False, timeout=60)
    entries = [e for e in split_dropbox(out) if f"Process: {package}" in e]
    if not entries:
        return
    reason, frames = parse_anr_trace(entries[-1])
    crash.message = reason or crash.message
    crash.frames = frames
    crash.raw = entries[-1][:30000]


def split_dropbox(out: str) -> list[str]:
    """Entries of `dumpsys dropbox --print`, oldest first: each starts after a line of '=' with its date line."""
    return [e for e in re.split(r"^=+\s*$", out, flags=re.M)[1:] if e.strip()]


def parse_anr_trace(entry: str) -> tuple[str, list[str]]:
    """(reason, main-thread frames) from one dropbox data_app_anr entry."""
    reason = re.search(r"^(?:Subject|Reason): (.+)$", entry, flags=re.M)
    main = re.search(r'^"main".*?(?=^\s*$)', entry, flags=re.M | re.S)
    frames = [ln.strip()[3:] for ln in main.group(0).splitlines() if ln.strip().startswith("at ")] if main else []
    return (reason.group(1).strip() if reason else ""), frames
