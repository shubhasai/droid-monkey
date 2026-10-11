"""`droid-monkey analyze`: static profile (no model) + a Claude Code read of the source -> ANALYSIS.md and flows.json.

Claude Code reads a clean copy of the source: no prose docs, scripts, git history or build output, so the
analysis comes from the code itself (docs can be stale, and a demo app's docs may describe its bugs).
"""

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .profile import AppProfile
from .tools import claude_code

OUT_DIR = ".droid-monkey"
FAULT_TYPES = ["process_death", "rotation", "network_loss", "permission_revoke", "dark_mode", "font_scale",
               "locale_rtl", "dont_keep_activities", "low_memory", "airplane_mode"]

_EXCLUDE_DIRS = {".git", "build", ".gradle", ".idea", ".entire", ".claude", OUT_DIR, "monkey-report", "repro",
                 "docs", "node_modules", "captures"}
_EXCLUDE_SUFFIXES = {".md", ".txt", ".sh", ".png", ".jpg", ".webp", ".jar", ".apk", ".zip"}


@dataclass
class AnalysisResult:
    markdown_path: Path
    flows_path: Path
    flows: list[dict]
    session_id: str
    cost_usd: float
    seconds: float


def source_snapshot(app_dir: Path, dest: Path) -> int:
    """Copy only code and build config into `dest`. Returns the number of files copied."""
    count = 0
    for f in app_dir.rglob("*"):
        rel = f.relative_to(app_dir)
        if not f.is_file() or (_EXCLUDE_DIRS & set(rel.parts)) or f.suffix.lower() in _EXCLUDE_SUFFIXES:
            continue
        if f.name in ("local.properties", "monkey.yaml") or f.name.startswith("."):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        count += 1
    return count


def _profile_brief(p: AppProfile) -> str:
    lines = [
        f"package: {p.package}  targetSdk: {p.target_sdk}  minSdk: {p.min_sdk}  ui: {p.ui_toolkit}",
        f"permissions: {', '.join(x.rsplit('.', 1)[-1] for x in p.permissions) or 'none'}",
        f"runtime (dangerous) permissions: {', '.join(x.rsplit('.', 1)[-1] for x in p.dangerous_permissions) or 'none'}",
        f"activities: {', '.join(a.name.rsplit('.', 1)[-1] + (' configChanges=' + '|'.join(a.config_changes) if a.config_changes else '') for a in p.activities)}",
        f"capabilities from dependencies: {', '.join(p.capabilities) or 'none'}",
        "static risk signals (generic patterns, may include false positives):",
    ]
    lines += [f"  - {s.kind} at {s.file}:{s.line}: {s.why}" for s in p.signals] or ["  - none"]
    return "\n".join(lines)


PROMPT = """You are the analysis step of droid-monkey, a tool that attacks Android apps with real-world faults
(the OS killing the app in the background, rotation and other configuration changes, network loss, revoked
permissions, low memory) and then fixes what breaks.

The current directory is a source-only copy of an Android app. A static scan already produced this profile:

{profile}

Read the code (start from the manifest's launcher activity and follow navigation) and write two files:

1. ANALYSIS.md, for developers. Sections:
   ## What the app does: 3-5 sentences.
   ## User flows worth testing: for each flow, the user's steps, the screens involved, the data the user would lose,
      and the main code locations (file:line).
   ## Risks by flow: for each risk, the fault type, why it would break, and the code evidence (file:line). Confirm or
      reject each static signal above after reading the code; say which ones are false positives.
   ## Recommended attacks: an ordered list, most likely to find a real bug first, each one sentence.

2. flows.json, for the tool, exactly this shape:
{{"flows": [{{
   "name": "snake_case_name",
   "description": "one sentence",
   "steps": ["user-level steps, e.g. open a product", "tap Buy now"],
   "ui_hints": ["visible labels or resource ids on the flow's screens that identify them"],
   "state_at_risk": ["what the user would lose"],
   "code": ["path/File.kt:line"],
   "risks": [{{"fault": "one of {faults}", "why": "one sentence", "code": "path/File.kt:line",
              "trigger": "the user action during which the fault should hit, e.g. tap Place order"}}]
}}]}}

Rules: base every claim on code you actually read and cite file:line. Use only the fault types listed. Do not modify
any other file. Keep ANALYSIS.md under 150 lines."""


def run(app_dir: Path, profile: AppProfile, model: str | None = "claude-sonnet-5-5") -> AnalysisResult:
    out_dir = app_dir / OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    profile.save(out_dir / "profile.json")

    with tempfile.TemporaryDirectory(prefix="droid-monkey-analysis-") as tmp:
        work = Path(tmp)
        source_snapshot(app_dir, work)
        prompt = PROMPT.format(profile=_profile_brief(profile), faults=", ".join(FAULT_TYPES))
        result = claude_code.run(prompt, cwd=work, allowed_tools=["Read", "Grep", "Glob", "Write"], model=model)
        md, fj = work / "ANALYSIS.md", work / "flows.json"
        if not result.ok or not md.exists() or not fj.exists():
            raise claude_code.ClaudeCodeError(f"analysis did not produce both files: {result.text[:300]}")
        flows = json.loads(fj.read_text())["flows"]
        shutil.copy2(md, out_dir / "ANALYSIS.md")
        (out_dir / "flows.json").write_text(json.dumps({"flows": flows}, indent=2))

    meta = {"session_id": result.session_id, "cost_usd": result.cost_usd, "seconds": result.seconds,
            "turns": result.turns, "model": model, "usage": result.usage}
    (out_dir / "analysis-run.json").write_text(json.dumps(meta, indent=2))
    return AnalysisResult(out_dir / "ANALYSIS.md", out_dir / "flows.json", flows, result.session_id,
                          result.cost_usd, result.seconds)

