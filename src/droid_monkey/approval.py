"""Ask the developer before droid-monkey changes their code: the issue, the evidence, and the suggested fix.

The run pauses (a LangGraph interrupt, so a paused run can be resumed later) with a fix proposal; the CLI shows it
and passes the answer back: yes (fix this one), no (record the suggestion and move on) or all (fix the rest
without asking).
"""

from pathlib import Path

import click

ANSWERS = {"y": "yes", "yes": "yes", "n": "no", "no": "no", "a": "all", "all": "all"}


def proposal(item: dict, result: dict, diagnosis: dict) -> dict:
    """Everything the developer needs to decide, from data droid-monkey measured, not from the model alone."""
    evidence = []
    if result.get("crash_signature"):
        evidence.append(f"{'ANR' if result.get('outcome') == 'anr' else 'crash'}: {result['crash_signature']}")
        evidence += [f"  {f}" for f in (result.get("app_frames") or [])[:5]]
    for name, (was, now) in (result.get("lost_fields") or {}).items():
        evidence.append(f"field {name}: '{was}' -> '{now}'")
    metrics = result.get("metrics") or {}
    if metrics.get("frames"):
        f = metrics["frames"]
        evidence.append(f"frames: {f['frozen']} frozen (>700 ms), {f['janky_pct']:.0f}% janky, p99 {f['p99_ms']:.0f} ms")
    if metrics.get("memory_after"):
        b, a = metrics["memory_before"], metrics["memory_after"]
        evidence.append(f"after forced GC: Activities {b['activities']} -> {a['activities']}, "
                        f"Java heap {b['java_heap_kb'] // 1024} -> {a['java_heap_kb'] // 1024} MB")
    leak = metrics.get("leak") or {}
    if leak.get("destroyed"):
        evidence.append("destroyed during the attack (LeakCanary): " + ", ".join(leak["destroyed"][:6]))
    if leak.get("trace"):
        evidence.append("leak trace (Shark): " + (", ".join(leak.get("leaking") or []) or "see leak-trace.txt"))
        evidence += ["  " + line for line in leak["trace"].splitlines()
                     if "~~~" in line or "Leaking: YES" in line or "╰→" in line or "GC Root" in line][:12]
    elif leak.get("note"):
        evidence.append(f"leak: {leak['note']}")
    for kind, runs in (metrics.get("startup") or {}).items():
        evidence.append(f"{kind} start: " + ", ".join(f"{r['ms']} ms" for r in runs))
    if metrics.get("trace"):
        from .tools.perfetto import TraceAnalysis
        evidence += ["perfetto: " + line for line in TraceAnalysis(**metrics["trace"]).summary().splitlines()[:12]]
    shots = Path(result.get("evidence_dir") or ".")
    files = [str(shots / n) for n in ("before.png", "after.png", "logcat.txt", "trace.perfetto-trace")
             if (shots / n).exists()]
    return {
        "attack": item["name"], "why": item.get("reason", ""), "outcome": result.get("outcome"),
        "detail": (result.get("reason") if result.get("outcome") == "anr" else result.get("exception"))
                  or result.get("reason", ""),
        "evidence": evidence, "evidence_files": files,
        "summary": diagnosis.get("summary", ""), "root_cause": diagnosis.get("root_cause", ""),
        "files_to_change": diagnosis.get("files_to_change", []), "fix_approach": diagnosis.get("fix_approach", ""),
        "test_plan": diagnosis.get("test_plan", ""), "code_evidence": diagnosis.get("evidence", []),
    }


def render(p: dict, footer: bool = True) -> str:
    from .theme import BADGES, FACES, style
    h = lambda t: style(t, bold=True)
    bar = style("| ", fg="bright_black")
    color = BADGES.get(p["outcome"], ("", "red"))[1]
    face = FACES.get(p["outcome"], FACES["worried"])
    # the monkey peeks over the top of the fix proposal
    lines = [""] + ["   " + style(row, fg=color, bold=True) for row in face[:-1]]
    lines.append(style("." + "-" * 7, fg="bright_black") + style(face[-1].strip(), fg=color, bold=True)
                 + style("--( ", fg="bright_black") + style(p["attack"], fg="yellow", bold=True)
                 + style(f": {BADGES.get(p['outcome'], ('', '', p['outcome']))[2]} )", fg=color, bold=True)
                 + style("-" * 30, fg="bright_black"))
    lines.append(bar + h("Issue:") + f" {p['detail']}")
    if p.get("retry"):
        r = p["retry"]
        lines.append(bar + style(f"Retry {r['attempt']} of {r['max']}: ", fg="yellow", bold=True)
                     + f"the previous fix failed verification: {r['previous_failed']}")
        lines.append(bar + "  The diagnosis below is revised. Saying no restores the code to the last good commit.")
    if p.get("why"):
        lines.append(bar + h("Why this attack:") + f" {p['why']}")
    if p["evidence"]:
        lines.append(bar + h("Evidence (measured on the device):"))
        lines += [bar + f"    {e}" for e in p["evidence"]]
    if p["evidence_files"]:
        lines.append(bar + style("    files: " + ", ".join(p["evidence_files"]), fg="bright_black"))
    lines.append(bar + h("Root cause:") + f" {p['root_cause']}")
    if p["code_evidence"]:
        lines += [bar + style(f"    {e}", fg="cyan") for e in p["code_evidence"][:6]]
    lines.append(bar + h("Suggested fix:") + f" {p['fix_approach']}")
    lines.append(bar + style(f"    files: {', '.join(p['files_to_change'])}", fg="bright_black"))
    lines.append(bar + h("Regression test:") + f" {p['test_plan']}")
    lines.append(style("'-- ", fg="bright_black") + style(
        "If you approve, a coding agent makes this fix; droid-monkey replays the identical attack and runs the "
        "test, and commits only if both pass." if footer else "suggested only: no code was changed", fg="bright_black"))
    return "\n".join(lines)


def ask_terminal(p: dict) -> str:
    from .theme import style
    click.echo(render(p))
    answer = click.prompt(style("(o.o)?", fg="yellow", bold=True) + " " + style("Fix it?", bold=True)
                          + " [y]es / [n]o / [a]ll remaining without asking",
                          default="y", show_default=False, type=click.Choice(sorted(ANSWERS), case_sensitive=False),
                          show_choices=False)
    return ANSWERS[answer.lower()]
