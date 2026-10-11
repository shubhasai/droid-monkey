"""droid-monkey command line."""

import time
from pathlib import Path

import click

from . import config as config_mod
from .attacks import CATALOGUE, Context
from .config import Scenario
from .tools import emulator, gradle
from .tools.adb import Adb
from .tools.device import Device

OUTCOME_COLOR = {"survived": "green", "crashed": "red", "lost_state": "yellow", "skipped": "white"}


def _context(app_dir: Path, run_id: str) -> Context:
    cfg = config_mod.load(app_dir)
    serial = emulator.running_emulator()
    adb = Adb(serial) if serial else emulator.boot(cfg.avd or emulator.list_avds()[0])
    run_dir = cfg.app_dir / "monkey-report" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return Context(adb=adb, device=Device(adb, cfg.package), cfg=cfg, run_dir=run_dir)


@click.group()
def main() -> None:
    """Attack an Android app with real-world faults, fix what breaks, prove each fix."""


@main.command()
@click.argument("name", type=click.Choice(sorted(CATALOGUE) + ["all"]))
@click.option("--app-dir", type=click.Path(path_type=Path, file_okay=False), default=".", help="The app repo (holds monkey.yaml).")
@click.option("--target", help="Target screen from monkey.yaml (default: the attack's scenario).")
@click.option("--action", help="Trigger step, e.g. 'tap submit_button' (default: the attack's scenario).")
@click.option("--build/--no-build", default=False, help="Build and install the app first.")
@click.option("--run-id", default=None, help="Evidence folder name under monkey-report/.")
def attack(name: str, app_dir: Path, target: str | None, action: str | None, build: bool, run_id: str | None) -> None:
    """Run one attack (or all) against the installed app and print the verdict. No fixing."""
    run_id = run_id or time.strftime("attack-%Y%m%d-%H%M%S")
    ctx = _context(app_dir, run_id)
    if build:
        res = gradle.build(ctx.cfg.app_dir, ctx.cfg.module, ctx.cfg.variant)
        if not res.ok or not res.apk:
            raise click.ClickException(f"build failed:\n{res.log_tail}")
        gradle.install(ctx.adb, res.apk)
        click.echo(f"built and installed {res.apk.name} in {res.seconds:.0f}s")

    names = sorted(CATALOGUE) if name == "all" else [name]
    for attack_name in names:
        default = ctx.cfg.scenarios.get(attack_name)
        if default is None and not target:
            click.echo(f"{attack_name:18} skipped: no scenario in monkey.yaml and no --target")
            continue
        scenario = Scenario(
            target=target or default.target,
            action=action if action is not None else (default.action if default else None),
            settle_seconds=default.settle_seconds if default else 4.0,
        )
        result = CATALOGUE[attack_name](ctx).run(scenario)
        detail = result.exception or result.reason
        click.echo(f"{attack_name:18} " + click.style(f"{result.outcome:10}", fg=OUTCOME_COLOR.get(result.outcome))
                   + f" {detail}  ({result.seconds:.0f}s)")
        if result.crash_signature:
            click.echo(f"{'':18} at {result.crash_signature}")
        for key, (old, new) in result.lost_fields.items():
            click.echo(f"{'':18} {key}: {old!r} -> {new!r}")
    click.echo(f"evidence: {ctx.run_dir}")
