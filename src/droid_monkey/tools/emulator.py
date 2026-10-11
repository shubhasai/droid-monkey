"""Find, boot, health-check and restart the emulator."""

import os
import shutil
import subprocess
import time
from pathlib import Path

from .adb import Adb, AdbError, AdbHang


def emulator_path() -> str:
    found = shutil.which("emulator")
    if found:
        return found
    sdk = os.environ.get("ANDROID_HOME") or str(Path.home() / "Library/Android/sdk")
    return str(Path(sdk) / "emulator" / "emulator")


def list_avds() -> list[str]:
    out = subprocess.run([emulator_path(), "-list-avds"], capture_output=True, text=True, timeout=30).stdout
    return [line.strip() for line in out.splitlines() if line.strip() and not line.startswith("INFO")]


def running_emulator() -> str | None:
    serials = [s for s in Adb().devices() if s.startswith("emulator-")]
    return serials[0] if serials else None


def healthy(adb: Adb) -> bool:
    try:
        return adb.shell("getprop sys.boot_completed", timeout=8, retries=0).strip() == "1"
    except (AdbError, AdbHang):
        return False


def boot(avd: str, timeout: float = 240.0) -> Adb:
    """Reuse a running emulator, otherwise boot `avd` and wait for boot_completed."""
    serial = running_emulator()
    if serial is None:
        subprocess.Popen(
            [emulator_path(), "-avd", avd, "-no-boot-anim", "-no-snapshot-save"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    deadline = time.time() + timeout
    while time.time() < deadline:
        serial = serial or running_emulator()
        if serial and healthy(Adb(serial)):
            return Adb(serial)
        time.sleep(2)
    raise AdbHang(f"emulator {avd} did not finish booting in {timeout:.0f}s")


def restart(adb: Adb, avd: str) -> Adb:
    """Kill a hung emulator and boot it again."""
    try:
        adb.run("emu", "kill", timeout=10, retries=0, check=False)
    except AdbHang:
        pass
    time.sleep(3)
    return boot(avd)
