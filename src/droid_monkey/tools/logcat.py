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
            crashes.append(Crash(kind="anr", exception="ANR", message=_strip(line), raw=line))
            break
    return crashes
