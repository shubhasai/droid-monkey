"""droid-monkey command line."""

import json
import time
from pathlib import Path

import click

from . import analysis as analysis_mod
from . import config as config_mod
from . import profile as profile_mod
from . import scenarios as scenarios_mod
from . import paths
from . import specs as specs_mod
from .config import Target
from .driver import Context
from .explorer import Explorer, ScreenMap
from .runner import SpecRunner
from .tools import emulator, gradle
from .tools.adb import Adb
from .tools.device import Device

BUILTIN = specs_mod.builtin_templates()

OUTCOME_COLOR = {"survived": "green", "crashed": "red", "lost_state": "yellow", "no_feedback": "magenta", "skipped": "white"}
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
        click.echo(click.style(f"  note: {note}", dim=True))
    return smap


def _print_map(smap: ScreenMap) -> None:
    click.echo(f"{len(smap.screens)} screens, {len(smap.edges)} edges, explored in {smap.seconds:.0f}s "
               f"({smap.mode} mode; screen-read overhead {smap.baseline_seconds:.1f}s)")
    for s in smap.screens.values():
        if s.files:
            click.echo(click.style(f"  [{s.id}] {s.title}", bold=True) + (f"  {s.description}" if s.description else ""))
            click.echo(f"      files: {', '.join(s.files)}")
            if s.fields:
                click.echo(f"      fields: {', '.join(s.fields)}")
            for a in s.actions:
                bits = [a.effect] + ([f"-> {a.to_screen}"] if a.to_screen else []) \
                    + ([f"slow {a.settle_seconds:.1f}s"] if (a.showed_progress or a.settle_seconds >= 1.5) else []) \
                    + (["asks " + ", ".join(p.rsplit('.', 1)[-1] for p in a.permissions)] if a.permissions else []) \
                    + ([f"CRASH {a.crash}"] if a.crash else [])
                click.echo(f"      {a.label[:40]:40} {' '.join(bits)}")
            continue
        form = f", form with {len(s.fields)} fields" if s.has_form else ""
        click.echo(click.style(f"  [{s.id}] {s.title or '(untitled)'}", bold=True) + f"  depth {len([p for p in s.path if p.startswith('tap')])}{form}")
        for a in s.actions:
            extra = []
            if a.to_screen:
                extra.append(f"-> {a.to_screen}")
            if a.showed_progress or a.settle_seconds >= 1.5:
                extra.append(f"slow {a.settle_seconds:.1f}s")
            if a.permissions:
                extra.append("asks " + ", ".join(p.rsplit('.', 1)[-1] for p in a.permissions))
            if a.crash:
                extra.append(f"CRASH {a.crash}")
            click.echo(f"      {a.label[:40]:40} {a.effect:12} {' '.join(extra)}")
    for c in smap.crashes:
        click.echo(click.style(f"  crash during exploration: {c}", fg="red"))
    for u in smap.unreached:
        click.echo(click.style(f"  not reached: {u.get('screen')}: {u.get('reason')}", fg="yellow"))


def _load_app(ctx: Context, re_explore: bool = False):
    """Screen map (explored if missing), targets, static profile, flows and saved specs for this app."""
    if re_explore or not _map_path(ctx).exists():
        click.echo("exploring the app first...")
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
        click.echo(click.style(f"  invalid spec {fname}: {'; '.join(errors)}", fg="red"))
    return smap, prof, saved, known


def _echo_result(name: str, result) -> None:
    detail = result.exception or result.reason
    click.echo(f"{name:28} " + click.style(f"{result.outcome:11}", fg=OUTCOME_COLOR.get(result.outcome))
               + f" {detail}  ({result.seconds:.0f}s)")
    if result.crash_signature:
        click.echo(f"{'':28} at {result.crash_signature}")
    for key, (old, new) in result.lost_fields.items():
        click.echo(f"{'':28} {key}: {old!r} -> {new!r}")


@click.group()
def main() -> None:
    """Attack an Android app with real-world faults, fix what breaks, prove each fix."""


@main.command()
@APP_DIR
@click.option("--force", is_flag=True, help="Overwrite an existing monkey.yaml.")
def init(app_dir: Path, force: bool) -> None:
    """Write monkey.yaml for this Android repo and check everything droid-monkey needs."""
    from . import init_cmd
    app_dir = Path(app_dir).resolve()
    cfg_path = app_dir / config_mod.CONFIG_NAME
    if cfg_path.exists() and not force:
        click.echo(f"{cfg_path.name} already exists (use --force to rewrite it)")
    else:
        d = init_cmd.detect(app_dir)
        if not d.package or not d.module:
            raise click.ClickException("no Android application module with an applicationId found here; "
                                       "run init from the app repo's root")
        init_cmd.write_config(app_dir, d)
        click.echo(click.style(f"wrote {cfg_path}", fg="green") + f": package {d.package}, module {d.module}, avd {d.avd}")
    click.echo("checks:")
    for name, ok, hint in init_cmd.checks(app_dir):
        click.echo(f"  {click.style('ok  ', fg='green') if ok else click.style('MISSING', fg='red')} {name}"
                   + ("" if ok else f"  ({hint})"))


@main.command()
@APP_DIR
@click.option("--static-only", is_flag=True, help="Only the free static scan; skip the Claude Code read.")
@click.option("--model", default="claude-sonnet-5-5", show_default=True, help="Model for the Claude Code analysis.")
def analyze(app_dir: Path, static_only: bool, model: str) -> None:
    """Analyse the app's repo: manifest, dependencies, risk patterns, then a Claude Code read -> ANALYSIS.md."""
    cfg = config_mod.load(app_dir)
    prof = profile_mod.analyze(cfg.app_dir, cfg.package, cfg.module, cfg.variant)
    click.echo(click.style(f"{prof.package}", bold=True) + f"  targetSdk {prof.target_sdk}, {prof.ui_toolkit}, "
               f"{prof.source_files} source files, capabilities: {', '.join(prof.capabilities) or 'none'}")
    click.echo(f"runtime permissions: {', '.join(x.rsplit('.', 1)[-1] for x in prof.dangerous_permissions) or 'none'}")
    click.echo(f"{len(prof.signals)} risk signals:")
    for s in prof.signals:
        click.echo(f"  {s.kind:36} {s.file}:{s.line}")
    click.echo("fault types that apply:")
    for attack, why in prof.applicable_attacks.items():
        click.echo(f"  {attack:18} {why}")
    if static_only:
        out = paths.profile_path(cfg.app_dir)
        prof.save(out)
        click.echo(f"profile: {out}")
        return
    click.echo(f"\nClaude Code ({model}) is reading the source...")
    res = analysis_mod.run(cfg.app_dir, prof, model=model)
    click.echo(f"{len(res.flows)} flows worth testing ({res.seconds:.0f}s, ${res.cost_usd:.2f} of plan usage):")
    for f in res.flows:
        faults = ", ".join(sorted({r.get('fault', '?') for r in f.get('risks', [])}))
        click.echo(f"  {f.get('name', '?'):24} {f.get('description', '')[:70]}  [{faults}]")
    click.echo(f"report: {res.markdown_path}\nflows:  {res.flows_path}")


@main.command()
@APP_DIR
@click.option("--mode", type=click.Choice(["claude", "rules"]), default="claude", show_default=True,
              help="claude: Claude Code reads the code and drives the app (screens + files, uses your Claude plan). "
                   "rules: free tap-everything exploration without a model.")
@click.option("--model", default="claude-sonnet-5-5", show_default=True, help="Model for Claude mode.")
def explore(app_dir: Path, mode: str, model: str) -> None:
    """Explore the installed app and save its screen graph (no faults injected)."""
    ctx = _context(app_dir, time.strftime("explore-%Y%m%d-%H%M%S"))
    if mode == "rules":
        smap = _explore(ctx)
    else:
        from . import claude_explore
        from .designer import load_flows
        prof = profile_mod.analyze(ctx.cfg.app_dir, ctx.cfg.package, ctx.cfg.module, ctx.cfg.variant)
        click.echo(f"Claude Code ({model}) is reading the code and driving the app; this takes a while...")
        partial = paths.partial_map_path(ctx.cfg.app_dir)
        res = claude_explore.run(ctx.cfg.app_dir, prof, partial, load_flows(ctx.cfg.app_dir), model=model)
        smap = res.smap
        click.echo(f"done in {res.seconds:.0f}s, ${res.cost_usd:.2f} of plan usage\n{res.summary[:1500]}\n")
        click.echo("replaying every recorded path to check it...")
        checks = claude_explore.verify_paths(ctx, smap)
        for sid, verdict in checks.items():
            click.echo(f"  {sid:24} " + click.style(verdict, fg="green" if verdict == "ok" else "red"))
        bad = [sid for sid, v in checks.items() if v != "ok"]
        if bad:
            smap.unreached += [{"screen": sid, "reason": f"recorded path does not replay ({checks[sid]})"} for sid in bad]
        smap.save(_map_path(ctx))  # replaces the previous map only now that the run finished and was checked
        partial.unlink(missing_ok=True)
    _print_map(smap)
    click.echo("\nattack candidates:")
    for c in scenarios_mod.generate(smap):
        click.echo(f"  {c.attack:18} [{c.screen}] {c.action or '-':28} {c.reason}")
    click.echo(f"map: {_map_path(ctx)}")


@main.command()
@click.argument("name")
@APP_DIR
@click.option("--target", help="Screen id from the screen map (built-in attacks; default: the best candidate).")
@click.option("--action", help="Trigger step, e.g. 'tap submit_button' (built-in attacks; default: the best candidate's).")
@click.option("--re-explore", is_flag=True, help="Explore again even if a screen map exists.")
@click.option("--build/--no-build", default=False, help="Build and install the app first.")
@click.option("--run-id", default=None, help="Evidence folder name under .droid-monkey/runs/.")
def attack(name: str, app_dir: Path, target: str | None, action: str | None, re_explore: bool, build: bool,
           run_id: str | None) -> None:
    """Run an attack: a built-in one, a saved spec by name, `builtin`, `custom` or `all`. No fixing."""
    run_id = run_id or time.strftime("attack-%Y%m%d-%H%M%S")
    ctx = _context(app_dir, run_id)
    if build:
        res = gradle.build(ctx.cfg.app_dir, ctx.cfg.module, ctx.cfg.variant, java_home=ctx.cfg.java_home)
        if not res.ok or not res.apk:
            raise click.ClickException(f"build failed:\n{res.log_tail}")
        gradle.install(ctx.adb, res.apk)
        click.echo(f"built and installed {res.apk.name} in {res.seconds:.0f}s")
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

    results = []
    for attack_name in names:
        if attack_name in saved:
            spec = saved[attack_name]
            click.echo(click.style(f"{attack_name}: ", bold=True) + spec.description)
        else:
            best = scenarios_mod.best(candidates, attack_name)
            if target:
                best = scenarios_mod.Candidate(attack_name, target, (action or "").removeprefix("tap ") or None,
                                               best.work_seconds if best else 2.0, 0, "chosen with --target",
                                               best.permission if best else None)
            elif best and action is not None:
                best.action = action.removeprefix("tap ") or None
            if best is None:
                click.echo(f"{attack_name:28} skipped: the explorer found nowhere to apply it")
                continue
            try:
                spec = specs_mod.render(BUILTIN[attack_name], best.values(), known)
            except specs_mod.SpecError as e:
                click.echo(f"{attack_name:28} skipped: {e}")
                continue
            click.echo(click.style(f"{attack_name}: ", bold=True) + best.reason)
        result = SpecRunner(ctx, spec).run()
        results.append(result)
        _echo_result(attack_name, result)
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "results.json").write_text(json.dumps([r.__dict__ for r in results], indent=2))
    click.echo(f"evidence: {ctx.run_dir}")


@main.group()
def attacks() -> None:
    """Attack types: list, show, and create them with the designer agent."""


@attacks.command("list")
@APP_DIR
def attacks_list(app_dir: Path) -> None:
    """Built-in attacks and the app's saved specs."""
    ctx = _context(app_dir, "attacks-list")
    _, _, saved, _ = _load_app(ctx)
    click.echo(click.style(f"built-in ({specs_mod.BUILTIN_DIR})", bold=True))
    for n, t in BUILTIN.items():
        click.echo(f"  {n:34} {t['description']}")
    click.echo(click.style(f"saved specs ({specs_mod.spec_dir(ctx.cfg.app_dir)})", bold=True))
    for n, sp in saved.items():
        click.echo(f"  {n:34} [{sp.fault_type}, {sp.source}] {sp.description}")
    if not saved:
        click.echo("  none yet: try `droid-monkey attacks suggest` or `droid-monkey attacks new \"...\"`")


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
    smap, prof, saved, _ = _load_app(ctx)
    flows = load_flows(ctx.cfg.app_dir)
    if not flows:
        click.echo(click.style("no analysis yet: run `droid-monkey analyze` for better attacks", fg="yellow"))
    designer = Designer(ctx, smap, prof, flows, saved, allow_try=try_on_device, max_tries=max_tries)
    click.echo(f"attack designer ({ctx.cfg.llm.provider}/{ctx.cfg.llm.model}) is working...")
    res = designer.run(goal, source)
    for t in res.tries:
        click.echo(f"  tried {t['spec']:30} -> {t.get('outcome')}: {(t.get('exception') or t.get('reason') or '')[:90]}")
    for p in res.saved:
        click.echo(click.style(f"  saved {p}", fg="green"))
    click.echo(f"\n{res.summary}\n")
    click.echo(f"{res.turns} turns, {res.input_tokens:,} in / {res.output_tokens:,} out tokens, ~${res.cost_usd:.3f}")


@attacks.command("suggest")
@APP_DIR
@click.option("--count", default=3, show_default=True, help="How many new attack types to design.")
@click.option("--try/--no-try", "try_on_device", default=True, help="Try each spec on the emulator before saving.")
def attacks_suggest(app_dir: Path, count: int, try_on_device: bool) -> None:
    """The designer agent writes new attack types for this app from its analysis and screen map."""
    from .designer import suggest_goal
    _design(app_dir, suggest_goal(count), "suggested", try_on_device, max_tries=count * 2 + 1)


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
@click.option("--coder-model", default="claude-sonnet-5-5", show_default=True, help="Model for the Claude Code fixer.")
@click.option("--run-id", default=None, help="Run id (default: a timestamp). The run branch is droid-monkey/<run-id>.")
@click.option("--resume", "resume_id", default=None, help="Resume a killed run by its run id.")
@click.option("--fail-first-fix", is_flag=True, help="Report the first passing verification as failed (labelled), to "
                                                      "demonstrate the retry path.")
@click.option("--naive", is_flag=True, help="Baseline for comparisons: skip diagnosis, give the coder only the failure.")
def run(app_dir: Path, only: str | None, coder_model: str, run_id: str | None, resume_id: str | None,
        fail_first_fix: bool, naive: bool) -> None:
    """The full loop: plan, attack, diagnose, fix with Claude Code, verify by replay, commit. Unattended."""
    from . import loop
    run_id = resume_id or run_id or time.strftime("run-%Y%m%d-%H%M%S")
    ctx = _context(app_dir, run_id)
    if not _map_path(ctx).exists():
        raise click.ClickException("no screen map yet: run `droid-monkey explore` first")
    smap, prof, saved, known = _load_app(ctx)
    names = [n.strip() for n in only.split(",")] if only else None
    final = loop.run(ctx, smap, prof, saved, known, run_id, only=names, coder_model=coder_model,
                     resume=bool(resume_id), echo=click.echo, fail_first_fix=fail_first_fix, naive=naive)
    click.echo("")
    for o in final.get("outcomes", []):
        color = {"fixed": "green", "unfixed": "red", "survived": "white", "skipped": "yellow",
                 "unreachable": "yellow"}.get(o["status"])
        extra = f"  {o['commit'][:8]}" + (f" checkpoint {o['checkpoint']}" if o.get("checkpoint") else "") if o.get("commit") else ""
        click.echo(f"  {o['attack']:28} " + click.style(f"{o['status']:9}", fg=color) + f" was {o['before']}{extra}")
    click.echo(f"branch: {final.get('branch')}   evidence: {ctx.run_dir}")


@main.command()
@click.argument("run_id")
@APP_DIR
def report(run_id: str, app_dir: Path) -> None:
    """Write the PR body, score card and dashboard for a finished run."""
    from . import report as report_mod
    cfg = config_mod.load(app_dir)
    r = report_mod.build(cfg.app_dir, cfg.package, run_id)
    click.echo(f"resilience {r.score}/100, {r.fixed} of {r.total} attacks fixed")
    click.echo(f"  PR body:    {r.pr_body}\n  score card: {r.score_card}\n  dashboard:  {r.dashboard}")


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
    click.echo(f"pushing {branch} ...")
    subprocess.run(["git", "push", "-u", "origin", branch], cwd=cfg.app_dir, check=True)
    cmd = ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", str(r.pr_body)]
    if draft:
        cmd.append("--draft")
    out = subprocess.run(cmd, cwd=cfg.app_dir, capture_output=True, text=True)
    if out.returncode != 0:
        raise click.ClickException(out.stderr.strip())
    click.echo(click.style(out.stdout.strip(), fg="green"))


@main.command()
@click.argument("run_a")
@click.argument("run_b")
@APP_DIR
def compare(run_a: str, run_b: str, app_dir: Path) -> None:
    """Before/after table of two runs, e.g. a --naive baseline vs a normal run."""
    from . import report as report_mod
    cfg = config_mod.load(app_dir)
    md, out = report_mod.compare(cfg.app_dir, run_a, run_b)
    click.echo(md)
    click.echo(f"saved: {out}")
