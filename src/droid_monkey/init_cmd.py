"""`droid-monkey init`: write monkey.yaml for an Android repo and check the prerequisites."""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG_NAME
from .tools import emulator


@dataclass
class Detected:
    module: str | None
    package: str | None
    avd: str | None
    java_home: str | None = None


def gradle_major(app_dir: Path) -> int | None:
    props = app_dir / "gradle" / "wrapper" / "gradle-wrapper.properties"
    m = re.search(r"gradle-(\d+)\.", props.read_text()) if props.exists() else None
    return int(m.group(1)) if m else None


def compatible_jdk(app_dir: Path) -> str | None:
    """Gradle before 9 cannot run on the newest JDKs: pin an installed JDK 21 or 17 for those repos."""
    major = gradle_major(app_dir)
    if major is None or major >= 9:
        return None
    for version in ("21", "17"):
        proc = subprocess.run(["/usr/libexec/java_home", "-v", version], capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return None


def find_app_module(app_dir: Path) -> tuple[str | None, str | None]:
    """The module that applies the Android application plugin, and its applicationId."""
    for build in sorted(app_dir.glob("*/build.gradle*")):
        text = build.read_text(errors="replace")
        if "com.android.application" in text or "android.application" in text:
            m = re.search(r'applicationId\s*=?\s*"([\w.]+)"', text)
            return build.parent.name, (m.group(1) if m else None)
    return None, None


def running_avd() -> str | None:
    serial = emulator.running_emulator()
    if not serial:
        return None
    try:
        from .tools.adb import Adb
        out = Adb(serial).run("emu", "avd", "name", timeout=10, check=False)
        name = out.strip().splitlines()[0].strip() if out.strip() else None
        return name if name and name != "OK" else None
    except Exception:
        return None


def detect(app_dir: Path) -> Detected:
    module, package = find_app_module(app_dir)
    avd = running_avd()
    if not avd:
        avds = emulator.list_avds()
        avd = avds[0] if avds else None
    return Detected(module, package, avd, compatible_jdk(app_dir))


def write_config(app_dir: Path, d: Detected) -> Path:
    path = app_dir / CONFIG_NAME
    path.write_text(
        "# droid-monkey config. Only what droid-monkey can't discover by itself.\n"
        f"package: {d.package}\nmodule: {d.module}\nvariant: debug\navd: {d.avd}\n"
        + (f"java_home: {d.java_home}   # this repo's Gradle needs an older JDK\n" if d.java_home else "")
        +
        "# Optional:\n"
        "# agent: auto          # coding agent: auto (Claude Code, else Codex, else Anthropic API) | claude | codex | api\n"
        "# entry_steps: [\"tap text:Log in\", \"fill\", \"tap login_button\"]   # apps behind a login\n"
        "# fill_values: {email: test@example.com}\n"
    )
    return path


def checks(app_dir: Path) -> list[tuple[str, bool, str]]:
    from .tracing import load_keys
    load_keys()
    def which(cmd: str) -> bool:
        return bool(shutil.which(cmd))
    claude_ok = which("claude") or (Path.home() / ".local/bin/claude").exists()
    entire_ok = which("entire") or Path("/opt/homebrew/bin/entire").exists()
    graph_ok = False
    if entire_ok:
        out = subprocess.run([shutil.which("entire") or "/opt/homebrew/bin/entire", "plugin", "list"],
                             capture_output=True, text=True).stdout
        graph_ok = "graph" in out
    entire_enabled = (app_dir / ".entire" / "settings.json").exists()
    return [
        ("adb", which("adb") or (Path.home() / "Library/Android/sdk/platform-tools/adb").exists(), "Android platform-tools"),
        ("emulator running", bool(emulator.running_emulator()), "start your AVD"),
        ("gradlew", (app_dir / "gradlew").exists(), "the app repo needs a Gradle wrapper"),
        ("claude (Claude Code)", claude_ok, "install Claude Code and log in: used by analyze, explore and the fixer"),
        ("entire", entire_ok, "brew install --cask entireio/tap/entire"),
        ("entire graph plugin", graph_ok, "entire plugin install graph"),
        ("Entire enabled in this repo", entire_enabled, "entire enable --agent claude-code"),
        ("DROID_MONKEY_API_KEY", bool(os.environ.get("DROID_MONKEY_API_KEY")), "Anthropic key in ~/.droid-monkey/.env"),
        ("NEATLOGS_API_KEY (optional)", bool(os.environ.get("NEATLOGS_API_KEY")), "neatlogs key in ~/.droid-monkey/.env for tracing"),
    ]
