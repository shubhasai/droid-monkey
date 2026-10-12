"""neatlogs incident 981cb137: the explorer returned an empty response without mapping screens, silently."""

import json
import os
import time
from types import SimpleNamespace

import pytest

from droid_monkey import claude_explore
from droid_monkey.tools import claude_code


def test_empty_or_errored_session_is_a_failure():
    assert claude_code.failure({"result": "", "usage": {"output_tokens": 0}, "num_turns": 1}).startswith("empty")
    assert claude_code.failure({"is_error": True, "subtype": "error_max_budget_usd", "result": ""}) == "error_max_budget_usd"
    assert claude_code.failure({"result": "mapped 4 screens", "usage": {"output_tokens": 900}}) == ""


def _setup(tmp_path, monkeypatch, sessions):
    out = tmp_path / ".droid-monkey" / "screen-map.json"
    out.parent.mkdir()
    out.write_text(json.dumps({"package": "p", "screens": {"old": {"id": "old", "path": [], "title": "Old", "fields": [],
                                                                    "fingerprint": [], "actions": []}}}))
    os.utime(out, (time.time() - 3600, time.time() - 3600))  # a map from an earlier run
    calls = []

    def fake_run(agent, prompt, **kw):
        calls.append(kw["name"])
        writes, res = sessions[len(calls) - 1]
        if writes:
            out.write_text(json.dumps({"package": "p", "screens": {"home": {"id": "home", "path": [], "title": "Home",
                                                                           "fields": [], "fingerprint": [], "actions": []}}}))
        return res
    monkeypatch.setattr(claude_explore, "preflight", lambda: None)
    monkeypatch.setattr(claude_explore, "source_snapshot", lambda *a: None)
    monkeypatch.setattr("droid_monkey.agents.run", fake_run)
    monkeypatch.setattr("droid_monkey.agents.McpServer", lambda *a, **k: None)
    monkeypatch.setattr("droid_monkey.config.load", lambda d: SimpleNamespace(package="p", module="app", variant="debug", entry_steps=[]))
    monkeypatch.setattr("droid_monkey.inventory.build", lambda *a: [])
    monkeypatch.setattr("droid_monkey.inventory.save", lambda *a: None)
    monkeypatch.setattr("droid_monkey.inventory.brief", lambda *a: "")
    monkeypatch.setattr(claude_explore, "_profile_brief", lambda p: "")
    return out, calls


def _res(ok, text="", error=""):
    return claude_code.ClaudeResult(ok, text, "s", 0.01, 1, 1.0, {}, error)


def test_an_empty_explore_is_retried_and_never_returns_the_old_map(tmp_path, monkeypatch):
    out, calls = _setup(tmp_path, monkeypatch, [(False, _res(False, error="empty: no output")),
                                                (False, _res(False, error="empty: no output"))])
    with pytest.raises(claude_code.ClaudeCodeError, match="recorded no screens: empty"):
        claude_explore.run(tmp_path, None, out, [], minutes=10)
    assert calls == ["explore", "explore.retry"]
    assert "old" in json.loads(out.read_text())["screens"]  # the earlier map is left as it was


def test_a_retry_that_maps_screens_succeeds(tmp_path, monkeypatch):
    out, calls = _setup(tmp_path, monkeypatch, [(False, _res(False, error="empty: no output")), (True, _res(True, "done"))])
    r = claude_explore.run(tmp_path, None, out, [], minutes=10)
    assert calls == ["explore", "explore.retry"] and list(r.smap.screens) == ["home"]
