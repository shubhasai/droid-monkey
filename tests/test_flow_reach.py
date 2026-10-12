from types import SimpleNamespace

from droid_monkey.designer import rank_by_reach


def _flow(name, files, faults):
    return {"name": name, "code": [f"{f}:10" for f in files], "risks": [{"fault": x, "code": f"{files[0]}:3"} for x in faults]}


def test_reached_flows_first_one_risk_per_flow_in_turn():
    smap = SimpleNamespace(
        screens={"home": SimpleNamespace(files=["Home.kt"]), "page": SimpleNamespace(files=["Page.kt", "PageVm.kt"]),
                 "stale": SimpleNamespace(files=["Edit.kt"])},
        unreached=[{"screen": "stale", "reason": "recorded path does not replay (fails)"}])
    edit = _flow("edit", ["Edit.kt"], ["process_death", "rotation"])   # its only screen's path is broken
    home = _flow("home", ["Home.kt"], ["network_loss", "rotation"])
    page = _flow("page", ["Page.kt"], ["process_death"])
    todo = [(f, r) for f in (edit, home, page) for r in f["risks"]]
    ranked, unreached = rank_by_reach(todo, smap)
    assert unreached == ["edit"]
    assert [(f["name"], r["fault"]) for f, r in ranked] == [("home", "network_loss"), ("page", "process_death"),
                                                           ("home", "rotation")]
