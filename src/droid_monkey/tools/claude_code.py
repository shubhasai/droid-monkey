"""Run Claude Code headless (`claude -p`) as a tool: analysis now, the fixer in Block 3.

It runs on the user's Claude Code login. API keys are removed from its environment so it never bills the
API key droid-monkey uses for its own calls. Only stdout is parsed (global hooks may write to stderr).
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class ClaudeCodeError(RuntimeError):
    pass


@dataclass
class ClaudeResult:
    ok: bool
    text: str
    session_id: str
    cost_usd: float
    turns: int
    seconds: float
    usage: dict = field(default_factory=dict)


def claude_path() -> str:
    found = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    if not Path(found).exists():
        raise ClaudeCodeError("Claude Code CLI not found: install it and log in (`claude`)")
    return found


def run(
    prompt: str,
    cwd: Path,
    allowed_tools: list[str],
    model: str | None = None,
    resume: str | None = None,
    timeout: float = 900,
    mcp_config: Path | None = None,
    disallowed_tools: list[str] | None = None,
    name: str = "session",
    max_budget_usd: float | None = None,
) -> ClaudeResult:
    """One `claude -p` session, traced in neatlogs as an LLM span with Claude Code's own token and cost report."""
    from .. import tracing
    tracing.init()
    span = tracing.ClaudeCodeSpan(name, model, prompt)
    try:
        result = _run(prompt, cwd, allowed_tools, model, resume, timeout, mcp_config, disallowed_tools,
                      max_budget_usd)
    except ClaudeCodeError as e:
        span.end(error=str(e))
        raise
    span.end(result)
    return result


def _run(prompt, cwd, allowed_tools, model, resume, timeout, mcp_config, disallowed_tools,
         max_budget_usd=None) -> ClaudeResult:
    cmd = [claude_path(), "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
           "--allowedTools", ",".join(allowed_tools)]
    if mcp_config:
        cmd += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
    if model:
        cmd += ["--model", model]
    if resume:
        cmd += ["--resume", resume]
    if max_budget_usd:
        cmd += ["--max-budget-usd", f"{max_budget_usd:.2f}"]
    if disallowed_tools:
        cmd += ["--disallowedTools", *disallowed_tools]  # variadic flag: keep it last
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "DROID_MONKEY_API_KEY")}
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ClaudeCodeError(f"claude -p timed out after {timeout:.0f}s") from e
    out = proc.stdout.strip()
    try:
        data = json.loads(out[out.find("{"):]) if "{" in out else {}
    except json.JSONDecodeError as e:
        raise ClaudeCodeError(f"claude -p returned non-JSON output: {out[:300]}") from e
    if not data:
        raise ClaudeCodeError(f"claude -p failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
    return ClaudeResult(
        ok=not data.get("is_error", False),
        text=str(data.get("result", "")),
        session_id=data.get("session_id", ""),
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        turns=int(data.get("num_turns") or 0),
        seconds=round((data.get("duration_ms") or 0) / 1000, 1),
        usage=data.get("usage") or {},
    )
