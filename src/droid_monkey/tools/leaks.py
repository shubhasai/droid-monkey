"""Why a destroyed screen is still in memory: the reference chain from a GC root, found by Shark.

The low_memory attack counts live Activities after a forced GC; that says a screen leaked, not what holds it. For the
"what", droid-monkey dumps the app's heap and runs Shark, LeakCanary's own heap analyser, through its command line
(`shark-cli`: `brew install leakcanary-shark`, or its release zip unpacked in ~/.droid-monkey/tools). It works for any debuggable build, whether or not the app ships
LeakCanary. Its leak trace names every reference from a GC root to the leaked Activity and marks the suspects
(`~~~`), which is what the diagnoser needs to name the cause instead of guessing it.

If the app does ship LeakCanary, its log also lists every Activity, Fragment and View it watched being destroyed
during the attack ("Watching instance of ..."), which tells which screens were recreated.
"""

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .adb import Adb, AdbError

REMOTE = "/data/local/tmp/droid-monkey-leak.hprof"
_WATCHING = re.compile(r"LeakCanary: Watching instance of ([\w.$]+) \(([\w.$]+) received (\w+#\w+)\(\) callback")
TRACE_LIMIT = 8000  # characters of Shark's report kept for the diagnoser


@dataclass
class LeakReport:
    hprof: str = ""               # local heap dump (open it in Android Studio's profiler)
    trace: str = ""               # Shark's leak traces, trimmed
    leaking: list[str] = field(default_factory=list)  # classes Shark reports as leaking
    destroyed: list[str] = field(default_factory=list)  # from LeakCanary's log, when the app ships it
    note: str = ""


TOOLS_DIR = Path.home() / ".droid-monkey" / "tools"
STUDIO_JAVA = Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home")


def shark_cli() -> str | None:
    """`shark-cli` on PATH (Homebrew), else the release unpacked under ~/.droid-monkey/tools."""
    found = shutil.which("shark-cli")
    if found:
        return found
    local = sorted(TOOLS_DIR.glob("shark-cli*/bin/shark-cli"))
    return str(local[-1]) if local else None


def _java_env() -> dict:
    """shark-cli is a Java program: use JAVA_HOME if set, else the JDK that ships with Android Studio."""
    import os
    env = dict(os.environ)
    if not env.get("JAVA_HOME") and not shutil.which("java") and STUDIO_JAVA.exists():
        env["JAVA_HOME"] = str(STUDIO_JAVA)
    return env


def destroyed(adb: Adb) -> list[str]:
    """Activities and Fragments LeakCanary saw being destroyed (from logcat), in order, with counts."""
    out = adb.run("logcat", "-d", "-s", "LeakCanary:D", check=False, timeout=30)
    counts: dict[str, int] = {}
    for m in _WATCHING.finditer(out):
        cls, owner, callback = m.groups()
        if cls.endswith("ReportFragment"):
            continue
        key = f"{owner.rsplit('.', 1)[-1]} ({callback})" if owner != cls else f"{cls.rsplit('.', 1)[-1]} ({callback})"
        counts[key] = counts.get(key, 0) + 1
    return [f"{k} x{n}" if n > 1 else k for k, n in counts.items()]


def dump_heap(adb: Adb, package: str, out: Path, timeout: float = 90) -> Path:
    """Force a GC, dump the heap and pull it. `am dumpheap` returns before the file is complete: wait until its
    size stops changing."""
    adb.shell(f"rm -f {REMOTE}", check=False)
    msg = adb.shell(f"am dumpheap -g {package} {REMOTE}", check=False)
    if "Error" in msg or "not debuggable" in msg:
        raise AdbError(f"heap dump refused: {msg.strip()[:200]}")
    deadline, last = time.time() + timeout, -1
    while time.time() < deadline:
        time.sleep(1.5)
        size = adb.shell(f"stat -c %s {REMOTE} 2>/dev/null", check=False).strip()
        if size.isdigit() and int(size) > 0 and int(size) == last:
            break
        last = int(size) if size.isdigit() else -1
    out.parent.mkdir(parents=True, exist_ok=True)
    adb.run("pull", REMOTE, str(out), check=False, timeout=300)
    adb.shell(f"rm -f {REMOTE}", check=False)
    if not out.exists() or out.stat().st_size == 0:
        raise AdbError("heap dump could not be pulled")
    return out


def _trim(report: str) -> str:
    """Shark's report from the first leak section, without the long metadata block at the end."""
    start = report.find("HEAP ANALYSIS RESULT")
    body = report[start:] if start != -1 else report
    end = body.find("METADATA")
    return (body[:end] if end != -1 else body).strip()[:TRACE_LIMIT]


def leaking_classes(trace: str) -> list[str]:
    """The objects at the end of each leak trace (`╰→ org.example.MainActivity instance`)."""
    return list(dict.fromkeys(m.group(1) for m in re.finditer(r"╰→\s*([\w.$]+)", trace)))


def analyse(adb: Adb, package: str, out_dir: Path, timeout: float = 300) -> LeakReport:
    rep = LeakReport()
    try:
        rep.destroyed = destroyed(adb)
    except AdbError:
        pass
    try:
        hprof = dump_heap(adb, package, out_dir / "heap.hprof")
        rep.hprof = str(hprof)
    except AdbError as e:
        rep.note = str(e)
        return rep
    cli = shark_cli()
    if not cli:
        rep.note = ("heap dump saved; install Shark (`brew install leakcanary-shark`) to get the reference chain "
                    "that holds the leaked screen")
        return rep
    try:
        proc = subprocess.run([cli, "--hprof", str(hprof), "analyze"], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL, env=_java_env())
    except subprocess.TimeoutExpired:
        rep.note = f"Shark did not finish within {timeout:.0f}s; the heap dump is saved"
        return rep
    report = proc.stdout + ("\n" + proc.stderr if proc.returncode else "")
    (out_dir / "shark-report.txt").write_text(report)
    rep.trace = _trim(report)
    rep.leaking = leaking_classes(rep.trace)
    if proc.returncode and not rep.trace:
        rep.note = f"Shark failed ({proc.returncode}): {proc.stderr.strip()[-200:]}"
    return rep
