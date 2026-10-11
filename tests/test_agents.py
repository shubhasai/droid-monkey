"""Every coding agent gets the same job and limits: the file policy enforces Claude Code's rules for the API agent,
Codex gets the right sandbox and MCP servers, and `auto` picks the first agent that is set up."""

import json

import pytest

from droid_monkey import agents
from droid_monkey.coder import ALLOWED, DENIED
from droid_monkey.llm import LLMConfig


def test_the_api_agent_applies_the_fixers_rules(tmp_path):
    for f in ("README.md", "app/build.gradle.kts", "app/src/main/Main.kt", ".git/HEAD", "docs/bugs.txt"):
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x")
    policy = agents.FilePolicy(tmp_path, ALLOWED, DENIED)
    specs, impl = agents._file_tools(policy)
    assert {s["name"] for s in specs} == {"read_file", "list_files", "write_file", "edit_file"}  # no search: Grep denied
    assert impl["read_file"]("app/src/main/Main.kt").endswith("x")
    for blocked in ("README.md", ".git/HEAD", "docs/bugs.txt", "../outside.kt"):
        with pytest.raises(PermissionError):
            impl["read_file"](blocked)
    with pytest.raises(PermissionError):
        impl["write_file"]("app/build.gradle.kts", "implementation(\"evil\")")
    assert "wrote" in impl["write_file"]("app/src/test/MainTest.kt", "class MainTest")
    assert impl["list_files"]("**/*") == "app/build.gradle.kts\napp/src/main/Main.kt\napp/src/test/MainTest.kt"  # docs hidden


def test_codex_gets_a_write_sandbox_and_droid_monkeys_mcp_server():
    build = agents.McpServer("build", "droid_monkey.build_mcp", ["--app-dir", "/a"])
    cmd = agents.codex_command("fix it", ALLOWED + ["mcp__build__compile"], [build], None, None)
    assert cmd[:2] == ["codex", "exec"] and cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert 'mcp_servers.build.enabled_tools=["compile"]' in cmd and cmd[-1] == "fix it"
    read_only = agents.codex_command("read", ["Read", "Grep", "Glob"], [], "gpt-x", "thread-1")
    assert read_only[read_only.index("--sandbox") + 1] == "read-only"
    assert read_only[-4:] == ["gpt-x", "resume", "thread-1", "read"]


def test_codex_events_give_session_text_and_tokens():
    out = "\n".join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "fixed it"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 50}}])
    ev = agents.parse_codex_events(out)
    assert (ev["thread_id"], ev["text"]) == ("t-1", "fixed it")
    assert ev["usage"] == {"input_tokens": 600, "cache_read_input_tokens": 400, "output_tokens": 50}


def test_auto_picks_claude_then_codex_then_the_api(monkeypatch):
    monkeypatch.setattr("droid_monkey.tracing.load_keys", lambda: None)
    installed = set()
    monkeypatch.setattr(agents.shutil, "which", lambda name: name if name in installed else None)
    monkeypatch.delenv("DROID_MONKEY_API_KEY", raising=False)
    with pytest.raises(agents.AgentError, match="no coding agent"):
        agents.resolve("auto", LLMConfig())
    monkeypatch.setenv("DROID_MONKEY_API_KEY", "k")
    assert agents.resolve("auto", LLMConfig()) == "api"
    installed.add("codex")
    assert agents.resolve("auto", LLMConfig()) == "codex"
    installed.add("claude")
    assert agents.resolve(None, LLMConfig()) == "claude"
    assert agents.model_for("codex", "claude-sonnet-5-5", None) is None   # Codex uses its own default model
    assert agents.model_for("api", None, None) == "claude-sonnet-5-5"
