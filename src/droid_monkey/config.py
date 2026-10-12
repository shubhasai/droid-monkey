"""monkey.yaml: everything app-specific lives here, never in droid-monkey's code."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .llm import LLMConfig

CONFIG_NAME = "monkey.yaml"


@dataclass
class Target:
    """A screen to reach, by replaying steps from a fresh launch (paths come from the explorer's screen map)."""
    name: str
    steps: list[str] = field(default_factory=list)
    fill: bool = False  # type sample values into every editable field once there
    fingerprint: list[str] = field(default_factory=list)  # what the screen looked like when mapped (to check arrival)


@dataclass
class AppConfig:
    app_dir: Path
    package: str
    module: str = "app"
    variant: str = "debug"
    avd: str | None = None
    entry_steps: list[str] = field(default_factory=list)   # optional: e.g. log in with a test account
    entry_check: str | None = None   # optional: a selector only a signed-in user sees, checked after entry_steps
    fill_values: dict[str, str] = field(default_factory=dict)  # optional: sample values by field-name keyword
    java_home: str | None = None  # optional: JDK for this repo's Gradle (older Gradle cannot run on the newest JDK)
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: str = "auto"              # coding agent: auto | claude | codex | api (see agents.py)
    screens_per_attack: int = 2      # each built-in attack runs on this many of its best screens
    explore_breadth: int = 5         # elements tried per screen by the fast explorers
    explore_minutes: float = 10      # agent explore: time limit; what was mapped by then is kept
    explore_max_screens: int = 60    # agent explore: stop recording new screens after this many
    explore_max_usd: float | None = None  # agent explore: spend limit (Claude Code only)
    verify_minutes: float = 10       # time limit for replaying the recorded paths after exploring
    live_backend: bool = True        # false only for a test or fake backend: then sign-up, publish, pay... may be tapped
    allow_actions: list[str] = field(default_factory=list)  # labels that are safe to tap even on the live backend
    agent_model: str | None = None   # optional model for that agent


def load(app_dir: Path) -> AppConfig:
    path = app_dir / CONFIG_NAME
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run `droid-monkey init` in the app's repo first.")
    raw = yaml.safe_load(path.read_text()) or {}
    return AppConfig(
        app_dir=app_dir.resolve(),
        package=raw["package"],
        module=raw.get("module", "app"),
        variant=raw.get("variant", "debug"),
        avd=raw.get("avd"),
        entry_steps=list(raw.get("entry_steps") or []),
        entry_check=raw.get("entry_check"),
        fill_values=dict(raw.get("fill_values") or {}),
        java_home=raw.get("java_home"),
        llm=LLMConfig.from_dict(raw.get("llm")),
        agent=str(raw.get("agent") or "auto"),
        screens_per_attack=int(raw.get("screens_per_attack") or 2),
        live_backend=raw.get("live_backend", True) is not False,
        explore_breadth=int(raw.get("explore_breadth") or 5),
        explore_minutes=float(raw.get("explore_minutes") or 10),
        explore_max_screens=int(raw.get("explore_max_screens") or 60),
        explore_max_usd=float(raw["explore_max_usd"]) if raw.get("explore_max_usd") else None,
        verify_minutes=float(raw.get("verify_minutes") or 10),
        allow_actions=[str(a) for a in (raw.get("allow_actions") or [])],
        agent_model=raw.get("agent_model"),
    )
