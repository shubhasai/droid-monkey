"""Scenario generation from a screen map: generic rules, no app knowledge."""

from droid_monkey.explorer import ActionInfo, ScreenInfo, ScreenMap
from droid_monkey.scenarios import best, generate


def sample_map() -> ScreenMap:
    home = ScreenInfo("s0", [], "Home", [], ["list"], [
        ActionInfo("row_#", "First row", "row_#", effect="new_screen", to_screen="s1", settle_seconds=5, showed_progress=True),
        ActionInfo("text:Help", "Help", "help", effect="same_screen", settle_seconds=0.1),
    ])
    detail = ScreenInfo("s1", ["tap row_1"], "Detail", [], ["title"], [
        ActionInfo("open_form", "Edit", "open_form", effect="new_screen", to_screen="s2", settle_seconds=0.2),
    ])
    form = ScreenInfo("s2", ["tap row_1", "tap open_form"], "Profile", ["name", "email", "phone"], ["name", "email"], [
        ActionInfo("save", "Save", "save", effect="new_screen", to_screen="s3", settle_seconds=2, showed_progress=True),
        ActionInfo("avatar", "Photo", "avatar", effect="permission", permissions=["android.permission.CAMERA"]),
    ])
    return ScreenMap("com.example", {s.id: s for s in (home, detail, form)})


def test_every_attack_gets_a_candidate_with_a_reason():
    cands = generate(sample_map())
    for attack in ("process_death", "rotation", "network_loss", "permission_revoke", "low_memory", "performance"):
        c = best(cands, attack)
        assert c is not None and c.reason, attack


def test_best_candidates_follow_the_generic_rules():
    cands = generate(sample_map())
    assert best(cands, "process_death").screen == "s2"           # the form has the most to lose
    assert best(cands, "network_loss").action == "save"      # submitting a form with a progress indicator
    assert best(cands, "permission_revoke").action == "avatar"
    assert best(cands, "permission_revoke").permission == "android.permission.CAMERA"
    assert best(cands, "rotation").action == "row_#"          # longest in-flight work
    assert best(cands, "performance").action == "row_#"       # the slowest thing a user can do
    assert best(cands, "low_memory").screen == "s2"           # deep, busy screen with a form


def test_fast_actions_are_not_rotation_or_network_targets():
    cands = generate(sample_map())
    assert not any(c.action == "text:Help" for c in cands)


def test_map_round_trips_through_json(tmp_path):
    smap = sample_map()
    smap.save(tmp_path / "map.json")
    again = ScreenMap.load(tmp_path / "map.json")
    assert again.screens["s2"].fields == ["name", "email", "phone"]
    assert again.screens["s2"].actions[1].permissions == ["android.permission.CAMERA"]


def test_builtin_templates_render_for_this_app():
    from droid_monkey.specs import Known, builtin_templates, render
    smap = sample_map()
    known = Known(set(smap.screens), {a.selector for s in smap.screens.values() for a in s.actions},
                  {"android.permission.CAMERA"})
    cands = generate(smap)
    for name, template in builtin_templates().items():
        spec = render(template, best(cands, name).values(), known)
        assert spec.name == name and any("fault" in st for st in spec.steps)


def test_rotation_on_a_form_drops_the_trigger_steps():
    from droid_monkey.specs import Known, builtin_templates, render
    smap = sample_map()
    known = Known(set(smap.screens), {"row_#", "save", "avatar", "open_form", "text:Help"}, {"android.permission.CAMERA"})
    form = next(c for c in generate(smap) if c.attack == "rotation" and c.action is None)
    spec = render(builtin_templates()["rotation"], form.values(), known)
    assert [list(s)[0] for s in spec.steps] == ["fill", "fault", "settle"]


def test_a_form_outranks_a_deep_screen_for_process_death():
    smap = sample_map()
    smap.screens["deep"] = ScreenInfo("deep", ["tap x"] * 13, "Drawer", [], ["drawer"], [])
    assert best(generate(smap), "process_death").screen == "s2"
