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


@dataclass
class AppConfig:
    app_dir: Path
    package: str
    module: str = "app"
    variant: str = "debug"
    avd: str | None = None
    entry_steps: list[str] = field(default_factory=list)   # optional: e.g. log in with a test account
    fill_values: dict[str, str] = field(default_factory=dict)  # optional: sample values by field-name keyword
    java_home: str | None = None  # optional: JDK for this repo's Gradle (older Gradle cannot run on the newest JDK)
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: str = "auto"              # coding agent: auto | claude | codex | api (see agents.py)
    screens_per_attack: int = 2      # each built-in attack runs on this many of its best screens
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
        fill_values=dict(raw.get("fill_values") or {}),
        java_home=raw.get("java_home"),
        llm=LLMConfig.from_dict(raw.get("llm")),
        agent=str(raw.get("agent") or "auto"),
        screens_per_attack=int(raw.get("screens_per_attack") or 2),
        agent_model=raw.get("agent_model"),
    )
