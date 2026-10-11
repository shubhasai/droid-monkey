"""Build and install the app under test with its own Gradle wrapper."""

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .adb import Adb, AdbError


@dataclass
class BuildResult:
    ok: bool
    seconds: float
    apk: Path | None
    log_tail: str


def _variant_task(module: str, variant: str, verb: str) -> str:
    return f":{module}:{verb}{variant[:1].upper()}{variant[1:]}"


def build(app_dir: Path, module: str = "app", variant: str = "debug", timeout: float = 900) -> BuildResult:
    start = time.time()
    proc = subprocess.run(
        ["./gradlew", _variant_task(module, variant, "assemble"), "--console=plain", "-q"],
        cwd=app_dir, capture_output=True, text=True, timeout=timeout,
    )
    apks = sorted((app_dir / module / "build" / "outputs" / "apk" / variant).glob("*.apk"), key=lambda p: p.stat().st_mtime)
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-40:])
    return BuildResult(proc.returncode == 0, time.time() - start, apks[-1] if apks else None, tail)


def install(adb: Adb, apk: Path) -> None:
    out = adb.run("install", "-r", "-t", str(apk), timeout=180)
    if "Success" not in out:
        raise AdbError(f"install failed: {out.strip()[-300:]}")
