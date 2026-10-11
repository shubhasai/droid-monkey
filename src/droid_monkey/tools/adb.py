"""Thin adb wrapper: every call has a timeout, one retry, and raises AdbHang when the device stops answering."""

import os
import shutil
import subprocess
from pathlib import Path


class AdbError(RuntimeError):
    pass


class AdbHang(AdbError):
    """adb did not answer in time, twice. The Watchdog restarts the emulator on this."""


def adb_path() -> str:
    found = shutil.which("adb")
    if found:
        return found
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or str(Path.home() / "Library/Android/sdk")
    candidate = Path(sdk) / "platform-tools" / "adb"
    if candidate.exists():
        return str(candidate)
    raise AdbError("adb not found: install Android platform-tools or set ANDROID_HOME")


class Adb:
    def __init__(self, serial: str | None = None, timeout: float = 30.0):
        self.bin = adb_path()
        self.serial = serial
        self.timeout = timeout

    def _base(self) -> list[str]:
        return [self.bin, "-s", self.serial] if self.serial else [self.bin]

    def run_bytes(self, *args: str, timeout: float | None = None, check: bool = True, retries: int = 1) -> bytes:
        cmd = self._base() + list(args)
        last: Exception | None = None
        for _ in range(retries + 1):
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=timeout or self.timeout)
            except subprocess.TimeoutExpired as e:
                last = e
                continue
            if check and proc.returncode != 0:
                raise AdbError(f"{' '.join(args)} failed ({proc.returncode}): {proc.stderr.decode(errors='replace').strip()}")
            return proc.stdout
        raise AdbHang(f"adb {' '.join(args)} timed out after {retries + 1} tries") from last

    def run(self, *args: str, **kw) -> str:
        return self.run_bytes(*args, **kw).decode(errors="replace")

    def shell(self, command: str, **kw) -> str:
        return self.run("shell", command, **kw)

    def devices(self) -> list[str]:
        out = Adb(timeout=10).run("devices")
        return [line.split()[0] for line in out.splitlines()[1:] if line.strip().endswith("device")]
