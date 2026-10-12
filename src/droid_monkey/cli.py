"""droid-monkey command line."""

import json
import sys
import time
from pathlib import Path

import click

from . import analysis as analysis_mod
from . import config as config_mod
from . import profile as profile_mod
from . import scenarios as scenarios_mod
from . import paths
from . import specs as specs_mod
from . import theme
from .theme import style
from .config import Target
from .driver import Context
from .explorer import Explorer, ScreenMap
from .runner import SpecRunner
from .tools import emulator, gradle
from .tools.adb import Adb
from .tools.device import Device

BUILTIN = specs_mod.builtin_templates()

APP_DIR = click.option("--app-dir", type=click.Path(path_type=Path, file_okay=False), default=".",
                       help="The app's repo (holds monkey.yaml).")


def _context(app_dir: Path, run_id: str) -> Context:
    cfg = config_mod.load(app_dir)
    serial = emulator.running_emulator()
    adb = Adb(serial) if serial else emulator.boot(cfg.avd or emulator.list_avds()[0])
    run_dir = paths.run_dir(cfg.app_dir, run_id)
    return Context(adb=adb, device=Device(adb, cfg.package), cfg=cfg, run_dir=run_dir)


def _map_path(ctx: Context) -> Path:
    return paths.map_path(ctx.cfg.app_dir)


def _explore(ctx: Context) -> ScreenMap:
    explorer = Explorer(ctx)
    smap = explorer.explore()
    smap.save(_map_path(ctx))
    for note in explorer.notes:
        theme.note(note)
    return smap


def _print_map(smap: ScreenMap) -> None:
    theme.header("screen map")
    click.echo(f"  {style(len(smap.screens), bold=True)} screens, {style(len(smap.edges), bold=True)} edges, "
               f"explored in {smap.seconds:.0f}s "
               + style(f"({smap.mode} mode; screen-read overhead {smap.baseline_seconds:.1f}s)", fg="bright_black"))
    for s in smap.screens.values():
        if s.files:
            click.echo(_screen_title(s.id, s.title) + (style(f"  {s.description}", fg="bright_black") if s.description else ""))
            click.echo(style("      files:  ", fg="bright_black") + ", ".join(s.files))
            if s.fields:
                click.echo(style("      fields: ", fg="bright_black") + ", ".join(s.fields))
            for a in s.actions:
                bits = [a.effect] + ([f"-> {a.to_screen}"] if a.to_screen else []) \
                    + ([f"slow {a.settle_seconds:.1f}s"] if (a.showed_progress or a.settle_seconds >= 1.5) else []) \
                    + (["asks " + ", ".join(p.rsplit('.', 1)[-1] for p in a.permissions)] if a.permissions else []) \
                    + ([style(f"CRASH {a.crash}", fg="red", bold=True)] if a.crash else [])
                click.echo(f"      {_branch()} {a.label[:40]:40} {' '.join(bits)}")
            continue
        form = f", form with {len(s.fields)} fields" if s.has_form else ""
        click.echo(_screen_title(s.id, s.title or "(untitled)")
                   + style(f"  depth {len([p for p in s.path if p.startswith('tap')])}{form}", fg="bright_black"))
        for a in s.actions:
            extra = []
            if a.to_screen:
                extra.append(f"-> {a.to_screen}")
            if a.showed_progress or a.settle_seconds >= 1.5:
                extra.append(f"slow {a.settle_seconds:.1f}s")
            if a.permissions:
                extra.append("asks " + ", ".join(p.rsplit('.', 1)[-1] for p in a.permissions))
            if a.crash:
                extra.append(style(f"CRASH {a.crash}", fg="red", bold=True))
            click.echo(f"      {_branch()} {a.label[:40]:40} {a.effect:12} {' '.join(extra)}")
    for c in smap.crashes:
        theme.bad(f"crash during exploration: {c}")
    for u in smap.unreached:
        theme.warn(f"not reached: {u.get('screen')}: {u.get('reason')}")


def _screen_title(sid: str, title: str) -> str:
    return "  " + style(f"[{sid}]", fg="cyan") + " " + style(title, bold=True)


def _branch() -> str:
    return theme.arrow()


def _load_app(ctx: Context, re_explore: bool = False):
    """Screen map (explored if missing), targets, static profile, flows and saved specs for this app."""
    if re_explore or not _map_path(ctx).exists():
        theme.note("no screen map yet, so the monkey explores the app first...")
        smap = _explore(ctx)
        _print_map(smap)
    else:
        smap = ScreenMap.load(_map_path(ctx))
    ctx.targets = {sid: Target(name=sid, steps=list(s.path), fill=s.has_form) for sid, s in smap.screens.items()}
    prof = profile_mod.analyze(ctx.cfg.app_dir, ctx.cfg.package, ctx.cfg.module, ctx.cfg.variant)
    known = specs_mod.Known(
        screens=set(smap.screens),
        selectors={a.selector for s in smap.screens.values() for a in s.actions} | {f for s in smap.screens.values() for f in s.fields},
        permissions=set(prof.dangerous_permissions),
    )
    saved, broken = specs_mod.load_all(ctx.cfg.app_dir, known)
    for fname, errors in broken.items():
        theme.bad(f"invalid spec {fname}: {'; '.join(errors)}")
    return smap, prof, saved, known


def _echo_result(name: str, result) -> None:
    detail = result.exception or result.reason
    lines = [detail] if detail else []
    if result.crash_signature:
        lines.append(f"at {result.crash_signature}")
    lines += [f"{key}: {old!r} -> {new!r}" for key, (old, new) in result.lost_fields.items()]
    lines.append(f"took {result.seconds:.0f}s")
    label = theme.BADGES.get(result.outcome, ("", "", result.outcome))[2].upper()
    theme.monkey_says(result.outcome, f"{label}  {name}", lines)


@click.group()
@click.version_option(package_name="droid-monkey", message="%(prog)s %(version)s  (o.o)")
def main() -> None:
    """(o.o) Attack an Android app with real-world faults, fix what breaks, prove each fix."""


@main.command()
@APP_DIR
@click.option("--force", is_flag=True, help="Overwrite an existing monkey.yaml.")
def init(app_dir: Path, force: bool) -> None:
    """Write monkey.yaml for this Android repo and check everything droid-monkey needs."""
    from . import init_cmd
    theme.banner("init")
    app_dir = Path(app_dir).resolve()
    cfg_path = app_dir / config_mod.CONFIG_NAME
    theme.header("monkey.yaml")
    if cfg_path.exists() and not force:
        theme.note(f"{cfg_path.name} already exists (use --force to rewrite it)")
    else:
        d = init_cmd.detect(app_dir)
        if not d.package or not d.module:
            raise click.ClickException("no Android application module with an applicationId found here; "
                                       "run init from the app repo's root")
        init_cmd.write_config(app_dir, d)
        theme.ok(f"wrote {cfg_path}")
        theme.kv("package", d.package)
        theme.kv("module", d.module)
        theme.kv("variant", d.variant)
        theme.kv("avd", str(d.avd))
    theme.header("checking the jungle", "(o_O)")
    missing = 0
    for name, ok, hint in init_cmd.checks(app_dir):
        if ok:
            click.echo(f"  {style('(^.^) ok     ', fg='green')} {name}")
        else:
            missing += 1
            click.echo(f"  {style('(x_x) MISSING', fg='red', bold=True)} {name}  "
                       + style(f"({hint})", fg="bright_black"))
    click.echo("")
    if missing:
        theme.monkey_says("worried", f"{missing} THING{'S' if missing != 1 else ''} MISSING",
                          ["the monkey can't swing without them"])
    else:
        theme.monkey_says("survived", "ALL SET", ["the jungle is ready", "next: droid-monkey analyze"])


AGENT = click.option("--agent", type=click.Choice(["auto", "claude", "codex", "api"]), default=None,
                     help="Coding agent: Claude Code, Codex, or droid-monkey's own agent on the Anthropic API. "
                          "Default: `agent` in monkey.yaml, else auto (the first one installed, in that order).")


def _agent(cfg, requested: str | None, model: str | None) -> tuple[str, str | None]:
    """The coding agent to use and the model to give it; says which one was picked."""
    from . import agents
    try:
        backend = agents.resolve(requested or cfg.agent, cfg.llm)
    except agents.AgentError as e:
        raise click.ClickException(str(e)) from None
    chosen = agents.model_for(backend, model, cfg.agent_model)
    theme.kv("agent", style(agents.NAMES[backend], fg="cyan", bold=True)
             + (style(f"  {chosen}", fg="bright_black") if chosen else ""))
    return backend, chosen


@main.command()
@APP_DIR
@click.option("--static-only", is_flag=True, help="Only the free static scan; skip the coding agent's read.")
@click.option("--model", default="claude-sonnet-5-5", show_default=True, help="Model for the coding agent's analysis.")
@AGENT
def analyze(app_dir: Path, static_only: bool, model: str, agent: str | None) -> None:
    """Analyse the app's repo: manifest, dependencies, risk patterns, then a Claude Code read -> ANALYSIS.md."""
    theme.banner("analyze")
    cfg = config_mod.load(app_dir)
    prof = profile_mod.analyze(cfg.app_dir, cfg.package, cfg.module, cfg.variant)
    theme.header(prof.package)
    theme.kv("targetSdk", str(prof.target_sdk))
    theme.kv("ui", prof.ui_toolkit)
    theme.kv("sources", f"{prof.source_files} files")
    theme.kv("can do", ", ".join(prof.capabilities) or "none")
    theme.kv("asks for", ", ".join(x.rsplit('.', 1)[-1] for x in prof.dangerous_permissions) or "none")
    theme.header(f"{len(prof.signals)} risk signals: ripe bananas", "(^.^)")
    by_kind: dict[str, list] = {}
    for s in prof.signals:
        by_kind.setdefault(s.kind, []).append(s)
    for kind, found in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):  # a real app has hundreds: group them
        click.echo(f"  {style(f'{kind:36}', fg='yellow')} {len(found):>4}  "
                   + style(", ".join(f"{Path(s.file).name}:{s.line}" for s in found[:3])
                           + (f" +{len(found) - 3} more" if len(found) > 3 else ""), fg="bright_black"))
    if len(prof.signals) > 3:
        theme.note(f"every location: {paths.profile_path(cfg.app_dir)}")
    if not prof.signals:
        theme.note("none found by the static scan")
    theme.header("fault types that apply", "(>.<)")
    for attack, why in prof.applicable_attacks.items():
        click.echo(f"  {style(f'{attack:18}', bold=True)} {why}")
    if static_only:
        out = paths.profile_path(cfg.app_dir)
        prof.save(out)
        click.echo("")
        theme.path_line("profile", out)
        return
    theme.header("reading the code", "(o_O)")
    backend, model = _agent(cfg, agent, model)
    with theme.working("the monkey is reading every branch of your source..."):
        res = analysis_mod.run(cfg.app_dir, prof, model=model, agent=backend, llm=cfg.llm)
    theme.header(f"{len(res.flows)} flows worth testing")
    for f in res.flows:
        faults = ", ".join(sorted({r.get('fault', '?') for r in f.get('risks', [])}))
        name = f.get("name", "?")
        click.echo(f"  {style(f'{name:24}', bold=True)} "
                   f"{f.get('description', '')[:70]}  " + style(f"[{faults}]", fg="magenta"))
    click.echo("")
    theme.kv("took", f"{res.seconds:.0f}s, ${res.cost_usd:.2f}")
    theme.path_line("report", res.markdown_path)
    theme.path_line("flows", res.flows_path)


@main.command()
@APP_DIR
@click.option("--mode", type=click.Choice(["agent", "claude", "jev", "rules"]), default="agent", show_default=True,
              help="agent: the coding agent reads the code and drives the app (screens + files). jev: fast, "
                   "TypeSafe's Jev ranks each screen's elements toward the analysed flows (needs TYPESAFE_API_KEY). "
                   "rules: free tap-everything exploration without a model. (claude is the old name for agent.)")
@click.option("--model", default="claude-sonnet-5-5", show_default=True, help="Model for agent mode.")
@AGENT
def explore(app_dir: Path, mode: str, model: str, agent: str | None) -> None:
    """Explore the installed app and save its screen graph (no faults injected)."""
    theme.banner("explore")
    ctx = _context(app_dir, time.strftime("explore-%Y%m%d-%H%M%S"))
    if mode == "rules":
        smap = _explore(ctx)
    elif mode == "jev":
        from .designer import load_flows
        from .jev_explore import JevExplorer
        from .tracing import load_keys
        load_keys()
        explorer = JevExplorer(ctx, load_flows(ctx.cfg.app_dir), save_path=paths.partial_map_path(ctx.cfg.app_dir))
        with theme.working("swinging through your screens (Jev picks each next tap)..."):
            smap = explorer.explore()
        smap.save(_map_path(ctx))
        for note in explorer.notes:
            theme.note(note)
    else:
        from . import claude_explore
        from .designer import load_flows
        prof = profile_mod.analyze(ctx.cfg.app_dir, ctx.cfg.package, ctx.cfg.module, ctx.cfg.variant)
        backend, model = _agent(ctx.cfg, agent, model)
        partial = paths.partial_map_path(ctx.cfg.app_dir)
        with theme.working("swinging through your screens (reading the code and driving the app)..."):
            res = claude_explore.run(ctx.cfg.app_dir, prof, partial, load_flows(ctx.cfg.app_dir), model=model,
                                     agent=backend, llm=ctx.cfg.llm)
        smap = res.smap
        theme.ok(f"explored in {res.seconds:.0f}s, ${res.cost_usd:.2f}")
        click.echo(f"\n{res.summary[:1500]}")
        theme.header("replaying every recorded path")
        with theme.working("retracing the monkey's steps..."):
            checks = claude_explore.verify_paths(ctx, smap)
        for sid, verdict in checks.items():
            if verdict == "ok":
                click.echo(f"  {style('(^.^)', fg='green')} {sid:24} " + style("replays", fg="green"))
            else:
                click.echo(f"  {style('(x_x)', fg='red')} {sid:24} " + style(verdict, fg="red"))
        bad = [sid for sid, v in checks.items() if v != "ok"]
        if bad:
            smap.unreached += [{"screen": sid, "reason": f"recorded path does not replay ({checks[sid]})"} for sid in bad]
        smap.save(_map_path(ctx))  # replaces the previous map only now that the run finished and was checked
        partial.unlink(missing_ok=True)
    _print_map(smap)
    theme.header("where the monkey will strike", "(>.<)")
    for c in scenarios_mod.generate(smap):
        click.echo(f"  {style(f'{c.attack:18}', fg='yellow', bold=True)} {style(f'[{c.screen}]', fg='cyan')} "
                   f"{c.action or '-':28} {style(c.reason, fg='bright_black')}")
    click.echo("")
    theme.path_line("map", _map_path(ctx))


@main.command()
@click.argument("name")
@APP_DIR
@click.option("--target", help="Screen id from the screen map (built-in attacks; default: the best candidate).")
@click.option("--action", help="Trigger step, e.g. 'tap submit_button' (built-in attacks; default: the best candidate's).")
@click.option("--re-explore", is_flag=True, help="Explore again even if a screen map exists.")
@click.option("--build/--no-build", default=None, help="Build and install the app first. Default: only when the "
                                                       "installed app was not built from the current source.")
@click.option("--run-id", default=None, help="Evidence folder name under .droid-monkey/runs/.")
@click.option("--screens", type=int, default=None, help="Built-in attacks run on this many of their best screens "
                                                      "(default: screens_per_attack in monkey.yaml, else 2).")
@click.option("--diagnose", is_flag=True, help="For each failure, find the root cause and print the suggested fix "
                                               "with its evidence (no code is changed).")
def attack(name: str, app_dir: Path, target: str | None, action: str | None, re_explore: bool, build: bool,
           run_id: str | None, screens: int | None, diagnose: bool) -> None:
    """Run an attack: a built-in one, a saved spec by name, `builtin`, `custom` or `all`. No fixing."""
    theme.banner(f"attack {name}")
    best = None
    run_id = run_id or time.strftime("attack-%Y%m%d-%H%M%S")
    ctx = _context(app_dir, run_id)
    from . import provenance
    mismatch = None if build else provenance.check(ctx.cfg.app_dir, ctx.adb, ctx.cfg.package)
    if build is None and mismatch:
        theme.note(f"building first: {mismatch}")
        build, mismatch = True, None
    elif mismatch:
        theme.warn(f"{mismatch}; results describe the installed build, not this source")
        if diagnose:
            theme.warn("--diagnose skipped: it reads this source, which is not what is installed")
            diagnose = False
    if build:
        with theme.working("gradle is growing the banana tree..."):
            res = gradle.build(ctx.cfg.app_dir, ctx.cfg.module, ctx.cfg.variant, java_home=ctx.cfg.java_home)
        if not res.ok or not res.apk:
            raise click.ClickException(f"build failed:\n{res.log_tail}")
        gradle.install(ctx.adb, res.apk, package=ctx.cfg.package, source_dir=ctx.cfg.app_dir)
        theme.ok(f"built and installed {res.apk.name} in {res.seconds:.0f}s")
    smap, prof, saved, known = _load_app(ctx, re_explore)
    candidates = scenarios_mod.generate(smap)

    if name == "all":
        names = sorted(BUILTIN) + sorted(saved)
    elif name == "builtin":
        names = sorted(BUILTIN)
    elif name == "custom":
        names = sorted(saved)
    elif name in BUILTIN or name in saved:
        names = [name]
    else:
        raise click.ClickException(f"unknown attack {name!r}. Built-in: {sorted(BUILTIN)}; saved specs: {sorted(saved) or 'none'}")

    # Built-in attacks run on their best N screens, unless --target/--action pins one.
    per_screen: dict[str, list[dict]] = {}
    if target is None and action is None:
        for a in scenarios_mod.builtin_attacks(smap, known, screens or ctx.cfg.screens_per_attack):
            per_screen.setdefault(a["base"], []).append(a)

    results = []
    for attack_name in names:
        if attack_name in saved:
            spec = saved[attack_name]
            theme.header(attack_name, "(>.<)")
            click.echo(style(f"  {spec.description}", fg="bright_black"))
        elif attack_name in per_screen:
            for a in per_screen[attack_name]:
                spec, best = a["spec"], None
                theme.header(a["name"], "(>.<)")
                click.echo(style(f"  {a['reason']}", fg="bright_black"))
                with theme.working(f"monkeying with the app: {a['name']}..."):
                    result = SpecRunner(ctx, spec).run()
                results.append(result)
                _echo_result(a["name"], result)
                if diagnose and result.outcome not in ("survived", "skipped"):
                    _suggest_fix(ctx, smap, spec, result, a["reason"])
            continue
        elif target is None and action is None:
            theme.header(attack_name, "(>.<)")
            click.echo(f"  {theme.badge('skipped')} the explorer found nowhere to apply it")
            continue
        else:
            best = scenarios_mod.best(candidates, attack_name)
            if target:
                best = scenarios_mod.Candidate(attack_name, target, (action or "").removeprefix("tap ") or None,
                                               best.work_seconds if best else 2.0, 0, "chosen with --target",
                                               best.permission if best else None)
            elif best and action is not None:
                best.action = action.removeprefix("tap ") or None
            theme.header(attack_name, "(>.<)")
            if best is None:
                click.echo(f"  {theme.badge('skipped')} the explorer found nowhere to apply it")
                continue
            try:
                spec = specs_mod.render(BUILTIN[attack_name], best.values(), known)
            except specs_mod.SpecError as e:
                click.echo(f"  {theme.badge('skipped')} {e}")
                continue
            click.echo(style(f"  {best.reason}", fg="bright_black"))
        with theme.working(f"monkeying with the app: {attack_name}..."):
            result = SpecRunner(ctx, spec).run()
        results.append(result)
        _echo_result(attack_name, result)
        if diagnose and result.outcome not in ("survived", "skipped"):
            _suggest_fix(ctx, smap, spec, result, best.reason if attack_name in BUILTIN and best else spec.description)
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "results.json").write_text(json.dumps([r.__dict__ for r in results], indent=2))
    _attack_summary(results)
    theme.path_line("evidence", ctx.run_dir)
    if results:
        theme.path_line("dashboard", _attack_dashboard(ctx, run_id, results))


def _attack_dashboard(ctx, run_id: str, results) -> Path:
    """The same visual report as a run's, for attacks run on their own (no fixing): what broke, with recordings,
    charts and, with --diagnose, the suggested fix."""
    from . import report as report_mod
    outcomes = []
    for r in results:
        fix = Path(r.evidence_dir) / "suggested-fix.json"
        d = json.loads(fix.read_text()) if fix.exists() else None
        outcomes.append({"attack": r.attack, "status": "survived" if r.outcome == "survived" else "found",
                         "before": r.outcome, "before_detail": r.crash_signature or r.reason, "metrics": r.metrics,
                         "diagnosis": {k: d[k] for k in ("summary", "root_cause", "fix_approach", "files_to_change",
                                                         "test_plan") if k in d} | {"evidence": d.get("code_evidence", [])}
                         if d else None})
    summary = {"run_id": run_id, "branch": "(attack only, no fixes)", "outcomes": outcomes, "mode": "attack",
               "finished": time.strftime("%Y-%m-%d %H:%M")}
    out = ctx.run_dir / "dashboard.html"
    report_mod.dashboard(summary, ctx.cfg.package, ctx.run_dir, out, ctx.cfg.app_dir)
    return out


def _attack_summary(results) -> None:
    if not results:
        return
    theme.header("scoreboard", "(^o^)")
    for r in results:
        click.echo(f"  {theme.badge(r.outcome)} {r.attack}")
    broke = sum(1 for r in results if r.outcome not in ("survived", "skipped"))
    if broke:
        theme.monkey_says("crashed", f"THE MONKEY BROKE {broke} OF {len(results)}",
                          ["the app has bugs worth fixing", "see the suggested fixes with --diagnose"])
    else:
        theme.monkey_says("survived", f"SURVIVED ALL {len(results)}", ["the monkey went home hungry"])


def _suggest_fix(ctx, smap, spec, result, reason: str) -> None:
    from dataclasses import asdict
    from . import approval
    from .diagnoser import diagnose as run_diagnoser
    screen = smap.screens.get(spec.screen)
    with theme.working("sniffing out the root cause..."):
        d = run_diagnoser(ctx.cfg.app_dir, ctx.cfg.module, ctx.cfg.llm, asdict(result), spec.__dict__,
                          asdict(screen) if screen else None)
    p = approval.proposal({"name": spec.name, "reason": reason}, asdict(result), d.to_dict())
    click.echo(approval.render(p, footer=False))
    (Path(result.evidence_dir) / "suggested-fix.json").write_text(json.dumps(p, indent=2))
    click.echo("  ->  to apply it: " + style(f"droid-monkey run --attacks {spec.name}", fg="cyan"))


@main.group()
def attacks() -> None:
    """Attack types: list, show, and create them with the designer agent."""


@attacks.command("list")
@APP_DIR
def attacks_list(app_dir: Path) -> None:
    """Built-in attacks and the app's saved specs."""
    ctx = _context(app_dir, "attacks-list")
    _, _, saved, _ = _load_app(ctx)
    theme.header("built-in tricks")
    theme.note(str(specs_mod.BUILTIN_DIR))
    for n, t in BUILTIN.items():
        click.echo(f"  {style(f'{n:34}', fg='yellow', bold=True)} {t['description']}")
    theme.header("this app's own tricks", "(^.^)")
    theme.note(str(specs_mod.spec_dir(ctx.cfg.app_dir)))
    for n, sp in saved.items():
        click.echo(f"  {style(f'{n:34}', fg='yellow', bold=True)} "
                   + style(f"[{sp.fault_type}, {sp.source}]", fg="magenta") + f" {sp.description}")
    if not saved:
        click.echo("  none yet: try " + style("droid-monkey attacks suggest", fg="cyan") + " or "
                   + style('droid-monkey attacks new "..."', fg="cyan"))


@attacks.command("show")
@click.argument("name")
@APP_DIR
def attacks_show(name: str, app_dir: Path) -> None:
    """Print a saved spec."""
    path = specs_mod.spec_dir(Path(app_dir).resolve()) / f"{name}.yaml"
    if not path.exists():
        raise click.ClickException(f"no saved spec {name}")
    click.echo(path.read_text())


def _design(app_dir: Path, goal: str, source: str, try_on_device: bool, max_tries: int) -> None:
    from .designer import Designer, load_flows
    ctx = _context(app_dir, time.strftime(f"design-%Y%m%d-%H%M%S"))
    theme.banner("attack designer")
    smap, prof, saved, _ = _load_app(ctx)
    flows = load_flows(ctx.cfg.app_dir)
    if not flows:
        theme.warn("no analysis yet: run `droid-monkey analyze` for better attacks")
    designer = Designer(ctx, smap, prof, flows, saved, allow_try=try_on_device, max_tries=max_tries)
    theme.header("designing new mischief", "(>.<)")
    theme.kv("designer", f"{ctx.cfg.llm.provider}/{ctx.cfg.llm.model}")
    with theme.working("the monkey is scheming..."):
        res = designer.run(goal, source)
    if res.tries:
        theme.header("tried on the emulator")
    for t in res.tries:
        click.echo(f"  {theme.badge(t.get('outcome') or '?')} {style(f"{t['spec']:30}", bold=True)} "
                   + style((t.get('exception') or t.get('reason') or '')[:90], fg="bright_black"))
    for p in res.saved:
        theme.ok(f"saved {p}")
    click.echo(f"\n{res.summary}\n")
    theme.kv("cost", f"{res.turns} turns, {res.input_tokens:,} in / {res.output_tokens:,} out tokens, ~${res.cost_usd:.3f}")


@attacks.command("suggest")
@APP_DIR
@click.option("--count", default=3, show_default=True, help="How many new attack types to design.")
@click.option("--try/--no-try", "try_on_device", default=True, help="Try each spec on the emulator before saving.")
def attacks_suggest(app_dir: Path, count: int, try_on_device: bool) -> None:
    """The designer agent writes new attack types for this app from its analysis and screen map."""
    from .designer import suggest_goal
    _design(app_dir, suggest_goal(count), "suggested", try_on_device, max_tries=count * 2 + 1)


@attacks.command("from-flows")
@APP_DIR
@click.option("--max", "limit", default=6, show_default=True, help="Design at most this many flow attacks.")
@click.option("--try/--no-try", "try_on_device", default=True, help="Try each spec on the emulator before saving.")
def attacks_from_flows(app_dir: Path, limit: int, try_on_device: bool) -> None:
    """Turn the user flows and risks from `analyze` into attacks: one validated, tried spec per risk, walking that
    flow and injecting that fault at the moment the analysis named."""
    from .designer import Designer, flow_goal, flow_risks, load_flows
    ctx = _context(app_dir, time.strftime("flows-%Y%m%d-%H%M%S"))
    theme.banner("attacks from user flows")
    smap, prof, saved, known = _load_app(ctx)
    flows = load_flows(ctx.cfg.app_dir)
    if not flows:
        raise click.ClickException("no flows yet: run `droid-monkey analyze` first")
    todo = flow_risks(flows, saved)
    theme.kv("flows", f"{len(flows)} flows, {sum(len(f.get('risks', [])) for f in flows)} risks, "
                      f"{len(todo)} not covered yet" + (f"; designing {limit}" if len(todo) > limit else ""))
    total_in = total_out = 0
    rows = []
    for flow, risk in todo[:limit]:
        theme.header(f"{flow.get('name')}: {risk.get('fault')}", "(>.<)")
        click.echo(style(f"  {risk.get('why', '')[:150]}", fg="bright_black"))
        designer = Designer(ctx, smap, prof, flows, saved, allow_try=try_on_device, max_tries=3)
        with theme.working("the monkey is scheming..."):
            res = designer.run(flow_goal(flow, risk), "flow")
        total_in, total_out = total_in + res.input_tokens, total_out + res.output_tokens
        for t in res.tries:
            click.echo(f"  {theme.badge(t.get('outcome') or '?')} {style(f'{t['spec']:30}', bold=True)} "
                       + style((t.get('exception') or t.get('reason') or '')[:90], fg="bright_black"))
        if res.saved:
            for p in res.saved:
                theme.ok(f"saved {p.name}")
            for p in res.saved:  # so the next flow's designer knows it exists
                try:
                    saved[p.stem] = specs_mod.parse(p.read_text(), known)
                except specs_mod.SpecError:
                    pass
        else:
            theme.warn(f"nothing saved: {res.summary[:200]}")
        rows.append((flow.get("name"), risk.get("fault"), [p.stem for p in res.saved]))
    theme.header("flow coverage")
    for name, fault, specs_saved in rows:
        click.echo(f"  {name:30} {fault:18} {', '.join(specs_saved) or style('not reproducible', fg='yellow')}")
    from .pricing import cost_usd
    theme.kv("cost", f"{total_in:,} in / {total_out:,} out tokens, ~${cost_usd(ctx.cfg.llm.model, total_in, total_out) or 0:.3f}")
    click.echo("these run in every `droid-monkey run` from now on")


@attacks.command("new")
@click.argument("prompt")
@APP_DIR
@click.option("--try/--no-try", "try_on_device", default=True, help="Try the spec on the emulator before saving.")
def attacks_new(prompt: str, app_dir: Path, try_on_device: bool) -> None:
    """Describe an attack in plain words; the designer agent turns it into a validated, tested spec."""
    from .designer import new_goal
    _design(app_dir, new_goal(prompt), "prompt", try_on_device, max_tries=3)


@main.command()
@APP_DIR
@click.option("--attacks", "only", default=None, help="Comma-separated attack names to run (default: all built-in and saved).")
@click.option("--coder-model", default="claude-sonnet-5-5", show_default=True, help="Model for the coding agent that fixes.")
@AGENT
@click.option("--run-id", default=None, help="Run id (default: a timestamp). The run branch is droid-monkey/<run-id>.")
@click.option("--resume", "resume_id", default=None, help="Resume a killed run by its run id.")
@click.option("--fail-first-fix", is_flag=True, help="Report the first passing verification as failed (labelled), to "
                                                      "demonstrate the retry path.")
@click.option("--naive", is_flag=True, help="Baseline for comparisons: skip diagnosis, give the coder only the failure.")
@click.option("--no-compile-tool", is_flag=True, help="Do not give the coder the compile tool (for comparisons).")
@click.option("--auto-fix", is_flag=True, help="Fix without asking (CI, benchmarks). By default each fix is shown "
                                                 "with its evidence and suggested change, and waits for your yes.")
@click.option("--screens", type=int, default=None, help="Built-in attacks run on this many of their best screens "
                                                      "(default: screens_per_attack in monkey.yaml, else 2).")
def run(app_dir: Path, only: str | None, coder_model: str, run_id: str | None, resume_id: str | None,
        fail_first_fix: bool, naive: bool, no_compile_tool: bool, auto_fix: bool, agent: str | None,
        screens: int | None) -> None:
    """The full loop: plan, attack, diagnose, ask you, fix with the coding agent, verify by replay, commit."""
    from . import approval
    theme.banner("run")
    if auto_fix:
        theme.warn("--auto-fix: droid-monkey will change code on its run branch without asking you first")

    def ask(proposal: dict) -> str:
        if sys.stdin.isatty():
            return approval.ask_terminal(proposal)
        click.echo(approval.render(proposal))
        theme.warn("not a terminal, so not fixing without approval: rerun with --auto-fix to fix without asking")
        return "no"

    from . import loop
    run_id = resume_id or run_id or time.strftime("run-%Y%m%d-%H%M%S")
    ctx = _context(app_dir, run_id)
    if not _map_path(ctx).exists():
        raise click.ClickException("no screen map yet: run `droid-monkey explore` first")
    smap, prof, saved, known = _load_app(ctx)
    theme.header(f"run {run_id}")
    theme.kv("app", ctx.cfg.package)
    backend, coder_model = _agent(ctx.cfg, agent, coder_model)
    names = [n.strip() for n in only.split(",")] if only else None
    final = loop.run(ctx, smap, prof, saved, known, run_id, only=names, coder_model=coder_model, agent=backend,
                     resume=bool(resume_id), echo=theme.loop_echo, fail_first_fix=fail_first_fix, naive=naive,
                     compile_tool=not no_compile_tool, auto_fix=auto_fix, ask=ask, screens_per_attack=screens or ctx.cfg.screens_per_attack)
    from .report import score_of
    outcomes = final.get("outcomes", [])
    theme.header("scoreboard", "(^o^)")
    for o in outcomes:
        extra = (style(f"  {o['commit'][:8]}", fg="yellow")
                 + (style(f" checkpoint {o['checkpoint']}", fg="bright_black") if o.get("checkpoint") else "")
                 if o.get("commit") else "")
        click.echo(f"  {theme.badge(o['status'])} {style(f"{o['attack']:28}", bold=True)} "
                   + style(f"was {o['before']}", fg="bright_black") + extra)
    if outcomes:
        score = score_of(outcomes)
        fixed = sum(1 for o in outcomes if o["status"] == "fixed")
        theme.score_monkey(score, f"RESILIENCE  {theme.banana_meter(score)}",
                           [theme.verdict_line(score), f"{fixed} fixed, {len(outcomes)} attacks"])
    click.echo("")
    theme.kv("branch", style(str(final.get("branch")), fg="cyan"))
    theme.path_line("evidence", ctx.run_dir)


@main.command()
@click.argument("run_id")
@APP_DIR
def report(run_id: str, app_dir: Path) -> None:
    """Write the PR body, score card and dashboard for a finished run."""
    from . import report as report_mod
    cfg = config_mod.load(app_dir)
    r = report_mod.build(cfg.app_dir, cfg.package, run_id)
    theme.header(f"report {run_id}", "(^o^)")
    theme.score_monkey(r.score, f"RESILIENCE  {theme.banana_meter(r.score)}",
                       [theme.verdict_line(r.score), f"{r.fixed} of {r.total} attacks fixed"])
    click.echo("")
    theme.path_line("PR body", r.pr_body)
    theme.path_line("score card", r.score_card)
    theme.path_line("dashboard", r.dashboard)


@main.command()
@click.argument("run_id")
@APP_DIR
@click.option("--base", default="main", show_default=True, help="Branch to open the PR against.")
@click.option("--draft", is_flag=True, help="Open the PR as a draft.")
def pr(run_id: str, app_dir: Path, base: str, draft: bool) -> None:
    """Push the run's branch (with Entire checkpoints) and open a GitHub PR with the evidence table."""
    import subprocess
    from . import report as report_mod
    cfg = config_mod.load(app_dir)
    summary = report_mod.load_summary(cfg.app_dir, run_id)
    r = report_mod.build(cfg.app_dir, cfg.package, run_id)
    branch = summary["branch"]
    fixed = r.fixed
    title = f"droid-monkey: fix {fixed} lifecycle bug{'s' if fixed != 1 else ''} (resilience {r.score}/100)"
    theme.header("opening a PR", "(^o^)")
    theme.note(f"pushing {branch} ...")
    subprocess.run(["git", "push", "-u", "origin", branch], cwd=cfg.app_dir, check=True)
    cmd = ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", str(r.pr_body)]
    if draft:
        cmd.append("--draft")
    out = subprocess.run(cmd, cwd=cfg.app_dir, capture_output=True, text=True)
    if out.returncode != 0:
        raise click.ClickException(out.stderr.strip())
    theme.ok(f"bananas delivered: {out.stdout.strip()}")


@main.command()
@click.argument("run_a")
@click.argument("run_b")
@APP_DIR
def compare(run_a: str, run_b: str, app_dir: Path) -> None:
    """Before/after table of two runs, e.g. a --naive baseline vs a normal run."""
    from . import report as report_mod
    cfg = config_mod.load(app_dir)
    md, out = report_mod.compare(cfg.app_dir, run_a, run_b)
    theme.header(f"{run_a}  vs  {run_b}")
    click.echo(md)
    theme.path_line("saved", out)
