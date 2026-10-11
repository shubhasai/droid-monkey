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


def run_test(app_dir: Path, module: str, variant: str, test_file: Path, timeout: float = 900) -> TestResult:
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
                          timeout=timeout)
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
