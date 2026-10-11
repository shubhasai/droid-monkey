"""The coding agent that reads, explores and fixes the app: Claude Code, Codex, or droid-monkey's own agent on the
Anthropic API for people who have neither CLI.

Every backend gets the same job description: a prompt, a working directory, the tools it may use (in Claude Code's
names: Read, Glob, Grep, Edit, Write, mcp__<server>__<tool>), what it must not touch (Claude Code permission rules
such as "Read(./**/*.md)"), and droid-monkey's MCP servers (device, build). Each enforces them as far as it can:

    claude  Claude Code (`claude -p`): allow and deny rules enforced by Claude Code; no shell.
    codex   Codex CLI (`codex exec`): read-only or workspace-write sandbox and the same MCP servers. Codex always has
            a shell inside its sandbox, so read restrictions (docs, git history) cannot be enforced; droid-monkey
            still discards any edit outside the allowed area afterwards.
    api     droid-monkey's own tool loop on the Anthropic API (the key in ~/.droid-monkey/.env): file tools that
            apply the same allow and deny rules, plus the MCP servers through an MCP client. No shell.

monkey.yaml:
    agent: auto          # auto | claude | codex | api; auto picks the first available in that order
    agent_model: null    # optional model for the agent (codex: an OpenAI model; api: a Claude model)
"""

import asyncio
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .tools.claude_code import ClaudeCodeError, ClaudeResult

BACKENDS = ("claude", "codex", "api")
NAMES = {"claude": "Claude Code", "codex": "Codex", "api": "Anthropic API (droid-monkey's own agent)"}
API_DEFAULT_MODEL = "claude-sonnet-5-5"


class AgentError(ClaudeCodeError):
    pass


@dataclass
class McpServer:
    """One of droid-monkey's own MCP servers, started as `python -m <module> <args>`."""
    name: str
    module: str
    args: list[str] = field(default_factory=list)

    @property
    def argv(self) -> list[str]:
        return ["-m", self.module, *self.args]


# ---- choosing the backend ------------------------------------------------------------------------

def _api_key_env(llm) -> str | None:
    env = getattr(llm, "api_key_env", "DROID_MONKEY_API_KEY")
    return env if os.environ.get(env) else None


def resolve(requested: str | None, llm) -> str:
    """The backend to use. `auto` takes the first one that is set up: Claude Code, then Codex, then the API key."""
    from . import tracing
    tracing.load_keys()
    requested = (requested or "auto").lower()
    have = {"claude": bool(shutil.which("claude")), "codex": bool(shutil.which("codex")),
            "api": bool(_api_key_env(llm)) and getattr(llm, "provider", "anthropic") == "anthropic"}
    if requested == "auto":
        for b in BACKENDS:
            if have[b]:
                return b
        raise AgentError("no coding agent available: install Claude Code (`claude`), or Codex (`codex`), or put an "
                         "Anthropic API key in ~/.droid-monkey/.env as DROID_MONKEY_API_KEY to use droid-monkey's "
                         "own agent (agent: api)")
    if requested not in BACKENDS:
        raise AgentError(f"unknown agent {requested!r}; use auto, claude, codex or api")
    if not have[requested]:
        need = {"claude": "Claude Code is not installed (`claude` not on PATH)",
                "codex": "Codex is not installed (`codex` not on PATH): npm install -g @openai/codex, then codex login",
                "api": "agent: api needs an Anthropic API key in ~/.droid-monkey/.env (DROID_MONKEY_API_KEY) "
                       "and llm.provider: anthropic"}[requested]
        raise AgentError(need)
    return requested


def model_for(backend: str, requested: str | None, agent_model: str | None) -> str | None:
    """droid-monkey's defaults are Claude model names; only pass a model the backend can use."""
    if agent_model:
        return agent_model
    if backend == "codex":
        return None if (requested or "").startswith("claude") else requested  # Codex's own default
    if backend == "api":
        return requested if (requested or "").startswith("claude") else API_DEFAULT_MODEL
    return requested


# ---- the one entry point -------------------------------------------------------------------------

def run(backend: str, prompt: str, cwd: Path, *, allowed_tools: list[str], disallowed_tools: list[str] | None = None,
        servers: list[McpServer] | None = None, model: str | None = None, resume: str | None = None,
        timeout: float = 900, name: str = "session", llm=None) -> ClaudeResult:
    servers = servers or []
    if backend == "claude":
        from .tools import claude_code
        with tempfile.TemporaryDirectory(prefix="droid-monkey-mcp-") as tmp:
            mcp_config = None
            if servers:
                mcp_config = Path(tmp) / "mcp.json"
                mcp_config.write_text(json.dumps({"mcpServers": {
                    s.name: {"type": "stdio", "command": sys.executable, "args": s.argv} for s in servers}}))
            return claude_code.run(prompt, cwd=cwd, allowed_tools=allowed_tools, model=model, resume=resume,
                                   timeout=timeout, mcp_config=mcp_config, disallowed_tools=disallowed_tools, name=name)
    if backend == "codex":
        return _traced("codex", name, model, prompt,
                       lambda: _codex(prompt, cwd, allowed_tools, servers, model, resume, timeout))
    if backend == "api":
        from . import tracing
        tracing.init()
        traced = tracing.span(kind="AGENT", name=f"api-agent.{name}")(_api)  # its model calls are priced per call
        return traced(prompt, cwd, allowed_tools, disallowed_tools or [], servers, model, resume, timeout, llm)
    raise AgentError(f"unknown agent {backend!r}")


def _traced(system: str, name: str, model: str | None, prompt: str, fn) -> ClaudeResult:
    from . import tracing
    tracing.init()
    span = tracing.ClaudeCodeSpan(name, model or system, prompt, system=system)
    try:
        result = fn()
    except AgentError as e:
        span.end(error=str(e))
        raise
    span.end(result)
    return result


# ---- Codex ---------------------------------------------------------------------------------------

def _toml(value) -> str:
    return json.dumps(value)  # JSON strings and arrays of strings are valid TOML values


def codex_command(prompt: str, allowed_tools: list[str], servers: list[McpServer], model: str | None,
                  resume: str | None) -> list[str]:
    writes = any(t in ("Edit", "Write") for t in allowed_tools)
    cmd = ["codex", "exec", "--json", "--skip-git-repo-check",
           "--sandbox", "workspace-write" if writes else "read-only", "-c", 'approval_policy="never"']
    for s in servers:
        tools = [t.split("__", 2)[2] for t in allowed_tools if t.startswith(f"mcp__{s.name}__")]
        key = f"mcp_servers.{s.name}"
        cmd += ["-c", f"{key}.command={_toml(sys.executable)}", "-c", f"{key}.args={_toml(s.argv)}",
                "-c", f"{key}.required=true", "-c", f"{key}.startup_timeout_sec=60",
                "-c", f"{key}.tool_timeout_sec=1200", "-c", f'{key}.default_tools_approval_mode="approve"']
        if tools:
            cmd += ["-c", f"{key}.enabled_tools={_toml(tools)}"]
    if model:
        cmd += ["-m", model]
    if resume:
        cmd += ["resume", resume]
    return cmd + [prompt]


def parse_codex_events(stdout: str) -> dict:
    out = {"thread_id": "", "text": "", "usage": {}, "error": "", "turns": 0}
    usage = {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type", "")
        if kind == "thread.started":
            out["thread_id"] = ev.get("thread_id", "")
        elif kind == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            out["text"] = ev["item"].get("text", "")
        elif kind == "turn.completed":
            out["turns"] += 1
            u = ev.get("usage") or {}
            cached = int(u.get("cached_input_tokens") or 0)
            usage["input_tokens"] += int(u.get("input_tokens") or 0) - cached
            usage["cache_read_input_tokens"] += cached
            usage["output_tokens"] += int(u.get("output_tokens") or 0)
        elif kind in ("turn.failed", "error"):
            out["error"] = json.dumps(ev.get("error") or ev.get("message") or ev)[:400]
    out["usage"] = usage
    return out


def _codex(prompt, cwd, allowed_tools, servers, model, resume, timeout) -> ClaudeResult:
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "DROID_MONKEY_API_KEY")}
    start = time.time()
    try:
        proc = subprocess.run(codex_command(prompt, allowed_tools, servers, model, resume), cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise AgentError(f"codex exec timed out after {timeout:.0f}s") from e
    ev = parse_codex_events(proc.stdout)
    if not ev["thread_id"] and proc.returncode != 0:
        raise AgentError(f"codex exec failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()[-400:]}")
    return ClaudeResult(ok=proc.returncode == 0 and not ev["error"], text=ev["text"] or ev["error"],
                        session_id=ev["thread_id"], cost_usd=0.0,  # Codex reports tokens, not a price
                        turns=ev["turns"], seconds=round(time.time() - start, 1), usage=ev["usage"])


# ---- droid-monkey's own agent on the Anthropic API -------------------------------------------------

_SESSIONS: dict[str, list] = {}  # session id -> messages, so a retry in the same run continues the conversation


class FilePolicy:
    """The Claude Code allow and deny rules, applied to droid-monkey's own file tools."""

    def __init__(self, root: Path, allowed: list[str], denied: list[str]):
        self.root = root.resolve()
        self.allowed = set(allowed)
        self.denied: dict[str, list[str]] = {}
        for rule in denied:
            m = re.match(r"^(\w+)\((.+)\)$", rule)
            if m:
                self.denied.setdefault(m.group(1), []).append(m.group(2).removeprefix("./"))
            else:
                self.allowed.discard(rule)  # a bare name ("Grep", "Bash") removes the whole tool

    def can(self, tool: str) -> bool:
        return tool in self.allowed

    def path(self, tool: str, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise PermissionError(f"{rel} is outside the project")
        relp = p.relative_to(self.root).as_posix()
        for pattern in self.denied.get(tool, []) + (self.denied.get("Edit", []) if tool == "Write" else []):
            if fnmatch.fnmatch(relp, pattern) or fnmatch.fnmatch(relp, pattern.removeprefix("**/")):
                raise PermissionError(f"{relp} is off limits ({tool}({pattern}))")
        return p

    def readable(self, rel: str) -> bool:
        try:
            self.path("Read", rel)
            return True
        except PermissionError:
            return False


SKIP_DIRS = {".git", "build", ".gradle", ".idea", ".kotlin", "node_modules", ".droid-monkey", ".entire"}


def _files(root: Path, policy: FilePolicy, pattern: str = "**/*") -> list[str]:
    out = []
    for p in root.glob(pattern):
        if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_DIRS):
            rel = p.relative_to(root).as_posix()
            if policy.readable(rel):
                out.append(rel)
    return sorted(out)


def _file_tools(policy: FilePolicy) -> tuple[list[dict], dict]:
    specs, impl = [], {}
    root = policy.root

    def add(name, description, props, required, fn):
        specs.append({"name": name, "description": description,
                      "input_schema": {"type": "object", "properties": props, "required": required}})
        impl[name] = fn

    if policy.can("Read"):
        def read_file(path: str, start: int = 1, end: int = 400) -> str:
            lines = policy.path("Read", path).read_text(errors="replace").splitlines()
            return "\n".join(f"{i}\t{line}" for i, line in enumerate(lines[start - 1:end], start)) or "(empty)"
        add("read_file", "Read lines [start, end] of a file (repo-relative path), numbered.",
            {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}}, ["path"], read_file)
    if policy.can("Glob"):
        def list_files(pattern: str = "**/*") -> str:
            return "\n".join(_files(root, policy, pattern)[:400]) or "no files"
        add("list_files", "List files matching a glob pattern such as **/*.kt.", {"pattern": {"type": "string"}}, [],
            list_files)
    if policy.can("Grep"):
        def search(regex: str, glob: str = "**/*") -> str:
            rx, hits = re.compile(regex), []
            for rel in _files(root, policy, glob):
                for i, line in enumerate((root / rel).read_text(errors="replace").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(hits) >= 200:
                            return "\n".join(hits) + "\n... more"
            return "\n".join(hits) or "no matches"
        add("search", "Regex search over the project's files; returns file:line matches.",
            {"regex": {"type": "string"}, "glob": {"type": "string"}}, ["regex"], search)
    if policy.can("Write"):
        def write_file(path: str, content: str) -> str:
            p = policy.path("Write", path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"wrote {path} ({len(content.splitlines())} lines)"
        add("write_file", "Create or overwrite a file with the given content.",
            {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"], write_file)
    if policy.can("Edit"):
        def edit_file(path: str, old: str, new: str) -> str:
            p = policy.path("Edit", path)
            text = p.read_text()
            if text.count(old) != 1:
                return f"edit failed: the old text occurs {text.count(old)} times in {path}; it must occur exactly once"
            p.write_text(text.replace(old, new))
            return f"edited {path}"
        add("edit_file", "Replace one exact, unique snippet of a file with new text.",
            {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
            ["path", "old", "new"], edit_file)
    return specs, impl


def _api(prompt, cwd, allowed_tools, disallowed_tools, servers, model, resume, timeout, llm) -> ClaudeResult:
    start = time.time()
    try:
        result = asyncio.run(asyncio.wait_for(
            _api_loop(prompt, Path(cwd), allowed_tools, disallowed_tools, servers, model, resume, llm), timeout))
    except TimeoutError as e:
        raise AgentError(f"the API agent timed out after {timeout:.0f}s") from e
    result.seconds = round(time.time() - start, 1)
    return result


async def _api_loop(prompt, cwd, allowed_tools, disallowed_tools, servers, model, resume, llm,
                    max_turns: int = 120) -> ClaudeResult:
    from contextlib import AsyncExitStack
    from dataclasses import replace

    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from .llm import LLMConfig, chat_model
    from .pricing import cost_usd

    policy = FilePolicy(cwd, allowed_tools, disallowed_tools)
    specs, impl = _file_tools(policy)
    base = llm or LLMConfig()
    chat = chat_model(replace(base, model=model or API_DEFAULT_MODEL, max_tokens=16000))

    async with AsyncExitStack() as stack:
        sessions = {}
        for s in servers:
            read, write = await stack.enter_async_context(stdio_client(StdioServerParameters(
                command=sys.executable, args=s.argv, env=dict(os.environ))))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            sessions[s.name] = session
            for t in (await session.list_tools()).tools:
                full = f"mcp__{s.name}__{t.name}"
                if full in allowed_tools:
                    specs.append({"name": f"{s.name}__{t.name}", "description": t.description or "",
                                  "input_schema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
                                                  or {"type": "object", "properties": {}}})

        bound = chat.bind_tools(specs)
        session_id = resume if resume in _SESSIONS else f"api-{int(time.time() * 1000)}"
        messages = _SESSIONS.get(resume) or [SystemMessage(
            "You are a coding agent working in the project in your current directory. Use the tools to read, "
            "explore and change it as the task asks; paths are relative to the project root. When the task is done, "
            "reply with a short summary and no tool calls.")]
        messages.append(HumanMessage(prompt))
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        text, turns = "", 0
        for turns in range(1, max_turns + 1):
            ai = await bound.ainvoke(messages)
            messages.append(ai)
            u = getattr(ai, "usage_metadata", None) or {}
            usage["input_tokens"] += int(u.get("input_tokens") or 0)
            usage["output_tokens"] += int(u.get("output_tokens") or 0)
            if not ai.tool_calls:
                text = ai.text
                break
            for call in ai.tool_calls:
                messages.append(ToolMessage(content=await _call(call, impl, sessions), tool_call_id=call["id"]))
        _SESSIONS[session_id] = messages
    cost = cost_usd(model or API_DEFAULT_MODEL, usage["input_tokens"], usage["output_tokens"]) or 0.0
    return ClaudeResult(ok=bool(text), text=text or f"stopped after {max_turns} turns without finishing",
                        session_id=session_id, cost_usd=cost, turns=turns, seconds=0.0, usage=usage)


async def _call(call: dict, impl: dict, sessions: dict) -> str:
    name, args = call["name"], call.get("args") or {}
    try:
        if name in impl:
            return str(impl[name](**args))[:20000]
        server, _, tool = name.partition("__")
        if server in sessions:
            res = await sessions[server].call_tool(tool, args)
            parts = [getattr(c, "text", "") or "" for c in (res.content or [])]
            return ("\n".join(parts) or "(no output)")[:20000]
        return f"unknown tool {name}"
    except Exception as e:  # tool errors go back to the model, which can correct itself
        return f"error: {e}"[:2000]
