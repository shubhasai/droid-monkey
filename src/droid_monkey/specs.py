"""Attack types as specs: YAML built only from fixed, verified building blocks, so every attack replays identically.

A spec lives in the app repo at .droid-monkey/attacks/<name>.yaml. Specs are written by hand, by the attack
designer agent (`attacks suggest`, `attacks new "<prompt>"`), or both. The validator is the gate: anything that
is not a known building block with valid arguments is rejected, so a spec can never run arbitrary commands.

Example:
    name: rotate_during_detail_load
    fault_type: rotation
    description: Rotate while the product detail is still loading.
    screen: s1de026            # start screen id from the screen map; its path is replayed from launch
    steps:
      - tap: product_1
      - wait: 1
      - fault: rotate
      - settle: 8
    expect: [no_crash]
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


FAULT_TYPES = ["process_death", "rotation", "network_loss", "permission_revoke", "configuration_change",
               "low_memory", "background"]

TRIM_LEVELS = ["RUNNING_MODERATE", "RUNNING_LOW", "RUNNING_CRITICAL", "HIDDEN", "BACKGROUND", "MODERATE", "COMPLETE"]

# name -> (argument schema, what it does). Schemas: "bool", "float:lo:hi", "choice:a|b", "permission", "locale".
FAULTS: dict[str, tuple[dict[str, str], str]] = {
    "rotate": ({}, "flip portrait/landscape and confirm the display applied it; recreates the Activity"),
    "network_off": ({}, "wifi and mobile data off, confirmed offline"),
    "network_on": ({}, "wifi and mobile data back on"),
    "airplane_on": ({}, "airplane mode on, confirmed offline"),
    "airplane_off": ({}, "airplane mode off"),
    "kill_in_background": ({}, "Home, then kill the process like the low-memory killer; confirmed dead"),
    "relaunch": ({}, "open the app like a launcher tap, restoring its task and saved state"),
    "background": ({"seconds": "float:0:60"}, "Home, wait, come back, without killing the process"),
    "revoke_permission": ({"permission": "permission"}, "revoke a runtime permission like Settings does; Android kills the app"),
    "dark_mode": ({"enabled": "bool"}, "switch system dark mode; a configuration change that recreates the Activity"),
    "font_scale": ({"scale": "float:0.85:2.0"}, "change the system font size; a configuration change"),
    "locale": ({"locale": "locale"}, "change the app's language; ar/he/fa/ur switch to right-to-left layout"),
    # dont_keep_activities is deliberately absent: `settings put global always_finish_activities 1` does not reach
    # ActivityManager at runtime (verified on API 36: the Activity stays STOPPED), so it could not confirm its effect.
    "trim_memory": ({"level": "choice:" + "|".join(TRIM_LEVELS)}, "send onTrimMemory; background levels send the app Home first"),
}

# Non-fault steps: key -> argument schema.
ACTIONS: dict[str, str] = {
    "tap": "selector",          # resource-id from the screen map, text:<label>, desc:<content-desc>, android:id/...
    "fill": "bool",             # type sample values into every empty editable field on screen
    "wait": "float:0:30",       # plain sleep, for timing a fault inside in-flight work
    "settle": "float:1:30",     # wait until the screen stops changing, at most this long
    "accept_permission": "bool",  # tap Allow if a system permission prompt is showing
    "back": "bool",
    "home": "bool",
    "restart_to_screen": "bool",  # fresh launch, then walk back to the spec's start screen (e.g. after a revoke kills the app)
}

EXPECTATIONS = {
    "no_crash": "no crash or ANR in the app's process",
    "keeps_fields": "every filled field has the same value at the end",
    "returns_to_screen": "the app ends on the screen the attack started from",
    "shows_feedback": "after the trigger the app shows something new (error, retry, message) instead of nothing",
}

LOCALES = {"ar", "he", "fa", "ur", "de", "fr", "es", "hi", "ja", "ru", "pt", "en"}
_NAME = re.compile(r"^[a-z][a-z0-9_]{2,60}$")


@dataclass
class Spec:
    name: str
    fault_type: str
    description: str
    screen: str
    steps: list[dict]
    expect: list[str] = field(default_factory=lambda: ["no_crash"])
    flow: str | None = None
    clear_data: bool = False
    source: str = "manual"  # manual | suggested | prompt

    def to_yaml(self) -> str:
        data = {k: v for k, v in self.__dict__.items() if v not in (None, False) or k in ("steps",)}
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


class SpecError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _check_arg(schema: str, value, where: str, known: "Known") -> str | None:
    kind, *rest = schema.split(":")
    if kind == "bool":
        return None if isinstance(value, bool) else f"{where}: expected true/false, got {value!r}"
    if kind == "float":
        lo, hi = float(rest[0]), float(rest[1])
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not lo <= value <= hi:
            return f"{where}: expected a number between {lo:g} and {hi:g}, got {value!r}"
        return None
    if kind == "choice":
        options = rest[0].split("|")
        return None if value in options else f"{where}: expected one of {options}, got {value!r}"
    if kind == "permission":
        if value not in known.permissions:
            return f"{where}: {value!r} is not a runtime permission this app declares ({sorted(known.permissions) or 'none'})"
        return None
    if kind == "locale":
        return None if value in LOCALES else f"{where}: unsupported locale {value!r}; use one of {sorted(LOCALES)}"
    if kind == "selector":
        if not isinstance(value, str) or not value:
            return f"{where}: expected a selector string"
        if value.startswith(("text:", "desc:", "android:id/")) or value in known.selectors:
            return None
        return f"{where}: selector {value!r} is not in the screen map; use one of {sorted(known.selectors)[:25]} or text:<label>"
    return f"{where}: unknown schema {schema}"


@dataclass
class Known:
    """What the validator checks names against: the app's screen map and profile."""
    screens: set[str]
    selectors: set[str]
    permissions: set[str]


def parse(text: str, known: Known) -> Spec:
    """Parse and validate YAML. Raises SpecError with every problem found, phrased so a model can fix them."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SpecError([f"not valid YAML: {e}"]) from None
    if not isinstance(raw, dict):
        raise SpecError(["the spec must be a YAML mapping"])

    errors: list[str] = []
    name = raw.get("name", "")
    if not isinstance(name, str) or not _NAME.match(name):
        errors.append("name: snake_case, 3-60 characters, starting with a letter")
    if raw.get("fault_type") not in FAULT_TYPES:
        errors.append(f"fault_type: one of {FAULT_TYPES}")
    if not isinstance(raw.get("description"), str) or not raw.get("description"):
        errors.append("description: one sentence is required")
    if raw.get("screen") not in known.screens:
        errors.append(f"screen: must be a screen id from the screen map: {sorted(known.screens)}")
    expect = raw.get("expect") or ["no_crash"]
    bad = [e for e in expect if e not in EXPECTATIONS]
    if bad:
        errors.append(f"expect: unknown {bad}; allowed {sorted(EXPECTATIONS)}")

    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps: a non-empty list is required")
        steps = []
    fault_steps = 0
    for i, step in enumerate(steps, 1):
        where = f"steps[{i}]"
        if not isinstance(step, dict) or not step:
            errors.append(f"{where}: each step is a mapping like {{tap: <selector>}} or {{fault: rotate}}")
            continue
        if "fault" in step:
            fault_steps += 1
            fname = step["fault"]
            if fname not in FAULTS:
                errors.append(f"{where}: unknown fault {fname!r}; allowed {sorted(FAULTS)}")
                continue
            schema = FAULTS[fname][0]
            extra = set(step) - {"fault"} - set(schema)
            if extra:
                errors.append(f"{where}: fault {fname} takes no argument(s) {sorted(extra)}; takes {sorted(schema) or 'none'}")
            for arg, arg_schema in schema.items():
                if arg not in step:
                    errors.append(f"{where}: fault {fname} needs '{arg}'")
                elif (msg := _check_arg(arg_schema, step[arg], f"{where}.{arg}", known)):
                    errors.append(msg)
            continue
        if len(step) != 1:
            errors.append(f"{where}: one action per step, got {sorted(step)}")
            continue
        (key, value), = step.items()
        if key not in ACTIONS:
            errors.append(f"{where}: unknown step {key!r}; allowed {sorted(ACTIONS)} or fault")
        elif (msg := _check_arg(ACTIONS[key], value, f"{where}.{key}", known)):
            errors.append(msg)
    if steps and fault_steps == 0:
        errors.append("steps: at least one fault step is required (otherwise it is not an attack)")

    if errors:
        raise SpecError(errors)
    return Spec(name=name, fault_type=raw["fault_type"], description=raw["description"], screen=raw["screen"],
                steps=steps, expect=list(expect), flow=raw.get("flow"), clear_data=bool(raw.get("clear_data")),
                source=raw.get("source", "manual"))


def spec_dir(app_dir: Path) -> Path:
    from .paths import attacks_dir
    return attacks_dir(app_dir)


def load_all(app_dir: Path, known: Known) -> tuple[dict[str, Spec], dict[str, list[str]]]:
    specs, broken = {}, {}
    for f in sorted(spec_dir(app_dir).glob("*.yaml")):
        try:
            spec = parse(f.read_text(), known)
            specs[spec.name] = spec
        except SpecError as e:
            broken[f.name] = e.errors
    return specs, broken


def save(app_dir: Path, spec: Spec) -> Path:
    path = spec_dir(app_dir) / f"{spec.name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(spec.to_yaml())
    return path


def catalogue_text() -> str:
    """The building blocks, described for humans and models."""
    lines = ["Faults (step form: {fault: <name>, <args>}):"]
    for name, (schema, what) in FAULTS.items():
        args = ", ".join(f"{k}: {v}" for k, v in schema.items())
        lines.append(f"  - {name}" + (f" ({args})" if args else "") + f": {what}")
    lines.append("Actions (step form: {<name>: <value>}):")
    for name, schema in ACTIONS.items():
        lines.append(f"  - {name}: {schema}")
    lines.append("Expectations (expect: [...]):")
    lines += [f"  - {k}: {v}" for k, v in EXPECTATIONS.items()]
    return "\n".join(lines)


# ---- built-in attacks: templates in this package, filled per app ---------------------------------

BUILTIN_DIR = Path(__file__).parent / "builtin_attacks"
_PLACEHOLDER = re.compile(r"^\{(\w+)\}$")


def builtin_templates() -> dict[str, dict]:
    return {f.stem: yaml.safe_load(f.read_text()) for f in sorted(BUILTIN_DIR.glob("*.yaml"))}


def _fill(value, values: dict):
    """Returns (filled value, missing placeholder or None)."""
    if isinstance(value, str) and (m := _PLACEHOLDER.match(value)):
        v = values.get(m.group(1))
        return (v, m.group(1)) if v is None else (v, None)
    return value, None


def render(template: dict, values: dict, known: Known) -> Spec:
    """Fill a built-in template for one app. Steps whose placeholder has no value here are dropped; the result is
    validated like any other spec."""
    data = {}
    for key, value in template.items():
        if key == "steps":
            continue
        filled, missing = _fill(value, values)
        if missing:
            raise SpecError([f"template needs '{missing}'"])
        data[key] = filled
    steps = []
    for step in template.get("steps", []):
        out, drop = {}, False
        for k, v in step.items():
            filled, missing = _fill(v, values)
            if missing:
                drop = True
                break
            out[k] = filled
        if not drop:
            steps.append(out)
    data["steps"] = steps
    spec = parse(yaml.safe_dump(data, sort_keys=False), known)
    spec.source = "built-in"
    return spec

