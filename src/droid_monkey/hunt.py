"""`droid-monkey hunt`: find bugs in normal use, with no fault injected.

Attacks ask "does the app survive what phones do to it?". Hunt asks "does the app work?". It walks every recorded
path in the screen map one step at a time, like a user would, and after every step runs three kinds of detector:

1. The app's own logs (code only): crashes and ANRs on an ordinary path, exceptions the app caught and only logged
   (a swallowed error is the classic silent bug), main-thread jank (Android's "Skipped N frames"), leaked windows.
2. The screen (code only): `null`, `NaN`, `undefined` or an unfilled `%s` / `{0}` shown to the user, negative money,
   a blank screen, the same list row twice.
3. The journey (a model, with the screenshots): what a careful tester would notice across screens: numbers that do
   not add up, a total that disagrees with the price shown a step earlier, a confirmation that says the wrong thing.
   The model must quote the text it saw; code checks that quote is really on that step's screen, so a finding
   nobody can see on the device is dropped.

Every issue keeps the steps that reproduce it from launch, and the screenshot and screen dump of the step where it
showed. Nothing here changes code.
"""

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import tracing
from .driver import AppDriver, Context, NavigationError
from .explorer import ScreenMap
from .tools import logcat, ui
from .tools.adb import AdbError

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Issue:
    kind: str                 # crash | anr | logged_exception | jank | leaked_window | placeholder_text |
                              # negative_amount | blank_screen | duplicate_row | logic
    severity: str             # high | medium | low
    title: str
    detail: str
    detector: str             # logs | screen | journey
    screen: str = ""          # screen id from the map, when the step lands on a mapped screen
    step: int = 0             # how many steps from launch
    steps: list[str] = field(default_factory=list)  # the steps that reproduce it, from launch
    evidence: list[str] = field(default_factory=list)  # files in the hunt folder, and quoted lines
    key: str = ""             # what makes two sightings the same issue

    def to_dict(self) -> dict:
        return asdict(self)


# ---- 1. the app's own logs -----------------------------------------------------------------------

_THREADTIME = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d+\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+(.*?)\s*: ?(.*)$")
_EXCEPTION = re.compile(r"((?:[a-zA-Z_][\w$]*\.)+[A-Z][\w$]*(?:Exception|Error))(?::\s*(.*))?")
_SKIPPED = re.compile(r"Skipped (\d+) frames")
JANK_FRAMES = 30         # ~0.5 s at 60 Hz: a pause a user sees
LAUNCH_JANK_FRAMES = 90  # a cold start always skips frames on an emulator; at launch only a long freeze counts


def scan_logs(text: str, package: str, pids: set[str], jank_frames: int = JANK_FRAMES) -> list[Issue]:
    """Exceptions the app logged and went on (with a stack trace through the app's own code), main-thread jank and
    leaked windows, from `logcat -v threadtime`. Fatal crashes come from the crash buffer instead (see logcat.py)."""
    issues: list[Issue] = []
    groups: list[list[tuple[str, str, str]]] = []  # consecutive W/E lines of one thread and tag
    last = None
    for line in text.splitlines():
        m = _THREADTIME.match(line)
        if not m:
            continue
        pid, tid, level, tag, msg = m.groups()
        if "has leaked window" in msg and package in msg:
            issues.append(Issue("leaked_window", "medium", "A window leaked when its screen closed", msg.strip(),
                                "logs", evidence=[line.strip()], key=f"leak:{msg.split('that was originally')[0][:80]}"))
        if pids and pid not in pids:
            last = None
            continue
        sk = _SKIPPED.search(msg) if tag == "Choreographer" else None
        if sk and int(sk.group(1)) >= jank_frames:
            n = int(sk.group(1))
            issues.append(Issue("jank", "medium" if n >= 60 else "low", f"The main thread froze for {n} frames",
                                f"Android skipped {n} frames (about {n * 16.7 / 1000:.1f} s at 60 Hz): the app did "
                                "heavy work on the main thread", "logs", evidence=[line.strip()], key="jank"))
        if level not in "WE" or tag in ("AndroidRuntime",):  # AndroidRuntime is the fatal crash: crash buffer
            last = None
            continue
        if last == (pid, tid, tag):
            groups[-1].append((tag, level, msg))
        else:
            groups.append([(tag, level, msg)])
            last = (pid, tid, tag)
    for g in groups:
        frames = [msg.strip()[3:] for _, _, msg in g if msg.strip().startswith("at ")]
        app_frames = [f for f in frames if f.startswith(package)]
        exc = next((m for _, _, msg in g if not msg.strip().startswith("at ") for m in [_EXCEPTION.search(msg)] if m), None)
        if not exc or not app_frames:
            continue
        tag, headline = g[0][0], g[0][2].strip()
        name, message = exc.group(1), (exc.group(2) or "").strip()
        short = name.rsplit(".", 1)[-1]
        issues.append(Issue(
            "logged_exception", "high" if g[0][1] == "E" else "medium",
            f"{short} caught and only logged in {_frame_place(app_frames[0])}",
            f"The app hit {name}{': ' + message if message else ''} and carried on, logging it under '{tag}'"
            + (f" ('{headline}')" if headline and name not in headline else "")
            + ". The user is never told and whatever depended on it silently has no value.",
            "logs", evidence=[f"{tag}: {headline}", f"{name}: {message}"] + [f"at {f}" for f in app_frames[:4]],
            key=f"exc:{name}@{app_frames[0]}"))
    return issues


def _frame_place(frame: str) -> str:
    """'com.ex.ui.Foo.bar(Foo.kt:12)' -> 'Foo.kt:12'."""
    m = re.search(r"\(([^)]+)\)", frame)
    return m.group(1) if m else frame


# ---- 2. the screen -------------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"(?<![\w-])(null|NaN|undefined|Infinity)(?![\w-])|%(?:\d\$)?[sd]\b|\{\d+\}|\$\{\w+\}|"
                          r"lorem ipsum", re.I)
_NEGATIVE_MONEY = re.compile(r"[-−]\s*[₹$€£¥]\s*\d|[₹$€£¥]\s*[-−]\s*\d")
# Words that make a negative amount correct: a discount, a refund, a saving is shown with a minus on purpose.
_DEDUCTION = re.compile(r"\b(off|discount|coupon|promo|saved?|savings|refund|cashback|credit|deduct\w*|reward|"
                        r"rebate|voucher|less)\b", re.I)
# Screens whose content is not text: a map, a video, a camera preview, a game or drawing canvas.
_NON_TEXT_UI = re.compile(r"MapView|SurfaceView|TextureView|VideoView|PlayerView|WebView|GLSurfaceView|Canvas|"
                          r"PreviewView|ImageView", re.I)


def check_screen(screen: ui.Screen, package: str) -> list[Issue]:
    nodes = [n for n in screen.app_nodes(package)]
    issues: list[Issue] = []
    shown = [(n, t) for n in nodes if not n.editable for t in (n.text, n.desc) if t]
    for n, t in shown:
        # Content (web pages, list rows of articles or posts) is data written by people: a word like "null" or
        # "NaN" there is an article about it, not a value the app failed to fill in. Only the app's own UI text.
        m = None if n.content else _PLACEHOLDER.search(t)
        if m:  # standing alone ("nullable" doesn't count), and not in a field the user types into
            issues.append(Issue("placeholder_text", "high", f"The screen shows '{m.group(0)}' to the user",
                                f"'{t}' ({n.rid or n.cls}): a value that was never filled in reached the UI",
                                "screen", evidence=[t], key=f"ph:{n.rid or t[:40]}"))
        if _NEGATIVE_MONEY.search(t) and not _deduction(n, t, nodes):
            issues.append(Issue("negative_amount", "high", "A negative amount of money is shown",
                                f"'{t}' ({n.rid or n.cls})", "screen", evidence=[t], key=f"neg:{n.rid or t[:40]}"))
    if nodes and not shown and not any("ProgressBar" in n.cls or _NON_TEXT_UI.search(n.cls) for n in nodes):
        issues.append(Issue("blank_screen", "medium", "The screen is blank",
                            "the app is in front but shows no text, label or image description", "screen", key="blank"))
    rows: dict[str, int] = {}
    for n in nodes:
        if n.content and n.clickable and len(n.text) > 12:
            rows[n.text] = rows.get(n.text, 0) + 1
    for text, count in rows.items():
        if count > 1:
            issues.append(Issue("duplicate_row", "low", "The same list row appears twice",
                                f"'{text[:80]}' is shown {count} times", "screen", evidence=[text], key=f"dup:{text[:60]}"))
    return issues


def _deduction(node: ui.Node, text: str, nodes: list[ui.Node]) -> bool:
    """A minus that belongs there: the amount's own text, or a label on the same row, says it is a discount,
    a saving, a refund..."""
    if _DEDUCTION.search(text):
        return True
    cy, h = node.center[1], max(1, node.bounds[3] - node.bounds[1])
    return any(n is not node and abs(n.center[1] - cy) <= h and _DEDUCTION.search(n.text or n.desc or "")
               for n in nodes)


# ---- 3. the journey (a model reads the screenshots) ------------------------------------------------

JOURNEY_PROMPT = """You are a meticulous QA tester for an Android app. Below is one user journey, step by step: \
the step taken, then the visible text of the screen after it, then its screenshot.

What the app is meant to do (from its analysis and user flows):
{context}

Find FUNCTIONAL bugs a real user would hit on this journey: numbers that do not add up or contradict each other \
across steps (a price, a discount, a total, a count), a value that changes when it should not, a confirmation or \
message that says something wrong, an action whose result is not what its label promised, data shown that \
contradicts what was entered. Ignore visual design, wording style, slowness and anything you cannot see.

Every bug must quote, word for word, the text it is about from the screen text of the step it cites. If nothing \
is wrong, return an empty list. Reply with ONLY JSON:
{{"issues": [{{"title": "...", "step": <step number>, "quote": "exact text from that step's screen", \
"related_step": <another step number or null>, "related_quote": "exact text or null", \
"severity": "high|medium|low", "detail": "what is wrong and what the right value would be"}}]}}"""


def _screen_text(screen: ui.Screen, package: str) -> list[str]:
    out = []
    for n in screen.app_nodes(package):
        for t in (n.text, n.desc):
            if t and t not in out:
                out.append(t)
    return out


def _quoted(quote: str, texts: list[str]) -> bool:
    q = re.sub(r"\s+", " ", (quote or "").strip().lower())
    return bool(q) and any(q in re.sub(r"\s+", " ", t.lower()) for t in texts)


def review_journey(llm, states: list[dict], context: str, images: bool = True) -> tuple[list[Issue], dict]:
    """One model call per journey. Code keeps only issues whose quote really is on the step they cite."""
    from dataclasses import replace

    from langchain_core.messages import HumanMessage

    from .llm import chat_model
    from .pricing import cost_usd
    from .ux import _image_block

    content: list = [{"type": "text", "text": JOURNEY_PROMPT.format(context=context[:6000])}]
    for i, s in enumerate(states):
        content.append({"type": "text", "text": f"\nStep {i}: {s['step'] or 'launch the app'}\nScreen text: "
                                                + json.dumps(s["texts"], ensure_ascii=False)[:2500]})
        if images and s.get("png") and Path(s["png"]).exists():
            content.append(_image_block(Path(s["png"]), width=300))
    chat = chat_model(replace(llm, max_tokens=4000))
    ai = chat.invoke([HumanMessage(content=content)])
    u = getattr(ai, "usage_metadata", None) or {}
    usage = {"model": llm.model, "input_tokens": int(u.get("input_tokens") or 0),
             "output_tokens": int(u.get("output_tokens") or 0)}
    usage["cost_usd"] = cost_usd(llm.model, usage["input_tokens"], usage["output_tokens"]) or 0.0
    m = re.search(r"\{.*\}", ai.text, flags=re.S)
    try:
        raw = json.loads(m.group(0)).get("issues", []) if m else []
    except ValueError:
        raw = []
    issues, dropped = [], 0
    for r in raw:
        step = r.get("step")
        if not isinstance(step, int) or not 0 <= step < len(states) or not _quoted(r.get("quote", ""), states[step]["texts"]):
            dropped += 1  # not on the screen it cites: not observed, so not reported
            continue
        evidence = [f"step {step}: \"{r['quote']}\""]
        rel = r.get("related_step")
        if isinstance(rel, int) and 0 <= rel < len(states) and _quoted(r.get("related_quote") or "", states[rel]["texts"]):
            evidence.append(f"step {rel}: \"{r['related_quote']}\"")
        sev = r.get("severity") if r.get("severity") in SEVERITY_ORDER else "medium"
        issues.append(Issue("logic", sev, str(r.get("title", "Logic bug"))[:120], str(r.get("detail", "")), "journey",
                            step=step, evidence=evidence, key=f"logic:{str(r.get('title', ''))[:50].lower()}"))
    usage["dropped_unquoted"] = dropped
    return issues, usage


# ---- walking the app ------------------------------------------------------------------------------

def journeys(smap: ScreenMap) -> list[tuple[str, list[str]]]:
    """The recorded paths that are not the start of a longer one: walking them visits every mapped screen."""
    paths = {sid: list(s.path) for sid, s in smap.screens.items()}
    out = []
    for sid, p in sorted(paths.items(), key=lambda kv: -len(kv[1])):
        if any(len(q) > len(p) and q[:len(p)] == p for q in paths.values()):
            continue
        if any(q == p for _, q in out):
            continue
        out.append((sid, p))
    return out


@dataclass
class HuntResult:
    issues: list[Issue]
    out_dir: Path
    journeys: int
    steps: int
    seconds: float
    usage: dict
    notes: list[str]


def _slug(step: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", step.lower()).strip("-")[:40] or "launch"


@tracing.span(kind="WORKFLOW", name="hunt")
def hunt(ctx: Context, smap: ScreenMap, context: str = "", use_model: bool = True, echo=print) -> HuntResult:
    start = time.time()
    out = ctx.run_dir
    out.mkdir(parents=True, exist_ok=True)
    package = ctx.cfg.package
    by_path = {tuple(s.path): sid for sid, s in smap.screens.items()}
    found: dict[str, Issue] = {}
    usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "dropped_unquoted": 0}
    notes: list[str] = []
    total_steps = 0

    def add(issue: Issue, j: int, i: int, steps: list[str], state_files: list[str]) -> None:
        issue.step, issue.steps = i, steps[:i]
        issue.screen = issue.screen or by_path.get(tuple(steps[:i]), "")
        issue.evidence = issue.evidence + [f for f in state_files if f]
        k = issue.key or issue.title
        if k not in found or SEVERITY_ORDER[issue.severity] < SEVERITY_ORDER[found[k].severity]:
            found[k] = issue

    for j, (leaf, steps) in enumerate(journeys(smap)):
        jdir = out / f"journey-{j + 1}-{leaf}"
        driver = AppDriver(ctx, jdir)
        echo(f"journey {j + 1}: {len(steps)} steps to {leaf}")
        states: list[dict] = []
        pids: set[str] = set()
        try:
            driver.fresh(clear_data=True)  # every recorded path starts from a clean install, like attacks replay it
            for i in range(len(steps) + 1):
                if i > 0:
                    driver.step(steps[i - 1])
                screen, _, _ = driver.settle(timeout=driver.NAV_SETTLE)
                pid = ctx.device.pid()
                if pid:
                    pids.add(pid)
                label = f"{i:02d}-{_slug(steps[i - 1]) if i else 'launch'}"
                screen = driver.snapshot(label)
                files = [str((jdir / f"{label}.png").relative_to(out)), str((jdir / f"{label}.xml").relative_to(out))]
                texts = _screen_text(screen, package)
                states.append({"step": steps[i - 1] if i else "", "texts": texts, "png": str(jdir / f"{label}.png"),
                               "screen": by_path.get(tuple(steps[:i]), "")})
                # what the app logged while it got here
                log = ctx.adb.run("logcat", "-d", "-v", "threadtime", "-b", "main,system")
                (jdir / f"{label}.logcat.txt").write_text(log)
                for c in logcat.read_crashes(ctx.adb, package):
                    sig = c.signature(package)
                    add(Issue(c.kind, "high", f"{'ANR' if c.kind == 'anr' else c.exception.rsplit('.', 1)[-1]} "
                                              "in normal use", f"{c.exception}: {c.message}"[:300], "logs",
                              evidence=[sig] + [f"at {f}" for f in c.app_frames(package)[:4]], key=f"crash:{sig}"),
                        j, i, steps, files)
                for issue in scan_logs(log, package, pids, LAUNCH_JANK_FRAMES if i == 0 else JANK_FRAMES):
                    add(issue, j, i, steps, files + [str((jdir / f"{label}.logcat.txt").relative_to(out))])
                logcat.clear(ctx.adb)
                for issue in check_screen(screen, package):
                    add(issue, j, i, steps, files)
                total_steps += 1
        except (NavigationError, AdbError) as e:
            notes.append(f"journey {j + 1} ({leaf}) stopped at step {len(states)}: {e}")
            echo(f"  stopped: {e}")
        (jdir / "journey.json").write_text(json.dumps(
            [{k: v for k, v in s.items() if k != "png"} for s in states], indent=1, ensure_ascii=False))
        if use_model and len(states) > 1:
            try:
                logic, u = review_journey(ctx.cfg.llm, states, context)
                for k in ("input_tokens", "output_tokens", "cost_usd", "dropped_unquoted"):
                    usage[k] += u.get(k) or 0
                usage["model"] = u.get("model")
                for issue in logic:
                    i = issue.step
                    quote = issue.evidence[0].split(": ", 1)[-1].strip('"').lower()
                    if any(x.step == i and x.detector != "journey" and any(quote in e.lower() for e in x.evidence)
                           for x in found.values()):
                        continue  # the logs or the screen check already reported this, with harder evidence
                    label = f"{i:02d}-{_slug(steps[i - 1]) if i else 'launch'}"
                    add(issue, j, i, steps, [str((jdir / f"{label}.png").relative_to(out))])
            except Exception as e:  # the deterministic detectors still stand without the model
                notes.append(f"journey review skipped: {e}")
    try:
        ctx.device.restore()
    except AdbError:
        pass
    issues = sorted(found.values(), key=lambda x: (SEVERITY_ORDER[x.severity], x.step))
    res = HuntResult(issues, out, len(journeys(smap)), total_steps, round(time.time() - start, 1), usage, notes)
    (out / "issues.json").write_text(json.dumps({
        "issues": [x.to_dict() for x in issues], "journeys": res.journeys, "steps": res.steps,
        "seconds": res.seconds, "usage": usage, "notes": notes}, indent=2, ensure_ascii=False))
    write_report(res, package)
    return res


def write_report(res: HuntResult, package: str) -> Path:
    lines = [f"# Hunt: bugs in normal use · {package}", "",
             f"{len(res.issues)} issues on {res.journeys} journeys ({res.steps} steps) in {res.seconds:.0f}s, "
             "no fault injected.", ""]
    for n, x in enumerate(res.issues, 1):
        lines += [f"## {n}. {x.title}", "",
                  f"- **{x.severity}** · {x.kind} · found by the {x.detector}"
                  + (f" · screen `{x.screen}`" if x.screen else ""),
                  f"- {x.detail}", "", "Steps from launch:", ""]
        lines += [f"{k + 1}. `{s}`" for k, s in enumerate(x.steps)] or ["(on launch)"]
        lines += ["", "Evidence:", ""] + [f"- {e}" if not e.endswith((".png", ".xml", ".txt")) else f"- [{e}]({e})"
                                          for e in x.evidence] + [""]
    for note in res.notes:
        lines.append(f"> {note}")
    path = res.out_dir / "HUNT.md"
    path.write_text("\n".join(lines))
    return path
