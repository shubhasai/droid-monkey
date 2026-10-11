"""The attack designer agent: `attacks suggest` and `attacks new "<prompt>"`.

A tool-using model loop on droid-monkey's own model (Anthropic API by default). It sees the app's analysis, flows
and screen map, can read and search the source, and must validate every spec and try it on the emulator before
saving it. The validator and the runner are code; the model only composes building blocks.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import specs as specs_mod
from . import tracing
from .driver import Context, NavigationError
from .runner import SpecRunner
from .explorer import ScreenMap
from .llm import chat_model
from .profile import AppProfile, source_files
from .tools.adb import AdbError

BUILT_IN = {name: t["description"] for name, t in specs_mod.builtin_templates().items()}

# Haiku 5.5 list prices per million tokens, for the cost line only.
PRICE_IN, PRICE_OUT = 0.10, 0.50


@dataclass
class DesignResult:
    saved: list[Path] = field(default_factory=list)
    tries: list[dict] = field(default_factory=list)
    summary: str = ""
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        return self.input_tokens / 1e6 * PRICE_IN + self.output_tokens / 1e6 * PRICE_OUT


def map_summary(smap: ScreenMap) -> str:
    lines = []
    for s in smap.screens.values():
        lines.append(f"screen {s.id}: '{s.title}' path={s.path}" + (f" form fields={s.fields}" if s.fields else ""))
        for a in s.actions:
            bits = [a.effect]
            if a.to_screen:
                bits.append(f"-> {a.to_screen}")
            if a.showed_progress or a.settle_seconds >= 1.5:
                bits.append(f"starts {a.settle_seconds:.1f}s of work")
            if a.permissions:
                bits.append("asks " + ",".join(a.permissions))
            if a.dialog_dismiss:
                bits.append(f"then a dialog dismissed by '{a.dialog_dismiss}'")
            lines.append(f"  action tap {a.selector!r} ('{a.label[:40]}'): {'; '.join(bits)}")
    return "\n".join(lines)


def flows_summary(flows: list[dict]) -> str:
    out = []
    for f in flows:
        out.append(f"flow {f.get('name')}: {f.get('description', '')}")
        for r in f.get("risks", []):
            out.append(f"  risk {r.get('fault')}: {r.get('why', '')} [trigger: {r.get('trigger', '?')}; code {r.get('code', '?')}]")
    return "\n".join(out) or "(no analysis yet: run `droid-monkey analyze`)"


SYSTEM = """You are the attack designer inside droid-monkey, a tool that attacks Android apps with real-world faults
and then fixes what breaks. You write attack specs: YAML that composes fixed, verified building blocks. You cannot run
shell commands; the only way to act on the device is through a spec.

How to work:
1. Pick the risk to target from the analysis and the screen map.
2. Read code if you need to understand timing or the trigger (read_code, search_code).
3. Write the spec, call validate_spec, fix every error it reports.
4. Call try_spec to run it on the emulator. Read the result:
   - "harness error" or a step that never found its element means your spec is wrong: fix the selector, path or timing.
   - crashed / lost_state / no_feedback means the attack found a problem. That is a good attack; save it.
   - survived: before accepting it, check the fault really overlapped the risky moment. If the work could have
     finished outside the fault window, fix the timing and try again. A survived result from a fault that missed
     its window is a wrong spec, not a passing check.
5. Call save_spec for each finished spec, then reply with a short summary of what you saved and why.

Spec format:
name: snake_case_name
fault_type: one of {fault_types}
description: one sentence a developer understands
screen: <screen id from the screen map; the tool replays its path from a fresh launch>
flow: <optional flow name from the analysis>
clear_data: false        # true only if the attack needs a first-time user (e.g. a permission prompt)
steps:                   # executed in order after reaching the screen
  - fill: true           # fill the form first if the screen has one
  - tap: <selector>
  - wait: 1
  - fault: rotate
  - settle: 8
expect: [no_crash, keeps_fields]

Building blocks:
{catalogue}

Timing: a tap that "starts N s of work" gives you a window of about N seconds. Each tap step also costs ~2 s of
screen reading before it lands. Rules:
- A fault that must hit while the work runs (rotate, a configuration change): wait about N/4 after the tap, then fault.
- A condition that must still hold when the work finishes (background, network off): it must last longer than the
  remaining work. For example, background seconds > N.
- Always end with a settle step long enough for the work to finish.
Selectors must come from the screen map (or text:<visible label>). Permissions must be ones the app declares.

The app
Profile: {profile}

Analysis (flows and risks found by reading the code):
{flows}

Screen map (from exploring the running app):
{screen_map}

Built-in attacks (do not duplicate these): {built_in}
Specs already saved: {existing}"""


def _profile_line(p: AppProfile) -> str:
    perms = ", ".join(p.dangerous_permissions) or "none"
    applies = "; ".join(f"{k}: {v}" for k, v in p.applicable_attacks.items())
    return f"{p.package}, {p.ui_toolkit}, runtime permissions: {perms}. Fault types that apply: {applies}"


class Designer:
    def __init__(self, ctx: Context, smap: ScreenMap, profile: AppProfile, flows: list[dict],
                 existing: dict[str, specs_mod.Spec], allow_try: bool = True, max_tries: int = 6, max_turns: int = 30):
        self.ctx, self.smap, self.profile, self.flows = ctx, smap, profile, flows
        self.existing = existing
        self.allow_try, self.max_tries, self.max_turns = allow_try, max_tries, max_turns
        self.known = specs_mod.Known(
            screens=set(smap.screens),
            selectors={a.selector for s in smap.screens.values() for a in s.actions} | {f for s in smap.screens.values() for f in s.fields},
            permissions=set(profile.dangerous_permissions),
        )
        app_dir = ctx.cfg.app_dir
        self.readable = {str(f.relative_to(app_dir)): f for f in source_files(app_dir, ctx.cfg.module)}
        self.result = DesignResult()

    # ---- tools (plain methods; wrapped as LangChain tools in run()) ------------------------------

    def read_code(self, path: str, start: int = 1, end: int = 200) -> str:
        f = self.readable.get(path)
        if f is None:
            return f"not readable: {path}. Readable source files: {sorted(self.readable)}"
        lines = f.read_text(errors="replace").splitlines()
        start, end = max(1, start), min(len(lines), end, start + 300)
        return "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))

    def search_code(self, pattern: str) -> str:
        hits = []
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        for rel, f in self.readable.items():
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:160]}")
        return "\n".join(hits[:40]) or "no matches"

    def validate_spec(self, spec_yaml: str) -> str:
        try:
            spec = specs_mod.parse(spec_yaml, self.known)
        except specs_mod.SpecError as e:
            return "INVALID:\n- " + "\n- ".join(e.errors)
        return f"valid: {spec.name} ({len(spec.steps)} steps)"

    def try_spec(self, spec_yaml: str) -> str:
        if not self.allow_try:
            return "trying on the device is disabled for this run; validate and save instead"
        if len(self.result.tries) >= self.max_tries:
            return f"try limit reached ({self.max_tries}); save what works now"
        try:
            spec = specs_mod.parse(spec_yaml, self.known)
        except specs_mod.SpecError as e:
            return "INVALID (fix before trying):\n- " + "\n- ".join(e.errors)
        record = {"spec": spec.name}
        try:
            res = SpecRunner(self.ctx, spec).run()
            record.update(outcome=res.outcome, reason=res.reason, exception=res.exception,
                          crash=res.crash_signature, seconds=res.seconds, notes=res.notes[-6:])
            text = (f"outcome: {res.outcome}\nreason: {res.reason}\n"
                    + (f"crash: {res.crash_signature}\n" if res.crash_signature else "")
                    + (f"lost fields: {res.lost_fields}\n" if res.lost_fields else "")
                    + f"steps run: {res.notes[-8:]}\ntook {res.seconds:.0f}s")
        except (NavigationError, AdbError) as e:
            record.update(outcome="harness_error", reason=str(e))
            text = f"harness error: {e}"
        self.result.tries.append(record)
        return text

    def save_spec(self, spec_yaml: str, source: str = "suggested") -> str:
        try:
            spec = specs_mod.parse(spec_yaml, self.known)
        except specs_mod.SpecError as e:
            return "INVALID:\n- " + "\n- ".join(e.errors)
        if spec.name in self.existing or spec.name in BUILT_IN:
            return f"a spec named {spec.name} already exists; choose another name"
        if self.allow_try and not any(t["spec"] == spec.name and t.get("outcome") != "harness_error" for t in self.result.tries):
            return "try_spec this spec successfully (no harness error) before saving it"
        spec.source = source
        path = specs_mod.save(self.ctx.cfg.app_dir, spec)
        self.existing[spec.name] = spec
        self.result.saved.append(path)
        return f"saved {path.name}"

    # ---- the loop --------------------------------------------------------------------------------

    def run(self, goal: str, source: str) -> DesignResult:
        model = chat_model(self.ctx.cfg.llm)  # initialises tracing before LangChain loads
        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
        from langchain_core.tools import StructuredTool

        tools = [
            StructuredTool.from_function(self.read_code, name="read_code",
                                         description="Read lines [start, end] of a source file (path relative to the repo)."),
            StructuredTool.from_function(self.search_code, name="search_code",
                                         description="Regex search over the app's source files; returns file:line matches."),
            StructuredTool.from_function(self.validate_spec, name="validate_spec",
                                         description="Validate a spec (YAML text). Returns 'valid' or every error to fix."),
            StructuredTool.from_function(self.try_spec, name="try_spec",
                                         description="Run a valid spec on the emulator and return the verdict and the steps run."),
            StructuredTool.from_function(lambda spec_yaml: self.save_spec(spec_yaml, source), name="save_spec",
                                         description="Save a valid, tried spec to the app repo."),
        ]
        by_name = {t.name: t for t in tools}
        bound = model.bind_tools(tools)
        system = SYSTEM.format(
            fault_types=specs_mod.FAULT_TYPES, catalogue=specs_mod.catalogue_text(),
            profile=_profile_line(self.profile), flows=flows_summary(self.flows), screen_map=map_summary(self.smap),
            built_in=json.dumps(BUILT_IN), existing=sorted(self.existing) or "none",
        )
        messages = [SystemMessage(system), HumanMessage(goal)]

        @tracing.span(kind="WORKFLOW", name=f"attack_designer:{source}")
        def loop():
            for turn in range(self.max_turns):
                ai = bound.invoke(messages)
                messages.append(ai)
                self.result.turns = turn + 1
                usage = getattr(ai, "usage_metadata", None) or {}
                self.result.input_tokens += usage.get("input_tokens", 0)
                self.result.output_tokens += usage.get("output_tokens", 0)
                if not ai.tool_calls:
                    self.result.summary = ai.text
                    return
                for call in ai.tool_calls:
                    tool = by_name.get(call["name"])
                    out = tool.invoke(call["args"]) if tool else f"unknown tool {call['name']}"
                    messages.append(ToolMessage(content=str(out), tool_call_id=call["id"]))

        loop()
        tracing.flush()
        return self.result


def suggest_goal(count: int) -> str:
    return (f"Design {count} new attack specs for this app. Target the risks in the analysis that the built-in attacks "
            "do not cover, most likely real bug first. Good candidates combine building blocks in ways the built-ins "
            "cannot (e.g. backgrounding during in-flight work, a configuration change (dark mode, font size, language) "
            "while a dialog or request is open, memory pressure on a filled form). Each spec must be validated, tried on the device, and saved.")


def new_goal(prompt: str) -> str:
    return (f"The developer asked for this attack: \"{prompt}\". Design one spec that does exactly that on this app, "
            "choosing the screen, trigger and timing from the screen map and the code. Validate it, try it on the "
            "device, fix it until it runs cleanly, and save it.")


def load_flows(app_dir: Path) -> list[dict]:
    f = app_dir / ".droid-monkey" / "flows.json"
    return json.loads(f.read_text()).get("flows", []) if f.exists() else []


def dump_yaml(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False)
