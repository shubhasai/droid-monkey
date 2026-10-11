"""Coder: Claude Code (claude -p) fixes the root cause and writes a regression test, in the app repo.

It runs in the app repo so Entire's hooks record the session, but it can only read and edit code: no shell, no git,
no docs or scripts (they can describe the bugs), and no build files (no new dependencies). droid-monkey commits the
result itself after checking which files changed. A failed fix is retried by resuming the same session.
"""

import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .diagnoser import Diagnosis
from .tools import claude_code, git

ALLOWED = ["Read", "Edit", "Write", "Glob"]
DENIED = [
    "Bash", "Grep", "WebFetch", "WebSearch",
    "Read(./**/*.md)", "Read(./*.md)", "Read(./**/*.sh)", "Read(./*.sh)", "Read(./docs/**)",
    "Read(./.droid-monkey/**)", "Read(./.git/**)", "Read(./.entire/**)",
    "Edit(./**/*.gradle.kts)", "Edit(./**/*.gradle)", "Edit(./gradle/**)", "Edit(./**/*.toml)", "Edit(./**/*.properties)",
    "Write(./**/*.gradle.kts)", "Write(./**/*.gradle)", "Write(./gradle/**)", "Write(./**/*.toml)",
    "Write(./**/*.properties)", "Edit(./.droid-monkey/**)", "Write(./.droid-monkey/**)",
]

BRIEF = """droid-monkey attacked this Android app with a real-world fault and it failed. Fix the root cause.

## What happened
{failure}

## Diagnosis
Root cause: {root_cause}
Fix approach: {fix_approach}
Evidence: {evidence}

## Files to change
{files}

## Regression test
{test_plan}

## Rules
- Fix the root cause in the files above with the idiomatic Android approach. Keep the change small and focused.
- Add exactly one regression test file under {module}/src/test (JVM unit test, preferred) or {module}/src/androidTest
  (instrumented), using only test libraries the project already has (see {module}/build.gradle.kts). It must fail on
  the old code and pass on the fixed code.
- Do not change build files or add dependencies. Do not touch unrelated code. Do not commit.
- Before you finish, call the compile tool on your test ('unit' or 'instrumented') and fix every error it reports.
- When done, reply with one short paragraph: what you changed and the test class name."""

RETRY = """Your fix did not pass verification.

{failure_note}

The diagnosis was revised:
Root cause: {root_cause}
Fix approach: {fix_approach}
Files to change: {files}

Rework the fix (and the test if needed) following the same rules, and call the compile tool before you finish.
Do not commit."""


@dataclass
class CoderResult:
    ok: bool
    summary: str
    session_id: str
    cost_usd: float
    seconds: float
    tokens: int = 0  # input (incl. cache) + output tokens of the Claude Code session
    changed: list[str] = field(default_factory=list)       # files kept (inside the module's src)
    rejected: list[str] = field(default_factory=list)      # files changed outside the allowed area, discarded
    test_files: list[str] = field(default_factory=list)


def fix(app_dir: Path, module: str, failure: str, diagnosis: Diagnosis, model: str,
        resume_session: str | None = None, failure_note: str = "", variant: str = "debug",
        java_home: str | None = None, compile_tool: bool = True) -> CoderResult:
    if resume_session:
        prompt = RETRY.format(failure_note=failure_note, root_cause=diagnosis.root_cause,
                              fix_approach=diagnosis.fix_approach, files=", ".join(diagnosis.files_to_change))
    else:
        prompt = BRIEF.format(failure=failure, root_cause=diagnosis.root_cause, fix_approach=diagnosis.fix_approach,
                              evidence="; ".join(diagnosis.evidence) or "-",
                              files="\n".join(f"- {f}" for f in diagnosis.files_to_change)
                              or "Not diagnosed: find the root cause and the files to change yourself.",
                              test_plan=diagnosis.test_plan, module=module)
    before = git.file_states(app_dir)  # anything already changed or untracked is not the coder's to keep or discard
    with tempfile.TemporaryDirectory(prefix="droid-monkey-coder-") as tmp:
        mcp_config, allowed = None, list(ALLOWED)
        if compile_tool:  # the one capability beyond editing: compile, through a fixed command
            args = ["-m", "droid_monkey.build_mcp", "--app-dir", str(app_dir), "--module", module, "--variant", variant]
            if java_home:
                args += ["--java-home", java_home]
            mcp_config = Path(tmp) / "mcp.json"
            mcp_config.write_text(json.dumps({"mcpServers": {"build": {"type": "stdio", "command": sys.executable,
                                                                        "args": args}}}))
            allowed.append("mcp__build__compile")
        else:
            prompt = prompt.replace("- Before you finish, call the compile tool on your test ('unit' or 'instrumented') "
                                    "and fix every error it reports.\n", "").replace(
                                    ", and call the compile tool before you finish", "")
        res = claude_code.run(prompt, cwd=app_dir, allowed_tools=allowed, model=model, resume=resume_session,
                              timeout=1200, disallowed_tools=DENIED, mcp_config=mcp_config)
    after = git.file_states(app_dir)
    touched = [p for p, h in after.items() if before.get(p) != h]

    # On a retry the previous attempt's edits are still in the tree: they count as the coder's work.
    previous = [p for p in after if p.startswith(f"{module}/src/") and p in before and resume_session]
    keep = sorted({p for p in touched + previous if p.startswith(f"{module}/src/")})
    rejected = [p for p in touched if not p.startswith(f"{module}/src/") and not p.startswith(".droid-monkey/")]
    if rejected:
        git.discard(app_dir, rejected)
    changed = keep
    tests = [p for p in keep if f"{module}/src/test/" in p or f"{module}/src/androidTest/" in p]
    u = res.usage or {}
    tokens = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                "cache_creation_input_tokens", "output_tokens"))
    return CoderResult(res.ok and bool(keep), res.text, res.session_id, res.cost_usd, res.seconds, tokens, keep,
                       rejected, tests)


def failure_text(result: dict, spec: dict) -> str:
    lines = [f"Attack `{spec.get('name')}` ({spec.get('fault_type')}): {spec.get('description')}",
             f"Steps: {json.dumps(spec.get('steps'))}",
             f"Result: {result.get('outcome')}: {result.get('reason')}"]
    if result.get("crash_signature"):
        lines.append(f"Crash: {result['crash_signature']}")
        lines += [f"  at {f}" for f in (result.get("app_frames") or [])[:6]]
    if result.get("lost_fields"):
        lines.append(f"Fields lost (before -> after): {result['lost_fields']}")
    return "\n".join(lines)
