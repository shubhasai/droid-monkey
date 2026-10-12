import time
from types import SimpleNamespace

from droid_monkey import config as config_mod
from droid_monkey.device_mcp import GraphRecorder
from droid_monkey.tools import claude_code


def _rec(max_screens=0, deadline=0.0, screens=0):
    rec = GraphRecorder.__new__(GraphRecorder)  # no device: only the limit logic
    rec.max_screens, rec.deadline = max_screens, deadline
    rec.map = SimpleNamespace(screens={f"s{i}": None for i in range(screens)})
    return rec


def test_no_limits_no_note():
    assert _rec().limit_note() == ""


def test_screen_limit():
    assert "LIMIT" in _rec(max_screens=3, screens=3).limit_note()
    assert _rec(max_screens=3, screens=2).limit_note() == ""


def test_time_limit_warns_before_deadline():
    assert "LIMIT" in _rec(deadline=time.time() + 30).limit_note()
    assert _rec(deadline=time.time() + 600).limit_note() == ""


def test_config_defaults(tmp_path):
    (tmp_path / "monkey.yaml").write_text("package: com.example\n")
    cfg = config_mod.load(tmp_path)
    assert (cfg.explore_minutes, cfg.explore_max_screens, cfg.explore_max_usd, cfg.verify_minutes) == (10, 60, None, 10)


def test_budget_flag(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return SimpleNamespace(stdout='{"result": "ok"}', stderr="", returncode=0)

    monkeypatch.setattr(claude_code.subprocess, "run", fake_run)
    monkeypatch.setattr(claude_code, "claude_path", lambda: "claude")
    claude_code._run("p", tmp_path, [], None, None, 5, None, None, max_budget_usd=2)
    assert seen["cmd"][seen["cmd"].index("--max-budget-usd") + 1] == "2.00"
