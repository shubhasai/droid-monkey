import csv
import json
from types import SimpleNamespace

from droid_monkey import econ, loop


def _app(tmp_path):
    app = tmp_path / "shop"
    run = app / ".droid-monkey" / "runs" / "run-1"
    (run / "rotation").mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({"mode": "droid-monkey", "seconds": 140, "reasoning": {"cost_usd": 0.001},
        "outcomes": [{"attack": "rotation", "status": "fixed", "metrics": {"diag_cost_usd": 0.01, "coder_cost_usd": 0.49}},
                     {"attack": "leak", "status": "unfixed", "metrics": {"diag_cost_usd": 0.0, "coder_cost_usd": 0.5}},
                     {"attack": "net", "status": "survived", "metrics": {}}]}))
    (run / "rotation" / "verdict.json").write_text(json.dumps({"outcome": "crashed", "seconds": 30}))
    (app / ".droid-monkey" / "analysis-run.json").write_text(json.dumps({"cost_usd": 1.2}))
    return app


def test_econ_measures_runs_and_sets_tier_budgets(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    doc = econ.run([_app(tmp_path)], tmp_path / "business")
    m = doc["measured"]
    assert m["fix_attempts"] == 2 and m["verified_fixes"] == 1
    assert abs(m["cost_per_verified_fix"] - 1.0) < 1e-9 and m["attack_failure_rate"] == 1.0
    team = doc["tiers"]["team"]
    assert abs(team["fix_budget_per_run"] - round(99 / 30 * 0.3 - doc["compute_per_run"] - 0.001, 4)) < 1e-3
    assert doc["tiers"]["free"]["fix_budget_per_run"] == 0.0
    rows = {r["driver"]: r for r in csv.DictReader((tmp_path / "business" / "drivers.csv").open())}
    assert rows["cost_per_verified_fix"]["measured"] == "yes" and rows["team_price"]["measured"] == "no"
    assert "run-1" in rows["cost_per_fix_attempt_max"]["source"]
    assert econ.tier_budget("team") == team["fix_budget_per_run"]          # what run --tier loads


def test_run_stops_fixing_when_the_tier_budget_is_spent():
    lp = loop.Loop.__new__(loop.Loop)
    lp.fix_budget, lp.tier = 0.5, "team"
    lp.cfg = SimpleNamespace(llm=SimpleNamespace(model="claude-haiku-5-5"))
    lp.note = lambda state, msg: [msg]
    state = {"result": {"outcome": "crashed", "reason": "boom"}, "current": {"name": "rotation"},
             "outcomes": [{"metrics": {"diag_cost_usd": 0.01, "coder_cost_usd": 0.55}}]}
    d = lp.decide(state)
    assert d["decision"] == "next" and "fix budget spent" in d["decision_reason"]
    d = lp.decide({**state, "decision": "verify_failed", "attempts": 1})
    assert d["decision"] == "give_up"
    lp.fix_budget = None
    assert lp.within_budget(state) == (True, "")
