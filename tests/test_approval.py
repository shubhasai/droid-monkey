"""The run must stop and ask before changing code, and do exactly what the developer answered."""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from droid_monkey.approval import proposal, render
from droid_monkey.loop import Loop, RunState

RESULT = {"outcome": "froze", "reason": "1 frozen frame(s) over 700 ms", "evidence_dir": "/nonexistent",
          "metrics": {"frames": {"total": 330, "frozen": 1, "janky_pct": 2.0, "slow_ui_thread": 3, "p99_ms": 77}}}
DIAGNOSIS = {"summary": "move product lookup off the main thread", "root_cause": "Thread.sleep in the click handler",
             "files_to_change": ["app/src/main/java/Shop.kt"], "fix_approach": "launch on Dispatchers.Default",
             "test_plan": "ShopViewModelTest", "evidence": ["Shop.kt:56 - sleep in clickable"]}


def graph(auto_fix: bool = False, restored: list | None = None):
    loop = Loop.__new__(Loop)
    loop.auto_fix, loop.echo = auto_fix, lambda line: None
    loop._restore_last_good = lambda state: (restored if restored is not None else []).append(state["last_good"])
    g = StateGraph(RunState)
    g.add_node("approve", loop.approve)
    g.add_node("declined", loop.declined)
    g.add_node("fix", lambda s: {"log": s.get("log", []) + ["fixed"]})
    g.add_edge(START, "approve")
    g.add_conditional_edges("approve", lambda s: "fix" if s.get("approval") in ("yes", "all") else "declined",
                            ["fix", "declined"])
    g.add_edge("fix", END)
    g.add_edge("declined", END)
    return g.compile(checkpointer=InMemorySaver())


START_STATE = {"current": {"name": "performance", "reason": "slowest action"}, "result": RESULT,
               "diagnosis": DIAGNOSIS, "index": 0, "outcomes": [], "log": []}


def run(answer, auto_fix=False, state=None, restored=None):
    g, config = graph(auto_fix, restored), {"configurable": {"thread_id": "t"}}
    out = g.invoke(state or START_STATE, config)
    asked = out.get("__interrupt__")
    if asked:
        out = g.invoke(Command(resume=answer), config)
    return asked, out


def test_run_pauses_with_the_issue_evidence_and_fix():
    asked, _ = run("no")
    p = asked[0].value
    assert p["outcome"] == "froze" and p["fix_approach"] == "launch on Dispatchers.Default"
    assert any("1 frozen" in e for e in p["evidence"])
    assert "Suggested fix" in render(p) and "Shop.kt:56" in render(p)


def test_yes_fixes_and_no_keeps_the_suggestion():
    _, out = run("yes")
    assert out["log"][-1] == "fixed"
    _, out = run("no")
    assert out["outcomes"][0]["status"] == "declined"
    assert out["outcomes"][0]["suggestion"]["summary"] == DIAGNOSIS["summary"]


def test_all_stops_asking_and_auto_fix_never_asks():
    _, out = run("all")
    assert out["auto_fix_all"] and out["log"][-1] == "fixed"
    asked, out = run(None, auto_fix=True)
    assert not asked and out["log"][-1] == "fixed"


def test_proposal_lists_measured_memory():
    r = {**RESULT, "outcome": "leaked", "metrics": {"memory_before": {"activities": 1, "java_heap_kb": 12000},
                                                   "memory_after": {"activities": 7, "java_heap_kb": 14000}}}
    assert any("Activities 1 -> 7" in e for e in proposal({"name": "low_memory"}, r, DIAGNOSIS)["evidence"])


def test_a_retry_asks_again_and_no_restores_the_code():
    retry = {**START_STATE, "attempts": 1, "failure_note": "replay still froze", "last_good": "abc123"}
    restored = []
    asked, out = run("no", state=retry, restored=restored)
    assert asked[0].value["retry"] == {"attempt": 2, "max": 2, "previous_failed": "replay still froze"}
    assert "Retry 2 of 2" in render(asked[0].value)
    assert out["outcomes"][0]["status"] == "declined" and restored == ["abc123"]


def test_the_run_returns_to_the_developers_branch(tmp_path):
    import subprocess
    from types import SimpleNamespace
    from droid_monkey.tools import git
    g = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    g("init", "-q", "-b", "main"); (tmp_path / "a.txt").write_text("a"); g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    loop = Loop.__new__(Loop)
    loop.echo, loop.cfg = (lambda line: None), SimpleNamespace(app_dir=tmp_path)
    state = {"home_branch": "main", "branch": "droid-monkey/r1", "run_id": "r1", "log": []}

    g("checkout", "-qb", "droid-monkey/r1")
    loop._go_home(state)                       # nothing committed: back on main, empty branch removed
    assert git.current_branch(tmp_path) == "main"
    assert not subprocess.run(["git", "rev-parse", "--verify", "--quiet", "droid-monkey/r1"], cwd=tmp_path).returncode == 0

    g("checkout", "-qb", "droid-monkey/r1"); (tmp_path / "a.txt").write_text("fixed"); g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fix")
    loop._go_home(state)                       # a fix was committed: back on main, the branch is kept
    assert git.current_branch(tmp_path) == "main" and (tmp_path / "a.txt").read_text() == "a"
    assert subprocess.run(["git", "rev-parse", "--verify", "--quiet", "droid-monkey/r1"], cwd=tmp_path).returncode == 0
