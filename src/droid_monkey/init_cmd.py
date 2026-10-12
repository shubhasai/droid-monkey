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
    variant: str = "debug"


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


_ID = r"""\s*=?\s*\(?\s*["']([\w.]+)["']"""  # applicationId "x", = "x" or 'x' (Kotlin DSL and Groovy)


def _block(text: str, name: str) -> str | None:
    """The body of the first `name { ... }` block, braces matched."""
    m = re.search(rf"\b{name}\s*\{{", text)
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end():i - 1]


def _entries(body: str) -> list[tuple[str, str]]:
    """Top-level `name { ... }` or `create("name") { ... }` entries of a block, in order."""
    out, i = [], 0
    pat = re.compile(r"""(?:create|register|maybeCreate)?\(?\s*["']?([A-Za-z]\w*)["']?\s*\)?\s*\{""")
    while (m := pat.search(body, i)):
        depth, j = 1, m.end()
        while j < len(body) and depth:
            depth += {"{": 1, "}": -1}.get(body[j], 0)
            j += 1
        out.append((m.group(1), body[m.end():j - 1]))
        i = j
    return out


def default_variant(text: str) -> tuple[str, str]:
    """(variant, applicationId suffix) for a debug build: the default flavor (isDefault, else the first) plus
    debug, e.g. ('devDebug', '.dev'). Without flavors: ('debug', the debug build type's suffix)."""
    suffix = ""
    flavors = _entries(_block(text, "productFlavors") or "")
    flavor = next((f for f in flavors if re.search(r"isDefault\s*(=|\.set\()\s*true", f[1])), flavors[0] if flavors else None)
    if flavor:
        m = re.search(r"applicationIdSuffix" + _ID, flavor[1])
        suffix += m.group(1) if m else ""
    debug = dict(_entries(_block(text, "buildTypes") or "")).get("debug", "")
    m = re.search(r"applicationIdSuffix" + _ID, debug)
    suffix += m.group(1) if m else ""
    return ((flavor[0] + "Debug") if flavor else "debug"), suffix


def find_app_module(app_dir: Path) -> tuple[str | None, str | None, str]:
    """The module that applies the Android application plugin, the package of its debug build, and the variant."""
    for build in sorted(app_dir.glob("*/build.gradle*")):
        text = build.read_text(errors="replace")
        if "com.android.application" in text or "android.application" in text:
            m = re.search(r"\bapplicationId" + _ID, text)
            variant, suffix = default_variant(text)
            return build.parent.name, (m.group(1) + suffix if m else None), variant
    return None, None, "debug"


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
    module, package, variant = find_app_module(app_dir)
    avd = running_avd()
    if not avd:
        avds = emulator.list_avds()
        avd = avds[0] if avds else None
    return Detected(module, package, avd, compatible_jdk(app_dir), variant)


def write_config(app_dir: Path, d: Detected) -> Path:
    path = app_dir / CONFIG_NAME
    path.write_text(
        "# droid-monkey config. Only what droid-monkey can't discover by itself.\n"
        f"package: {d.package}\nmodule: {d.module}\nvariant: {d.variant}\navd: {d.avd}\n"
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
