"""`droid-monkey explore` driven by Claude Code: read the code to know every screen and its files, then drive the
emulator through droid-monkey's device MCP server to reach them all and record the screen graph."""

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .analysis import _profile_brief, source_snapshot
from .config import Target
from .driver import AppDriver, Context, NavigationError
from .explorer import ScreenMap, _similar
from .tools.adb import AdbError
from .profile import AppProfile
from .tools import claude_code

DEVICE_TOOLS = ["launch_fresh", "screen", "tap", "type_text", "scroll", "back", "allow_permission",
                "deny_permission", "record_screen", "graph_status", "finish"]

PROMPT = """You are the explorer inside droid-monkey, a tool that attacks Android apps with real-world faults and fixes
what breaks. Your job: build a complete map of this app's screens, as a graph, with the source files behind each one.

The current directory is a source-only copy of the app. Static profile:
{profile}
{flows}
You have Read, Grep and Glob over the source, and device tools (mcp__device__*) that drive the running app on the
emulator. Every action you take on the device is recorded and must be replayable, so act only through those tools.

Work in this order:
1. Read the code first: find the launcher activity and how navigation works (nav graph, routes, fragments,
   activities, screen composables, deep links). Make a checklist of every screen, and for each the files that
   implement it: the screen itself, its ViewModel or presenter, and the repositories or services it calls.
2. Call launch_fresh to start. It always clears the app's data, so every recorded path starts from a first-time
   user and replays on its own: when a screen needs data (an item to open, a task to edit), create it on the way
   there after each launch_fresh.
3. Breadth-first: on each new screen, call record_screen with a short name, one sentence on its purpose, and its
   files from your checklist (paths relative to the repo root). Then try every distinct interactive element on it.
   Treat repeated list rows as one unless the code shows they open different screens.
4. Reach each screen with every state that changes where you can go: fill forms with values that pass the app's
   validation (read the validation code), open menus, dialogs and tabs, scroll lists to find items below the fold,
   and answer permission prompts (allow, and where it matters also deny). Do not enumerate every order of taps.
5. When a branch is done or the app crashes, use launch_fresh and navigate to the next unexplored screen,
   recreating any data it needs. Use graph_status to see untried elements.
6. Stop when every screen in your checklist is reached or you know why it cannot be (login wall, needs real
   payment, needs a notification or deep link you cannot trigger, debug-only). Then call finish with the
   unreached screens as 'Name: reason' and short notes.

Only record the app's own screens: system permission prompts and other apps are not screens (answer prompts with
allow_permission / deny_permission). An app dialog that blocks the screen (e.g. a popup with OK) is worth recording.
Rules: never type real personal data; use obvious test values. Do not leave the app on purpose. A crash is a finding,
not a failure: it is recorded automatically, so relaunch and continue. Keep going until the checklist is covered."""


@dataclass
class ClaudeExploreResult:
    smap: ScreenMap
    cost_usd: float
    seconds: float
    session_id: str
    summary: str


def preflight() -> None:
    """Make sure the device MCP server can start in this Python before spending a Claude session on it."""
    proc = subprocess.run([sys.executable, "-c", "import droid_monkey.device_mcp"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise claude_code.ClaudeCodeError(
            "the device server cannot start in this environment: " + proc.stderr.strip().splitlines()[-1]
            + "\nIf droid-monkey is installed as a uv tool, refresh it: "
              "uv tool install --editable --reinstall <path to droid-monkey>")


def run(app_dir: Path, profile: AppProfile, out: Path, flows: list[dict], model: str = "claude-sonnet-5-5",
        timeout: float = 3600) -> ClaudeExploreResult:
    preflight()
    out.parent.mkdir(parents=True, exist_ok=True)
    flows_text = ""
    if flows:
        flows_text = "\nFlows found by the earlier code analysis:\n" + "\n".join(
            f"- {f.get('name')}: {f.get('description', '')}" for f in flows)
    with tempfile.TemporaryDirectory(prefix="droid-monkey-explore-") as tmp:
        work = Path(tmp) / "src"
        work.mkdir()
        source_snapshot(app_dir, work)
        mcp_config = Path(tmp) / "mcp.json"
        mcp_config.write_text(json.dumps({"mcpServers": {"device": {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-m", "droid_monkey.device_mcp", "--app-dir", str(app_dir), "--source", str(work),
                     "--out", str(out)],
        }}}))
        result = claude_code.run(
            PROMPT.format(profile=_profile_brief(profile), flows=flows_text),
            cwd=work,
            allowed_tools=["Read", "Grep", "Glob"] + [f"mcp__device__{t}" for t in DEVICE_TOOLS],
            model=model,
            timeout=timeout,
            mcp_config=mcp_config,
            name="explore",
        )
    if not out.exists():
        raise claude_code.ClaudeCodeError(f"exploration produced no graph: {result.text[:400]}")
    return ClaudeExploreResult(ScreenMap.load(out), result.cost_usd, result.seconds, result.session_id, result.text)


def verify_paths(ctx: Context, smap: ScreenMap) -> dict[str, str]:
    """Replay every screen's recorded path from a fresh launch and check it lands on that screen."""
    driver = AppDriver(ctx, ctx.run_dir / "verify-paths")
    results: dict[str, str] = {}
    for sid, info in smap.screens.items():
        try:
            driver.fresh(clear_data=True)
            driver.go_to(Target(name=sid, steps=list(info.path)))
            screen, _, _ = driver.settle()
            ok = _similar(screen.fingerprint(smap.package), frozenset(info.fingerprint))
            results[sid] = "ok" if ok else "lands on a different screen"
        except (NavigationError, AdbError) as e:
            results[sid] = f"fails: {e}"
    try:
        ctx.device.restore()
    except AdbError:
        pass
    return results
