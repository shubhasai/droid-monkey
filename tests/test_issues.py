import json

from droid_monkey import issues, jobs


def _root(tmp_path):
    root = tmp_path / ".droid-monkey"
    a = root / "runs" / "attack-1"
    h = root / "runs" / "hunt-1"
    a.mkdir(parents=True)
    h.mkdir(parents=True)
    (a / "results.json").write_text(json.dumps([
        {"attack": "rotation", "outcome": "crashed", "crash_signature": "IllegalStateException @ Deal.kt:3", "screen": "detail"},
        {"attack": "network_loss", "outcome": "survived", "reason": "fine", "screen": "checkout"}]))
    (h / "issues.json").write_text(json.dumps({"issues": [
        {"title": "The screen shows '%d'", "detail": "Ships in %d days", "severity": "high", "key": "ph:dispatch",
         "screen": "detail", "steps": ["tap product_1"], "evidence": ["Ships in %d days", "j/01.png"]}]}))
    (root / "ux-audit.json").write_text(json.dumps({"findings": [
        {"id": "label_heart", "title": "Label the heart", "problem": "no label", "change": "add one", "severity": "medium",
         "screens": ["shop"], "evidence": ["shop: unlabeled"], "files": [], "observed": True, "status": "proposed"}]}))
    return root


def test_collect_and_fix_routes(tmp_path):
    found = {x.source: x for x in issues.collect(_root(tmp_path))}
    assert set(found) == {"attack", "hunt", "ux"}                 # the survived attack is not an issue
    assert found["attack"].fix_args == ["run", "--attacks", "rotation"]
    assert found["hunt"].fix_args == ["fix", found["hunt"].id] and found["hunt"].evidence == ["Ships in %d days"]


def test_a_later_pass_closes_the_issue(tmp_path):
    root = _root(tmp_path)
    later = root / "runs" / "attack-2"
    later.mkdir()
    (later / "results.json").write_text(json.dumps([{"attack": "rotation", "outcome": "survived", "reason": "ok"}]))
    import os
    os.utime(later, (9e9, 9e9))
    assert "attack" not in {x.source for x in issues.collect(root)}


def test_rank_without_jev_uses_severity_and_caches(tmp_path):
    root = _root(tmp_path)
    ranked, note = issues.rank(root, issues.collect(root), "com.ex", use_jev=False)
    assert ranked[0].source == "attack" and ranked[0].ranked_by == "severity"
    assert (root / "issues-rank.json").exists()


def test_fix_and_run_buttons_build_valid_commands():
    cmds = jobs.commands()
    assert jobs.argv(cmds["fix"], {"issue_id": "abc123"}) == ["fix", "abc123"]
    assert jobs.argv(cmds["run"], {"only": "low_memory_on_detailscreen"}) == ["run", "--attacks", "low_memory_on_detailscreen"]


def test_an_attack_named_in_an_older_report_still_runs():
    from droid_monkey import scenarios, specs
    from droid_monkey.explorer import ActionInfo, ScreenInfo, ScreenMap
    smap = ScreenMap(package="com.ex", screens={
        "shop": ScreenInfo(id="shop", path=[], title="Shop", fields=[], fingerprint=[]),
        "detail": ScreenInfo(id="detail", path=["tap p1"], title="Detail", fields=[], fingerprint=[]),
        "checkout": ScreenInfo(id="checkout", path=["tap p1", "tap buy"], title="Checkout", fields=["name"], fingerprint=[],
                               actions=[ActionInfo(selector="buy", label="Buy", group="", effect="new_screen", settle_seconds=2)])})
    known = specs.Known(screens=set(smap.screens), permissions=set(), selectors=set())
    a = scenarios.named_attack(smap, known, "low_memory_on_detail")
    assert a and a["screen"] == "detail" and a["spec"].screen == "detail" and a["spec"].name == "low_memory_on_detail"
    assert scenarios.named_attack(smap, known, "low_memory")["base"] == "low_memory"
    assert scenarios.named_attack(smap, known, "no_such_attack") is None


def test_fixed_issues_stay_listed_with_their_branch(tmp_path):
    import os
    root = _root(tmp_path)
    hunt = next(x for x in issues.collect(root) if x.source == "hunt")
    fx = root / "runs" / "fix-1"
    fx.mkdir()
    (fx / "fix.json").write_text(json.dumps({"issue": hunt.id, "key": "ph:dispatch", "source": "hunt", "title": hunt.title,
                                             "status": "fixed", "branch": "droid-monkey/fix-abc", "commit": "d054227cafe"}))
    run = root / "runs" / "run-1"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({"branch": "droid-monkey/run-1", "outcomes": [
        {"attack": "rotation", "status": "fixed", "before": "crashed", "commit": "709354b"}]}))
    os.utime(fx, (9e9, 9e9))
    os.utime(run, (9e9 + 1, 9e9 + 1))
    found = {x.source: x for x in issues.collect(root)}
    assert found["hunt"].status == "fixed" and found["hunt"].branch == "droid-monkey/fix-abc"
    assert found["hunt"].fix_args == ["pr", "fix-1"] and not found["hunt"].seen_after_fix
    assert found["attack"].status == "fixed" and found["attack"].branch == "droid-monkey/run-1"
    assert found["attack"].title.endswith("crashed") and found["ux"].status == "open"
    ranked, _ = issues.rank(root, list(found.values()), use_jev=False)
    assert ranked[-1].status == "fixed" and ranked[0].status == "open"


def test_fix_run_page_shows_branch_and_pr(tmp_path):
    from droid_monkey import live
    root = _root(tmp_path)
    fx = root / "runs" / "fix-20261012-162253"
    fx.mkdir()
    (fx / "fix.json").write_text(json.dumps({"issue": "abc", "key": "k", "source": "hunt", "title": "Negative money",
                                             "status": "fixed", "message": "fixed", "branch": "droid-monkey/fix-abc",
                                             "commit": "d054227cafe", "check": "re-walked on the device: gone"}))
    info = live.run_info(fx)
    assert info["fixed"] == 1 and not info["live"] and info["outcomes"] == [("Negative money", "fixed")]
    page = live.page_run(root, tmp_path, fx.name)
    assert "droid-monkey/fix-abc" in page and "prbtn" in page and "re-walked" in page and "running" not in page
