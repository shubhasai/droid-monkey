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
        evidence.append(f"crash: {result.get('exception')} at {result['crash_signature']}")
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
    if metrics.get("trace"):
        from .tools.perfetto import TraceAnalysis
        evidence += ["perfetto: " + line for line in TraceAnalysis(**metrics["trace"]).summary().splitlines()[:6]]
    shots = Path(result.get("evidence_dir") or ".")
    files = [str(shots / n) for n in ("before.png", "after.png", "logcat.txt", "trace.perfetto-trace")
             if (shots / n).exists()]
    return {
        "attack": item["name"], "why": item.get("reason", ""), "outcome": result.get("outcome"),
        "detail": result.get("exception") or result.get("reason", ""),
        "evidence": evidence, "evidence_files": files,
        "summary": diagnosis.get("summary", ""), "root_cause": diagnosis.get("root_cause", ""),
        "files_to_change": diagnosis.get("files_to_change", []), "fix_approach": diagnosis.get("fix_approach", ""),
        "test_plan": diagnosis.get("test_plan", ""), "code_evidence": diagnosis.get("evidence", []),
    }


def render(p: dict) -> str:
    h = lambda t: click.style(t, bold=True)
    lines = ["", click.style(f"── {p['attack']}: {p['outcome']} ", fg="red", bold=True) + "─" * 40,
             f"{h('Issue:')} {p['detail']}"]
    if p.get("why"):
        lines.append(f"{h('Why this attack:')} {p['why']}")
    if p["evidence"]:
        lines.append(h("Evidence (measured on the device):"))
        lines += [f"  {e}" for e in p["evidence"]]
    if p["evidence_files"]:
        lines.append("  files: " + ", ".join(p["evidence_files"]))
    lines.append(f"{h('Root cause:')} {p['root_cause']}")
    if p["code_evidence"]:
        lines += [f"  {e}" for e in p["code_evidence"][:6]]
    lines.append(f"{h('Suggested fix:')} {p['fix_approach']}")
    lines.append(f"  files: {', '.join(p['files_to_change'])}")
    lines.append(f"{h('Regression test:')} {p['test_plan']}")
    lines.append("If you approve, Claude Code makes this fix; droid-monkey replays the identical attack and runs the "
                 "test, and commits only if both pass.")
    return "\n".join(lines)


def ask_terminal(p: dict) -> str:
    click.echo(render(p))
    answer = click.prompt("Fix it? [y]es / [n]o / [a]ll remaining without asking", default="y",
                          show_default=False, type=click.Choice(sorted(ANSWERS), case_sensitive=False),
                          show_choices=False)
    return ANSWERS[answer.lower()]
