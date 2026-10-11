"""droid-monkey command line."""

import json
import time
from pathlib import Path

import click

from . import config as config_mod
from . import scenarios as scenarios_mod
from .attacks import CATALOGUE, Context
from .config import Scenario, Target
from .explorer import Explorer, ScreenMap
from .tools import emulator, gradle
from .tools.adb import Adb
from .tools.device import Device

OUTCOME_COLOR = {"survived": "green", "crashed": "red", "lost_state": "yellow", "skipped": "white"}
APP_DIR = click.option("--app-dir", type=click.Path(path_type=Path, file_okay=False), default=".",
                       help="The app's repo (holds monkey.yaml).")


def _context(app_dir: Path, run_id: str) -> Context:
    cfg = config_mod.load(app_dir)
    serial = emulator.running_emulator()
    adb = Adb(serial) if serial else emulator.boot(cfg.avd or emulator.list_avds()[0])
    run_dir = cfg.app_dir / "monkey-report" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return Context(adb=adb, device=Device(adb, cfg.package), cfg=cfg, run_dir=run_dir)


def _map_path(ctx: Context) -> Path:
    return ctx.cfg.app_dir / "monkey-report" / "screen-map.json"


def _explore(ctx: Context) -> ScreenMap:
    explorer = Explorer(ctx)
    smap = explorer.explore()
    smap.save(ctx.run_dir / "screen-map.json")
    smap.save(_map_path(ctx))
    for note in explorer.notes:
        click.echo(click.style(f"  note: {note}", dim=True))
    return smap


def _print_map(smap: ScreenMap) -> None:
    click.echo(f"{len(smap.screens)} screens explored in {smap.seconds:.0f}s (screen-read overhead {smap.baseline_seconds:.1f}s)")
    for s in smap.screens.values():
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


@click.group()
def main() -> None:
    """Attack an Android app with real-world faults, fix what breaks, prove each fix."""


@main.command()
@APP_DIR
def explore(app_dir: Path) -> None:
    """Explore the installed app and save its screen map (no faults injected)."""
    ctx = _context(app_dir, time.strftime("explore-%Y%m%d-%H%M%S"))
    smap = _explore(ctx)
    _print_map(smap)
    click.echo("\nattack candidates:")
    for c in scenarios_mod.generate(smap):
        click.echo(f"  {c.attack:18} [{c.screen}] {c.action or '-':28} {c.reason}")
    click.echo(f"map: {_map_path(ctx)}")


@main.command()
@click.argument("name", type=click.Choice(sorted(CATALOGUE) + ["all"]))
@APP_DIR
@click.option("--target", help="Screen id from the screen map (default: the best candidate for this attack).")
@click.option("--action", help="Trigger step, e.g. 'tap submit_button' (default: the best candidate's).")
@click.option("--re-explore", is_flag=True, help="Explore again even if a screen map exists.")
@click.option("--build/--no-build", default=False, help="Build and install the app first.")
@click.option("--run-id", default=None, help="Evidence folder name under monkey-report/.")
def attack(name: str, app_dir: Path, target: str | None, action: str | None, re_explore: bool, build: bool,
           run_id: str | None) -> None:
    """Run one attack (or all) against the installed app and print the verdict. No fixing."""
    run_id = run_id or time.strftime("attack-%Y%m%d-%H%M%S")
    ctx = _context(app_dir, run_id)
    if build:
        res = gradle.build(ctx.cfg.app_dir, ctx.cfg.module, ctx.cfg.variant)
        if not res.ok or not res.apk:
            raise click.ClickException(f"build failed:\n{res.log_tail}")
        gradle.install(ctx.adb, res.apk)
        click.echo(f"built and installed {res.apk.name} in {res.seconds:.0f}s")

    if re_explore or not _map_path(ctx).exists():
        click.echo("exploring the app first...")
        smap = _explore(ctx)
        _print_map(smap)
    else:
        smap = ScreenMap.load(_map_path(ctx))
    ctx.targets = {sid: Target(name=sid, steps=list(s.path), fill=s.has_form) for sid, s in smap.screens.items()}
    candidates = scenarios_mod.generate(smap)

    names = sorted(CATALOGUE) if name == "all" else [name]
    results = []
    for attack_name in names:
        best = scenarios_mod.best(candidates, attack_name)
        manual = ctx.cfg.scenarios.get(attack_name)
        if target:
            scenario = Scenario(target=target, action=action or None)
        elif best:
            scenario = Scenario(target=best.screen, action=best.action if action is None else (action or None),
                                settle_seconds=best.settle_seconds)
            click.echo(click.style(f"{attack_name}: ", bold=True) + best.reason)
        elif manual:
            scenario = manual
        else:
            click.echo(f"{attack_name:18} skipped: the explorer found nowhere to apply it")
            continue
        result = CATALOGUE[attack_name](ctx).run(scenario)
        results.append(result)
        detail = result.exception or result.reason
        click.echo(f"{attack_name:18} " + click.style(f"{result.outcome:10}", fg=OUTCOME_COLOR.get(result.outcome))
                   + f" {detail}  ({result.seconds:.0f}s)")
        if result.crash_signature:
            click.echo(f"{'':18} at {result.crash_signature}")
        for key, (old, new) in result.lost_fields.items():
            click.echo(f"{'':18} {key}: {old!r} -> {new!r}")
    (ctx.run_dir / "results.json").write_text(json.dumps([r.__dict__ for r in results], indent=2))
    click.echo(f"evidence: {ctx.run_dir}")
