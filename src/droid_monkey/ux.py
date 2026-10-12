"""`droid-monkey ux`: a UX audit of the mapped app, and the changes you accept, made and checked.

1. Measure (code only): for every mapped screen, how many taps it takes to reach (the shortest route through the
   screen map) against the route recorded, touch targets under Android's 48 dp minimum, icon buttons with no label
   for screen readers, slow actions, long forms, dead ends.
2. Review (Anthropic API, with vision): the model sees every screen's screenshot, the measurements, the user flows
   and the analysis, can read and search the source, and writes findings: the flow, the problem, the evidence
   (screens, numbers), why it matters, a concrete change and the files it touches.
3. Apply, only with your yes: the API coding agent makes the change on a droid-monkey/ux-<run> branch, the app is
   rebuilt and reinstalled, the path to the screen is replayed and an "after" screenshot is taken next to the
   "before" one, then it is committed. Declined findings stay in UX_AUDIT.md as suggestions.
"""

import base64
import io
import json
import re
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

from . import paths, tracing
from .explorer import ScreenMap
from .safety import labels
from .tools import ui

MIN_TARGET_DP = 48
REVIEW_MODEL = "claude-sonnet-5-5"


# ---- 1. measure ------------------------------------------------------------------------------------

def _density(adb) -> int:
    m = re.search(r"(\d+)\s*$", adb.shell("wm density", check=False).strip())
    return int(m.group(1)) if m else 420


def shortest_taps(smap: ScreenMap) -> dict[str, int]:
    """Fewest screen changes from the launch screen to each screen, through the recorded links."""
    roots = [sid for sid, s in smap.screens.items() if not s.path] or list(smap.screens)[:1]
    graph: dict[str, set[str]] = {}
    for e in smap.edges:
        graph.setdefault(e["from"], set()).add(e["to"])
    for s in smap.screens.values():
        for a in s.actions:
            if a.to_screen:
                graph.setdefault(s.id, set()).add(a.to_screen)
    dist = {r: 0 for r in roots}
    queue = deque(roots)
    while queue:
        cur = queue.popleft()
        for nxt in graph.get(cur, ()):
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                queue.append(nxt)
    return dist


def measure(app_dir: Path, smap: ScreenMap, density: int) -> dict:
    """The facts the review starts from, per screen."""
    scale = 160 / max(density, 1)
    dist = shortest_taps(smap)
    out = {}
    for sid, s in smap.screens.items():
        xml = paths.screens_dir(app_dir) / f"{sid}.xml"
        small, unlabeled = [], []
        if xml.exists():
            screen = ui.parse(xml.read_text())
            for n in screen.app_nodes(smap.package):
                if not (n.clickable and n.area):
                    continue
                w, h = (n.bounds[2] - n.bounds[0]) * scale, (n.bounds[3] - n.bounds[1]) * scale
                name = (labels(n, screen) or [n.rid.rsplit("/", 1)[-1] or n.cls.rsplit(".", 1)[-1]])[0][:40]
                if min(w, h) < MIN_TARGET_DP:
                    small.append(f"{name} ({w:.0f}x{h:.0f} dp)")
                spoken = [x for x in labels(n, screen) if x != n.rid.rsplit("/", 1)[-1]]  # an id is not read aloud
                if not spoken and not n.editable:
                    unlabeled.append(n.rid.rsplit("/", 1)[-1] or f"{n.cls.rsplit('.', 1)[-1]} at {n.center}")
        taps_recorded = len([p for p in s.path if p.startswith(("tap", "enter"))])
        out[sid] = {
            "title": s.title, "taps_from_launch_shortest": dist.get(sid), "taps_recorded_path": taps_recorded,
            "input_fields": len(s.fields), "touch_targets_under_48dp": small[:12],
            "unlabeled_buttons": unlabeled[:12],
            "slow_actions": [f"{a.label[:40]} ({a.settle_seconds:.1f}s)" for a in s.actions if a.settle_seconds >= 2],
            "asks_permission": [p.rsplit(".", 1)[-1] for a in s.actions for p in a.permissions],
            "dead_end": not any(a.to_screen for a in s.actions) and not any(e["from"] == sid for e in smap.edges),
            "has_screenshot": (paths.screens_dir(app_dir) / f"{sid}.png").exists(),
        }
    return out


def capture_missing(ctx, smap: ScreenMap, echo=print) -> list[str]:
    """Screens mapped before screenshots were kept: replay each path once and save its screenshot and dump."""
    from .config import Target
    from .driver import AppDriver, NavigationError
    from .tools.adb import AdbError
    d = AppDriver(ctx, ctx.run_dir / "ux-capture")
    failed = []
    for sid, s in smap.screens.items():
        if (paths.screens_dir(ctx.cfg.app_dir) / f"{sid}.png").exists():
            continue
        try:
            d.fresh(clear_data=True)
            d.go_to(Target(name=sid, steps=list(s.path)))
            screen, _, _ = d.settle()
            paths.save_screen(ctx.adb, ctx.cfg.app_dir, sid, screen.xml)
            echo(f"  captured {sid}")
        except (NavigationError, AdbError) as e:
            failed.append(sid)
            echo(f"  could not reach {sid}: {str(e)[:100]}")
    return failed


# ---- 2. review ------------------------------------------------------------------------------------

@dataclass
class Finding:
    id: str
    title: str
    flow: str
    screens: list[str]
    problem: str
    evidence: list[str]
    why: str
    change: str
    files: list[str]
    severity: str = "medium"      # high | medium | low
    kind: str = "navigation"      # navigation | accessibility | feedback | forms | performance | content
    status: str = "proposed"      # proposed | applied | declined | failed
    commit: str | None = None
    before: str | None = None
    after: str | None = None
    note: str = ""


SYSTEM = """You are a senior mobile UX reviewer auditing an Android app. droid-monkey explored the running app: you
have every screen's screenshot (as images), measurements taken from the live UI, the user flows found by reading
the code, and tools to read and search the source.

Find what makes the main user flows slower, harder or less accessible than they need to be, and propose concrete
changes a developer can make in this codebase. Rules:
- Every finding cites evidence: screen ids, a measurement (taps, dp, seconds, field counts) or what a screenshot
  shows. No finding without evidence; no generic advice.
- Prefer high-impact issues on the main flows: extra steps, dead ends, missing feedback, unlabeled controls,
  small touch targets, confusing order, long forms, permission asked too early.
- The change must be specific and implementable in this code (name the files; read them first) and must not
  change the app's backend behaviour.
- At most {max_findings} findings, most important first.

Reply with ONLY this JSON, no prose:
{{"findings": [{{"id": "short_snake_id", "title": "imperative, under 70 chars", "flow": "flow name",
  "screens": ["screen ids"], "problem": "2-3 sentences", "evidence": ["screen_id: what shows it"],
  "why": "the user impact in one sentence", "change": "the concrete change, 2-4 sentences",
  "files": ["repo-relative paths"], "severity": "high|medium|low",
  "kind": "navigation|accessibility|feedback|forms|performance|content"}}]}}"""


def _image_block(png: Path, width: int = 360) -> dict:
    """A downscaled screenshot for the model: enough to judge layout, a fraction of the tokens."""
    from PIL import Image
    img = Image.open(png)
    if img.width > width:
        img = img.resize((width, int(img.height * width / img.width)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=70)
    return {"type": "image", "source_type": "base64", "mime_type": "image/jpeg",
            "data": base64.b64encode(buf.getvalue()).decode()}


def review(app_dir: Path, module: str, llm, smap: ScreenMap, facts: dict, flows: list[dict], analysis: str,
           model: str = REVIEW_MODEL, max_findings: int = 8, max_turns: int = 20) -> tuple[list[Finding], dict]:
    from dataclasses import replace

    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
    from langchain_core.tools import StructuredTool

    from .codetools import CodeTools
    from .llm import chat_model
    from .pricing import cost_usd

    tools_impl = CodeTools(app_dir, module)
    tools = [StructuredTool.from_function(tools_impl.read_code, name="read_code",
                                          description="Read lines [start, end] of a source file (repo-relative)."),
             StructuredTool.from_function(tools_impl.search_code, name="search_code",
                                          description="Regex search over the app's source; returns file:line matches.")]
    by_name = {t.name: t for t in tools}
    chat = chat_model(replace(llm, model=model, max_tokens=16000)).bind_tools(tools)

    content: list = [{"type": "text", "text": (
        "User flows:\n" + json.dumps(flows, indent=1)[:12000] + "\n\nAnalysis:\n" + analysis[:6000]
        + "\n\nMeasurements per screen:\n" + json.dumps(facts, indent=1)[:20000]
        + "\n\nScreens (id, title, recorded path); each screenshot follows its line:")}]
    for sid, s in smap.screens.items():
        content.append({"type": "text", "text": f"{sid}: {s.title} | path: {' > '.join(s.path[-6:]) or '(launch)'}"})
        png = paths.screens_dir(app_dir) / f"{sid}.png"
        if png.exists():
            content.append(_image_block(png))
    messages = [SystemMessage(SYSTEM.format(max_findings=max_findings)), HumanMessage(content=content)]
    tin = tout = 0
    for _ in range(max_turns):
        ai = chat.invoke(messages)
        messages.append(ai)
        u = getattr(ai, "usage_metadata", None) or {}
        tin, tout = tin + int(u.get("input_tokens") or 0), tout + int(u.get("output_tokens") or 0)
        if ai.tool_calls:
            for call in ai.tool_calls:
                tool = by_name.get(call["name"])
                out = tool.invoke(call["args"]) if tool else f"unknown tool {call['name']}"
                messages.append(ToolMessage(content=str(out)[:12000], tool_call_id=call["id"]))
            continue
        m = re.search(r"\{.*\}", ai.text, flags=re.S)
        try:
            raw = json.loads(m.group(0))["findings"] if m else None
        except (ValueError, KeyError):
            raw = None
        if raw is None:
            messages.append(HumanMessage("Reply with ONLY the JSON object described."))
            continue
        known = set(smap.screens)
        findings = []
        for f in raw:
            files = [p.lstrip("./") for p in f.get("files", []) if (app_dir / p.lstrip("./")).is_file()]
            findings.append(Finding(id=re.sub(r"[^a-z0-9_]", "_", str(f.get("id", "finding")).lower())[:40],
                                    title=f.get("title", ""), flow=f.get("flow", ""),
                                    screens=[s for s in f.get("screens", []) if s in known],
                                    problem=f.get("problem", ""), evidence=list(f.get("evidence", [])),
                                    why=f.get("why", ""), change=f.get("change", ""), files=files,
                                    severity=f.get("severity", "medium"), kind=f.get("kind", "navigation")))
        return findings, {"model": model, "input_tokens": tin, "output_tokens": tout,
                          "cost_usd": cost_usd(model, tin, tout)}
    raise RuntimeError("the UX review did not produce findings")


def write_report(app_dir: Path, findings: list[Finding], usage: dict) -> Path:
    out = paths.data_dir(app_dir) / "UX_AUDIT.md"
    lines = ["# UX audit", "", f"{len(findings)} findings · review by {usage.get('model')} · "
             f"{usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out tokens, "
             f"${(usage.get('cost_usd') or 0):.3f}", ""]
    for f in findings:
        lines += [f"## {f.title}", "", f"`{f.id}` · {f.severity} · {f.kind} · flow: {f.flow} · status: **{f.status}**"
                  + (f" · commit `{f.commit[:8]}`" if f.commit else ""), "",
                  f"**Problem.** {f.problem}", "", "**Evidence.**"] + [f"- {e}" for e in f.evidence] + [
                  "", f"**Why it matters.** {f.why}", "", f"**Change.** {f.change}", "",
                  f"**Files.** {', '.join(f'`{p}`' for p in f.files) or '-'}", ""]
        if f.before or f.after:
            lines += [f"Before: `{f.before}` · After: `{f.after}`", ""]
        if f.note:
            lines += [f"_{f.note}_", ""]
    out.write_text("\n".join(lines))
    (paths.data_dir(app_dir) / "ux-audit.json").write_text(json.dumps(
        {"usage": usage, "findings": [asdict(f) for f in findings]}, indent=2))
    return out


# ---- 3. apply (only with consent) -----------------------------------------------------------------------

BRIEF = """Make this UX improvement to the Android app. It was found by a UX audit of the running app.

{title}
Flow: {flow}
Problem: {problem}
Evidence: {evidence}
Change: {change}
Files: {files}

Rules:
- Make exactly this change, small and focused, in the files above (others only if strictly needed).
- Keep behaviour and backend calls the same; this is a UX change. Add string resources for any new text.
- Do not change build files or add dependencies. Do not commit.
- Call the compile tool ('main') before you finish and fix every error.
- Reply with one short paragraph: what you changed."""


def apply_finding(ctx, smap: ScreenMap, f: Finding, out_dir: Path, agent: str, model: str | None, echo=print) -> None:
    from . import agents
    from .coder import ALLOWED, DENIED
    from .config import Target
    from .driver import AppDriver, NavigationError
    from .tools import git, gradle
    from .tools.adb import AdbError

    cfg = ctx.cfg
    before_states = git.file_states(cfg.app_dir)
    build = agents.McpServer("build", "droid_monkey.build_mcp",
                             ["--app-dir", str(cfg.app_dir), "--module", cfg.module, "--variant", cfg.variant]
                             + (["--java-home", cfg.java_home] if cfg.java_home else []))
    res = agents.run(agent, BRIEF.format(title=f.title, flow=f.flow, problem=f.problem, evidence="; ".join(f.evidence),
                                         change=f.change, files=", ".join(f.files) or "find them"),
                     cfg.app_dir, allowed_tools=ALLOWED + ["mcp__build__compile"], disallowed_tools=DENIED,
                     servers=[build], model=model, timeout=1200, name=f"ux.{f.id}", llm=cfg.llm)
    after_states = git.file_states(cfg.app_dir)
    touched = [p for p, h in after_states.items() if before_states.get(p) != h]
    keep = [p for p in touched if p.startswith(f"{cfg.module}/src/")]
    stray = [p for p in touched if p not in keep and not p.startswith(".droid-monkey/")]
    if stray:
        git.discard(cfg.app_dir, stray)
    if not keep:
        f.status, f.note = "failed", f"the coding agent changed nothing: {res.text[:200]}"
        return
    built = gradle.build(cfg.app_dir, cfg.module, cfg.variant, java_home=cfg.java_home)
    if not built.ok or not built.apk:
        git.discard(cfg.app_dir, keep)
        f.status, f.note = "failed", "the change does not build; discarded"
        return
    gradle.install(ctx.adb, built.apk, package=cfg.package, source_dir=cfg.app_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = next((s for s in f.screens if s in smap.screens), None)
    if target:
        before = paths.screens_dir(cfg.app_dir) / f"{target}.png"
        f.before = str(before) if before.exists() else None
        try:
            d = AppDriver(ctx, out_dir)
            d.fresh(clear_data=True)
            d.go_to(Target(name=target, steps=list(smap.screens[target].path)))
            d.settle()
            (out_dir / "after.png").write_bytes(ui.screenshot(ctx.adb))
            f.after = str(out_dir / "after.png")
        except (NavigationError, AdbError) as e:
            f.note = f"after screenshot not taken: the path to {target} changed ({str(e)[:100]})"
    sha, checkpoint = git.commit(cfg.app_dir, keep, f"ux({f.id}): {f.title}\n\n{f.problem}\n\nChange: {f.change}\n\n"
                                                   f"Evidence: {'; '.join(f.evidence)}\ndroid-monkey ux audit")
    f.status, f.commit = "applied", sha
    echo(f"  applied {f.id}: commit {sha[:8]}" + (f", Entire checkpoint {checkpoint}" if checkpoint else ""))


def render(f: Finding) -> str:
    import click
    h = lambda t: click.style(t, bold=True)
    tone = {"high": "red", "medium": "yellow", "low": "white"}.get(f.severity, "white")
    lines = ["", click.style(f"── {f.title} ", fg=tone, bold=True) + f"({f.severity}, {f.kind}, flow: {f.flow})",
             f"{h('Problem:')} {f.problem}", h("Evidence:")] + [f"  {e}" for e in f.evidence]
    if f.screens:
        lines.append("  screenshots: " + ", ".join(f".droid-monkey/screens/{s}.png" for s in f.screens))
    lines += [f"{h('Why it matters:')} {f.why}", f"{h('Suggested change:')} {f.change}",
              f"  files: {', '.join(f.files) or '(the agent will find them)'}"]
    return "\n".join(lines)


def run_audit(ctx, smap: ScreenMap, flows: list[dict], analysis: str,
              review_model: str = REVIEW_MODEL) -> tuple[list[Finding], dict, Path]:
    """Measure and review; writes UX_AUDIT.md and ux-audit.json. Applying is the caller's, finding by finding."""
    tracing.init(tags=[f"ux:{ctx.run_dir.name}", f"app:{ctx.cfg.package}"])
    facts = measure(ctx.cfg.app_dir, smap, _density(ctx.adb))
    findings, usage = review(ctx.cfg.app_dir, ctx.cfg.module, ctx.cfg.llm, smap, facts, flows, analysis,
                             model=review_model)
    return findings, usage, write_report(ctx.cfg.app_dir, findings, usage)
