"""monkey.yaml: everything app-specific lives here, never in droid-monkey's code."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_NAME = "monkey.yaml"


@dataclass
class Target:
    """A screen state to attack, reached by replaying steps from launch."""
    name: str
    steps: list[str] = field(default_factory=list)
    fill: bool = False  # type sample values into every editable field once there


@dataclass
class Scenario:
    """Default target and trigger action for one attack. The planner chooses these from Block 3 on."""
    target: str
    action: str | None = None
    settle_seconds: float = 4.0


@dataclass
class AppConfig:
    app_dir: Path
    package: str
    module: str = "app"
    variant: str = "debug"
    avd: str | None = None
    targets: dict[str, Target] = field(default_factory=dict)
    scenarios: dict[str, Scenario] = field(default_factory=dict)
    fill_values: dict[str, str] = field(default_factory=dict)


def load(app_dir: Path) -> AppConfig:
    path = app_dir / CONFIG_NAME
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run `droid-monkey init` in the app's repo first.")
    raw = yaml.safe_load(path.read_text()) or {}
    targets = {name: Target(name=name, steps=list(t.get("steps", [])), fill=bool(t.get("fill", False)))
               for name, t in (raw.get("targets") or {}).items()}
    scenarios = {name: Scenario(target=s["target"], action=s.get("action"),
                                settle_seconds=float(s.get("settle_seconds", 4.0)))
                 for name, s in (raw.get("scenarios") or {}).items()}
    return AppConfig(
        app_dir=app_dir.resolve(),
        package=raw["package"],
        module=raw.get("module", "app"),
        variant=raw.get("variant", "debug"),
        avd=raw.get("avd"),
        targets=targets,
        scenarios=scenarios,
        fill_values=dict(raw.get("fill_values") or {}),
    )
