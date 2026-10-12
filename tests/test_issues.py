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
