"""Build and install the app under test with its own Gradle wrapper."""

import os
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


def android_sdk() -> str | None:
    """The Android SDK: ANDROID_HOME / ANDROID_SDK_ROOT, the standard macOS location, or wherever adb lives."""
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var) and Path(os.environ[var]).is_dir():
            return os.environ[var]
    default = Path.home() / "Library" / "Android" / "sdk"
    if default.is_dir():
        return str(default)
    import shutil
    adb = shutil.which("adb")
    return str(Path(adb).resolve().parent.parent) if adb else None


def gradle_env(java_home: str | None) -> dict:
    """Environment for ./gradlew: the Android SDK always set (a fresh clone has no local.properties), and
    JAVA_HOME pinned when the repo needs a specific JDK."""
    env = dict(os.environ)
    sdk = android_sdk()
    if sdk:
        env.setdefault("ANDROID_HOME", sdk)
        env.setdefault("ANDROID_SDK_ROOT", sdk)
    if java_home:
        env["JAVA_HOME"] = java_home
    return env


def build(app_dir: Path, module: str = "app", variant: str = "debug", timeout: float = 900,
          java_home: str | None = None) -> BuildResult:
    start = time.time()
    proc = subprocess.run(
        ["./gradlew", _variant_task(module, variant, "assemble"), "--console=plain", "-q"],
        cwd=app_dir, capture_output=True, text=True, timeout=timeout, env=gradle_env(java_home),
    )
    apks = sorted((app_dir / module / "build" / "outputs" / "apk" / variant).glob("*.apk"), key=lambda p: p.stat().st_mtime)
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-40:])
    return BuildResult(proc.returncode == 0, time.time() - start, apks[-1] if apks else None, tail)


def install(adb: Adb, apk: Path, package: str | None = None) -> None:
    """Install (replace) the APK. On a full device, `install -r` needs room for both copies, so uninstall the old
    one first and retry: attacks clear app data anyway."""
    try:
        out = adb.run("install", "-r", "-t", str(apk), timeout=180)
    except AdbError as e:
        if "INSUFFICIENT_STORAGE" not in str(e) or not package:
            raise
        adb.run("uninstall", package, check=False, timeout=60)
        out = adb.run("install", "-t", str(apk), timeout=180)
    if "Success" not in out:
        raise AdbError(f"install failed: {out.strip()[-300:]}")


@dataclass
class TestResult:
    ok: bool
    seconds: float
    kind: str  # unit | instrumented
    test_class: str
    log_tail: str


def test_class_of(path: Path) -> tuple[str, str] | None:
    """('unit'|'instrumented', fully qualified class) for a test source file, from its package line and file name."""
    parts = path.parts
    kind = "instrumented" if "androidTest" in parts else ("unit" if "test" in parts else None)
    if kind is None or path.suffix not in (".kt", ".java"):
        return None
    text = path.read_text(errors="replace")
    pkg = next((ln.split()[1].rstrip(";") for ln in text.splitlines() if ln.startswith("package ")), "")
    return kind, (pkg + "." if pkg else "") + path.stem


def run_test(app_dir: Path, module: str, variant: str, test_file: Path, timeout: float = 900,
             java_home: str | None = None) -> TestResult:
    """Run just one test class: a JVM unit test, or an instrumented test on the connected emulator."""
    found = test_class_of(test_file)
    if found is None:
        return TestResult(False, 0.0, "?", str(test_file), "not a test source file under src/test or src/androidTest")
    kind, cls = found
    v = variant[:1].upper() + variant[1:]
    if kind == "unit":
        args = [f":{module}:test{v}UnitTest", "--tests", cls]
    else:
        args = [f":{module}:connected{v}AndroidTest", f"-Pandroid.testInstrumentationRunnerArguments.class={cls}"]
    start = time.time()
    proc = subprocess.run(["./gradlew", *args, "--console=plain"], cwd=app_dir, capture_output=True, text=True,
                          timeout=timeout, env=gradle_env(java_home))
    return TestResult(proc.returncode == 0, round(time.time() - start, 1), kind, cls,
                      failure_excerpt(proc.stdout + proc.stderr, app_dir, module))


_NOISE = ("> Task ", "BUILD ", "Deprecated Gradle", "You can use '--warning-mode", "For more on this",
          "See https://docs.gradle.org", "actionable task")


def failure_excerpt(output: str, app_dir: Path, module: str, limit: int = 60) -> str:
    """The lines that explain a test failure (assertions, exceptions, compile errors), not the Gradle task list.
    Falls back to the JUnit XML report when the console only says that tests failed."""
    lines = [ln for ln in output.splitlines() if ln.strip() and not ln.startswith(_NOISE)]
    keys = ("FAILED", "Error", "error:", "Exception", "expected", "Expected", "but was", "AssertionError", "e: ", "Caused by")
    picked = [ln for ln in lines if any(k in ln for k in keys)]
    for xml in sorted((app_dir / module / "build").glob("**/TEST-*.xml"), key=lambda p: p.stat().st_mtime)[-3:]:
        text = xml.read_text(errors="replace")
        if "<failure" in text:
            start = text.find("<failure")
            picked.append(text[start:start + 1500])
    return "\n".join((picked or lines)[-limit:])
