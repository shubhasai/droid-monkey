"""`droid-monkey fix ISSUE`: fix one hunt or UX issue from the ranked list, with your consent, and check it.

1. Explain the issue (what, where, evidence, the steps that show it) and ask before changing any code.
2. On a droid-monkey/fix-<id> branch, the coding agent makes the smallest fix (the same rules and tools as `run`).
3. Build and install it, then check on the device: a hunt issue is re-walked from launch with the same steps and
   the same detectors (logs and screen); it must be gone. A UX issue gets its after-screenshot.
4. Commit only if the check passes; otherwise the sources go back. Either way you are back on your branch.

Attack failures are fixed by `run --attacks <name>`, which replays the identical attack.
"""

import json
import time
from pathlib import Path

import click

from . import agents, paths
from .driver import AppDriver, Context, NavigationError
from .issues import Issue
from .tools import git, gradle, ui
from .tools.adb import AdbError

BRIEF = """Fix this bug in the Android app. droid-monkey found it on a device: {source}.

{title}
What happens: {detail}
On screen: {screen}
Evidence: {evidence}
Steps from a fresh launch that show it: {steps}
{files}
Rules:
- Find the root cause in the code and make the smallest correct fix; keep everything else the same.
- Do not change build files or add dependencies. Do not commit.
- Call the compile tool ('main') before you finish and fix every error.
- Reply with one short paragraph: the cause and what you changed."""


def explain(x: Issue) -> str:
    lines = [click.style(f"── {x.title}", bold=True) + f"  ({x.source}, {x.severity}, ranked {x.score:.2f}: {x.rank_reason})",
             f"What happens: {x.detail}"]
    if x.screen:
        lines.append(f"Screen: {x.screen}")
    lines += ["Evidence:"] + [f"  {e}" for e in x.evidence] if x.evidence else []
    if x.steps:
        lines.append("Steps from launch: " + " > ".join(x.steps))
    lines.append("If you say yes, the coding agent fixes it on a branch, droid-monkey rebuilds the app and checks the "
                 "issue on the device, and commits only if it is gone.")
    return "\n".join(lines)


def still_there(ctx: Context, x: Issue, folder: str = "recheck") -> bool | None:
    """Re-walk a hunt issue's steps from a clean launch and run the same detectors. None: could not check."""
    from . import hunt
    d = AppDriver(ctx, ctx.run_dir / folder)
    try:
        d.fresh(clear_data=True)
        for step in x.steps:
            d.step(step)
            d.settle(timeout=d.NAV_SETTLE)
        screen, _, _ = d.settle(timeout=d.NAV_SETTLE)
        d.snapshot("after")
        log = ctx.adb.run("logcat", "-d", "-v", "threadtime", "-b", "main,system")
        pid = ctx.device.pid()
        seen = hunt.check_screen(screen, ctx.cfg.package) + hunt.scan_logs(log, ctx.cfg.package, {pid} if pid else set())
        if x.key.startswith("logic:"):
            return None  # a journey-level logic bug needs the model to re-judge it: the screenshot is the evidence
        return any(i.key == x.key for i in seen)
    except (NavigationError, AdbError):
        return None


def _record(ctx: Context, x: Issue, status: str, message: str, started: float, **extra) -> str:
    """fix.json in the run folder: what was fixed and how it ended, for the dashboard and the issue list."""
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "fix.json").write_text(json.dumps(
        {"issue": x.id, "key": x.key, "source": x.source, "title": x.title, "detail": x.detail, "screen": x.screen,
         "steps": x.steps, "status": status, "message": message, "seconds": round(time.time() - started), **extra},
        indent=1, ensure_ascii=False))
    return message


def fix(ctx: Context, x: Issue, agent: str, model: str | None, ask, others: list[Issue] | None = None) -> str:
    """`others`: open hunt issues on the same screen. After the fix they are re-walked too, and the ones that are gone
    are recorded as fixed by the same commit (one bug is often reported by the hunt and the UX audit both)."""
    cfg = ctx.cfg
    started = time.time()
    if x.source == "attack":
        raise click.ClickException(f"attack failures are fixed by replaying the attack: droid-monkey run --attacks {x.attack}")
    click.echo(explain(x))
    if ask() != "yes":
        return _record(ctx, x, "declined", "declined: no code was changed", started)
    home = git.current_branch(cfg.app_dir)
    branch = f"droid-monkey/fix-{x.id}"
    base = git.start_run_branch(cfg.app_dir, branch, cfg.module)
    before = git.file_states(cfg.app_dir)
    build_server = agents.McpServer("build", "droid_monkey.build_mcp",
                                    ["--app-dir", str(cfg.app_dir), "--module", cfg.module, "--variant", cfg.variant]
                                    + (["--java-home", cfg.java_home] if cfg.java_home else []))
    from .coder import ALLOWED, DENIED
    files = f"Files involved: {', '.join(x.files)}\n" if x.files else ""
    click.echo("  the coding agent is fixing it...")
    res = agents.run(agent, BRIEF.format(source={"hunt": "a bug in normal use", "ux": "a UX audit finding"}[x.source],
                                         title=x.title, detail=x.detail, screen=x.screen or "-",
                                         evidence="; ".join(x.evidence) or "-", steps=" > ".join(x.steps) or "-",
                                         files=files),
                     cfg.app_dir, allowed_tools=ALLOWED + ["mcp__build__compile"], disallowed_tools=DENIED,
                     servers=[build_server], model=model, timeout=1200, name=f"fix.{x.id}", llm=cfg.llm)
    touched = [p for p, h in git.file_states(cfg.app_dir).items() if before.get(p) != h]
    keep = [p for p in touched if p.startswith(f"{cfg.module}/src/")]
    git.discard(cfg.app_dir, [p for p in touched if p not in keep and not p.startswith(".droid-monkey/")])

    def give_up(why: str) -> str:
        git.discard(cfg.app_dir, git.dirty_paths(cfg.app_dir, f"{cfg.module}/src"))
        git.checkout(cfg.app_dir, home)
        git.delete_branch(cfg.app_dir, branch)
        return _record(ctx, x, "unfixed", f"not fixed: {why}; your code is unchanged and you are back on {home}", started,
                       reason=why, agent_note=res.text[:1500])

    click.echo(f"  {res.text[:400]}")
    if not keep:
        return give_up("the coding agent changed nothing")
    built = gradle.build(cfg.app_dir, cfg.module, cfg.variant, java_home=cfg.java_home)
    if not built.ok or not built.apk:
        return give_up("the change does not build")
    gradle.install(ctx.adb, built.apk, package=cfg.package, source_dir=cfg.app_dir)
    check = "built"
    if x.source == "hunt":
        gone = still_there(ctx, x)
        if gone is True:
            return give_up("the issue is still there after the fix (re-walked its steps on the device)")
        check = "re-walked on the device: gone" if gone is False else "built (re-check needs the model: see the screenshot)"
    also = []
    for o in others or []:
        if o.id != x.id and o.steps and still_there(ctx, o, f"recheck-{o.id}") is False:
            also.append(o)
    if also:
        click.echo("  also gone after this fix (re-walked on the device): " + "; ".join(o.title for o in also))
        if check == "built":
            check = "built; re-walked on the device: the same bug's hunt issue is gone"
    try:
        ctx.device.restore()
    except AdbError:
        pass
    sha, checkpoint = git.commit(cfg.app_dir, keep, f"fix: {x.title}\n\n{x.detail}\n\nFound by droid-monkey ({x.source}); "
                                                    f"check: {check}."
                                                    + "".join(f"\nAlso fixes: {o.title} (re-walked on the device: gone)" for o in also))
    git.checkout(cfg.app_dir, home)
    return _record(ctx, x, "fixed", f"fixed on {branch}: commit {sha[:8]}" + (f", Entire checkpoint {checkpoint}" if checkpoint else "")
                   + f" ({check}). Back on {home}; review the branch or open a PR.", started,
                   branch=branch, commit=sha, checkpoint=checkpoint, check=check, files=keep, agent_note=res.text[:1500],
                   also_fixed=[{"source": o.source, "key": o.key, "title": o.title} for o in also])
