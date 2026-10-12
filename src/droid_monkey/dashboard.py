"""The run report as one HTML page: what droid-monkey found and fixed, and the evidence for each attack.

Summary first (score, the numbers that matter, the receipt, every attack in one table), then one case per attack:
the verdict and diagnosis next to the screen recordings (the attack, and the identical attack replayed after the
fix), the measurements as charts, and the raw evidence (screenshots, logs, Perfetto, the fix's diff) folded away.
The page is self-contained apart from the run folder's own media files, so it opens straight from disk.
"""

import html
import json
import subprocess
from pathlib import Path

from . import charts

esc = html.escape

STATUS = {  # how each outcome reads, and its tone
    "fixed": ("Fixed", "ok"), "survived": ("Survived", "ok"), "declined": ("Fix suggested", "warn"),
    "unfixed": ("Not fixed", "bad"), "found": ("Broken", "bad"), "skipped": ("Skipped", "muted"),
    "unreachable": ("Screen unreachable", "warn"),
}
OUTCOME = {  # what the attack saw, in words
    "crashed": "crashed", "anr": "not responding (ANR)", "lost_state": "lost the user's input",
    "no_feedback": "failed silently", "leaked": "leaked memory", "froze": "froze the UI", "slow_start": "slow to start",
    "survived": "survived", "unreachable": "screen unreachable",
}
GLYPH = {"ok": "✓", "bad": "✕", "warn": "!", "muted": "–"}
FAULT_WORDS = {"process_death": "process death", "rotation": "rotation", "network_loss": "network loss",
               "permission_revoke": "permission revoked", "low_memory": "memory pressure", "performance": "CPU load",
               "startup": "cold and hot starts", "background": "backgrounding", "configuration_change": "configuration change",
               "custom": "custom attack"}


def _broke(o: dict) -> str:
    return "true" if o.get("before") not in ("survived", "unreachable") else "false"


def _verdict(ev: Path) -> dict:
    try:
        return json.loads((ev / "verdict.json").read_text())
    except (OSError, ValueError):
        return {}


def _latest_replay(ev: Path) -> Path | None:
    replays = sorted((p for p in ev.glob("replay-*") if p.is_dir()), key=lambda p: p.name)
    return replays[-1] if replays else None


def _fix_diff(app_dir: Path | None, commit: str | None) -> str:
    if not app_dir or not commit:
        return ""
    out = subprocess.run(["git", "show", "--format=", "--no-color", commit], cwd=app_dir, capture_output=True,
                         text=True, stdin=subprocess.DEVNULL).stdout
    rows = []
    for ln in out.splitlines()[:500]:
        cls = ("add" if ln.startswith("+") and not ln.startswith("+++") else
               "del" if ln.startswith("-") and not ln.startswith("---") else
               "hunk" if ln.startswith("@@") else "file" if ln.startswith(("diff ", "+++", "---", "index ", "new file")) else "")
        rows.append(f'<span class="{cls}">{esc(ln) or " "}</span>' if cls else esc(ln))
    return '<div class="scroll"><pre class="diff">' + "\n".join(rows) + "</pre></div>" if rows else ""


def _video(src: str, caption: str, tone: str, poster: str = "") -> str:
    poster_attr = f' poster="{poster}"' if poster else ""
    return (f'<figure class="clip"><video controls muted playsinline preload="metadata"{poster_attr} src="{src}"></video>'
            f'<figcaption><span class="dot {tone}"></span>{esc(caption)}</figcaption></figure>')


def _screens(o: dict, ev: Path, rel: str) -> str:
    captions = {"before": "Start screen", "fault": "When the fault hit", "after": "After the fault"}
    shots = [(n, c) for n, c in captions.items() if (ev / f"{n}.png").exists()]
    replay = _latest_replay(ev)
    if replay is not None and (replay / "after.png").exists() and o["status"] == "fixed":
        shots.append((f"{replay.name}/after", "After the fix (replay)"))
    return "".join(f'<figure class="shot"><button type="button" class="zoom" data-src="{rel}/{n}.png" '
                   f'aria-label="Enlarge: {esc(c)}"><img loading="lazy" src="{rel}/{n}.png" alt="{esc(c)}"></button>'
                   f"<figcaption>{esc(c)}</figcaption></figure>" for n, c in shots)


def _diagnosis(o: dict) -> str:
    d = o.get("diagnosis") or {}
    if not d:
        return '<p class="note">Not diagnosed: the app survived this attack.</p>' if o["status"] == "survived" else ""
    files = "".join(f"<code class='chip'>{esc(f)}</code>" for f in d.get("files_to_change", []))
    label = "Suggested fix (not applied)" if o["status"] in ("declined", "found") else "Fix"
    rows = [("Root cause", f"<p>{esc(d.get('root_cause', ''))}</p>"),
            (label, f"<p>{esc(d.get('fix_approach', ''))}</p>")]
    if files:
        rows.append(("Files", f'<div class="chips">{files}</div>'))
    if d.get("test_plan") and o["status"] != "fixed":
        rows.append(("Regression test", f"<p>{esc(d['test_plan'])}</p>"))
    if o.get("commit"):
        tests = ", ".join(Path(t).stem for t in o.get("tests", [])) or "-"
        rows.append(("Proof", f"<p>Replayed the identical attack: survived. <code>{esc(tests)}</code> passes. "
                              f"Commit <code>{esc(o['commit'][:8])}</code>"
                              + (f", Entire checkpoint <code>{esc(o['checkpoint'])}</code>" if o.get("checkpoint") else "")
                              + f". {o.get('attempts', 1)} attempt{'s' if o.get('attempts', 1) != 1 else ''}.</p>"))
    return '<dl class="facts">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>"


def _measurements(o: dict, v: dict) -> str:
    from .runner import SpecRunner
    m = v.get("metrics") or o.get("metrics") or {}
    trace = m.get("trace") or {}
    blocks = []
    timeline = charts.main_thread(trace.get("main_timeline") or {}, trace.get("long_main_slices") or [])
    if timeline:
        blocks.append(("Main thread over the attack", "Perfetto: what the UI thread did; long blocks are boxed", timeline))
    frames = charts.frame_times((m.get("frames") or {}).get("histogram") or [])
    if frames:
        blocks.append(("Frame times", "Android draws a frame every 16 ms; Play counts a frame over 700 ms as frozen", frames))
    if m.get("startup"):
        blocks.append(("Launch times", "Time to the first frame, against the Play vitals limits",
                       charts.launches(m["startup"], SpecRunner.START_LIMITS_MS)))
    mem = charts.memory(m.get("memory_before") or {}, m.get("memory_after") or {})
    if mem:
        blocks.append(("Memory after a forced GC", "Destroyed screens still alive mean something holds on to them", mem))
    rss = charts.rss(trace.get("rss_series") or [])
    if rss:
        blocks.append(("Resident memory", "The app's RSS over the trace", rss))
    lost = v.get("lost_fields") or {}
    if lost:
        rows = "".join(f"<tr><td><code>{esc(k)}</code></td><td>{esc(str(b))}</td>"
                       f"<td class='gone'>{esc(str(a)) if a else 'empty'}</td></tr>" for k, (b, a) in lost.items())
        blocks.insert(0, ("Lost input", "Each field the user filled in, before and after the fault",
                          f"<div class='scroll'><table class='grid'><thead><tr><th>Field</th><th>Before</th><th>After</th>"
                          f"</tr></thead><tbody>{rows}</tbody></table></div>"))
    return "".join(f'<section class="measure"><h4>{t}</h4><p class="hint">{h}</p>{body}</section>' for t, h, body in blocks)


def _case(o: dict, run_dir: Path, app_dir: Path | None, fault: str) -> str:
    rel, ev = o["attack"], run_dir / o["attack"]
    v = _verdict(ev)
    m = v.get("metrics") or o.get("metrics") or {}
    label, tone = STATUS.get(o["status"], (o["status"], "muted"))
    seen = OUTCOME.get(o["before"], o["before"])
    now = {"fixed": "survives the replay", "survived": "survived", "declined": "unchanged", "unfixed": "still fails",
           "found": "not fixed (attack only)"}.get(o["status"], o["status"])

    clips = []
    if (ev / "recording.mp4").exists():
        still = next((f"{rel}/{n}.png" for n in ("fault", "after", "before") if (ev / f"{n}.png").exists()), "")
        clips.append(_video(f"{rel}/recording.mp4", f"The attack: {seen}", "bad" if o["before"] != "survived" else "ok", still))
    replay = _latest_replay(ev)
    if replay is not None and o["status"] == "fixed" and (replay / "recording.mp4").exists():
        still = next((f"{rel}/{replay.name}/{n}.png" for n in ("after", "fault") if (replay / f"{n}.png").exists()), "")
        clips.append(_video(f"{rel}/{replay.name}/recording.mp4", "The same attack after the fix: survived", "ok", still))
    media = ""
    if clips:
        both = '<button type="button" class="btn play-both">Play both from the start</button>' if len(clips) == 2 else ""
        media = f'<div class="screen"><div class="clips">{"".join(clips)}</div>{both}</div>'

    details = []
    shots = _screens(o, ev, rel)
    if shots:
        details.append(("Screenshots", f'<div class="shots">{shots}</div>', True))
    diff = _fix_diff(app_dir, o.get("commit"))
    if diff:
        details.append((f"The fix, commit {esc(o['commit'][:8])}", diff, False))
    if (ev / "logcat.txt").exists():
        name = "Android's ANR record" if o["before"] == "anr" else "Crash log"
        details.append((name, f"<div class='scroll'><pre>{esc((ev / 'logcat.txt').read_text()[:8000])}</pre></div>", False))
    if (ev / "leak-trace.txt").exists():
        details.append(("Leak trace (Shark)", f"<div class='scroll'><pre>{esc((ev / 'leak-trace.txt').read_text())}</pre></div>"
                        f"<p class='hint'>The reference chain from a GC root to the leaked screen; the suspects are "
                        f"underlined with <code>~~~</code>. Heap dump: <code>{rel}/heap.hprof</code> (Android Studio "
                        "&rarr; Profiler &rarr; Load from file).</p>", True))
    if (ev / "trace-analysis.txt").exists():
        details.append(("Perfetto analysis", f"<div class='scroll'><pre>{esc((ev / 'trace-analysis.txt').read_text())}</pre></div>"
                        f"<p class='hint'>Open <code>{rel}/trace.perfetto-trace</code> at "
                        "<a href='https://ui.perfetto.dev' target='_blank' rel='noopener'>ui.perfetto.dev</a> for the full trace.</p>", False))
    evidence = (o.get("diagnosis") or {}).get("evidence") or []
    if evidence:
        details.append(("Code evidence", "<ul class='evidence'>" + "".join(f"<li><code>{esc(e)}</code></li>" for e in evidence[:10]) + "</ul>", False))

    meta = [FAULT_WORDS.get(fault, fault), f"screen {m['screen']}" if m.get("screen") else "", f"{m['seconds']:.0f}s" if m.get("seconds") else ""]
    chips = "".join(f'<span class="chip">{esc(x)}</span>' for x in meta if x)
    return f"""<article class="case" id="{esc(o['attack'])}" data-status="{esc(o['status'])}" data-broke="{_broke(o)}">
  <header class="case-head">
    <span class="badge {tone}" aria-hidden="true">{GLYPH[tone]}</span>
    <div class="case-title"><h3>{esc(o['attack'])}</h3><div class="chips">{chips}</div></div>
    <span class="pill {tone}">{esc(label)}</span>
  </header>
  <p class="transition"><span class="was">{esc(seen)}</span><span class="arrow" aria-hidden="true">→</span><span class="now {tone}">{esc(now)}</span></p>
  <p class="detail">{esc(str(o.get('before_detail', '')))}</p>
  <div class="case-body{' has-media' if media else ''}">
    <div class="case-text">{_diagnosis(o)}</div>
    {media}
  </div>
  {_measurements(o, v)}
  {''.join(f'<details{" open" if opened else ""}><summary>{t}</summary>{body}</details>' for t, body, opened in details)}
</article>"""


def _receipt_table(r: dict) -> str:
    api = r["reasoning_cost"] + r["diag_cost"] + (r["coder_cost"] if r["coder_agent"] == "api" else 0.0)
    rows = [
        ("Plan and decide", f"{r['reasoning_model']}", f"{r['reasoning_tokens']:,}", f"${r['reasoning_cost']:.4f}", "API"),
        ("Diagnosis", f"{r['reasoning_model']}", f"{r['diag_tokens']:,}", f"${r['diag_cost']:.4f}", "API"),
        ("Fixes", f"{r['coder_agent']} · {r['coder_model']} · {r['coder_sessions']} session{'s' if r['coder_sessions'] != 1 else ''}",
         f"{r['coder_tokens']:,}", f"${r['coder_cost']:.2f}", r["coder_billing"]),
    ]
    body = "".join(f"<tr><td>{a}</td><td>{esc(b)}</td><td class='num'>{c}</td><td class='num'>{d}</td><td>{esc(e)}</td></tr>"
                   for a, b, c, d, e in rows)
    plan = 0.0 if r["coder_agent"] == "api" else r["coder_cost"]
    total = f"${api:.4f} API" + (f" + ${plan:.2f} {r['coder_billing']}" if plan else "")
    return (f"<div class='scroll'><table class='grid receipt'><thead><tr><th>Step</th><th>Model</th><th class='num'>Tokens</th>"
            f"<th class='num'>Cost</th><th>Billed as</th></tr></thead><tbody>{body}</tbody>"
            f"<tfoot><tr><td>Total</td><td colspan='4'>{esc(total)}</td></tr></tfoot></table></div>")


def render(summary: dict, package: str, run_dir: Path, app_dir: Path | None, score: int, receipt: dict, fault_of) -> str:
    outs = summary.get("outcomes", [])
    counts = {k: sum(1 for o in outs if o["status"] == k) for k in STATUS}
    problems = sum(r for r in receipt["found"].values())
    mode = summary.get("mode", "droid-monkey")
    attack_only = mode == "attack"

    index = "".join(
        f'<li data-status="{esc(o["status"])}" data-broke="{_broke(o)}"><a href="#{esc(o["attack"])}"><span class="dot {STATUS.get(o["status"], ("", "muted"))[1]}"></span>'
        f'{esc(o["attack"])}</a></li>' for o in outs)
    table = "".join(
        f'<tr data-status="{esc(o["status"])}" data-broke="{_broke(o)}"><td><a href="#{esc(o["attack"])}">{esc(o["attack"])}</a></td>'
        f'<td>{esc(fault_of(o["attack"]))}</td><td>{esc(OUTCOME.get(o["before"], o["before"]))}</td>'
        f'<td><span class="pill {STATUS.get(o["status"], ("", "muted"))[1]}">{esc(STATUS.get(o["status"], (o["status"], ""))[0])}</span></td>'
        f'<td class="num">{(o.get("metrics") or {}).get("seconds", 0):.0f}s</td></tr>' for o in outs)
    filters = [("all", "All", len(outs)), ("problems", "Broke", sum(1 for o in outs if o["before"] not in ("survived", "unreachable"))),
               ("fixed", "Fixed", counts["fixed"]), ("survived", "Survived", counts["survived"]),
               ("open", "Still open", counts["declined"] + counts["unfixed"] + counts["found"])]
    filter_html = "".join(f'<button type="button" class="filter" data-filter="{k}" aria-pressed="{str(k == "all").lower()}">'
                          f'{name} <span class="count">{n}</span></button>' for k, name, n in filters if n or k == "all")
    found = ", ".join(f"{n} {OUTCOME.get(k, k)}" for k, n in sorted(receipt["found"].items(), key=lambda kv: -kv[1]))
    stats = [("Attacks", str(receipt["attacks"]), f"on {receipt['screens']} screen{'s' if receipt['screens'] != 1 else ''}" if receipt["screens"] else ""),
             ("Broke the app", str(problems), found or "nothing broke"),
             ("Fixed", str(receipt["fixed"]), "attack only, no fixing" if attack_only else f"{receipt['first_try']} on the first try"),
             ("Time", f"{receipt['minutes']}", "minutes")]
    stat_html = "".join(f'<div class="stat"><span class="label">{a}</span><span class="value">{b}</span><span class="sub">{esc(c)}</span></div>'
                        for a, b, c in stats)
    ring = 2 * 3.14159 * 42
    meta = [f"run <code>{esc(summary['run_id'])}</code>", f"branch <code>{esc(str(summary.get('branch')))}</code>"]
    if summary.get("coder_agent") and not attack_only:
        meta.append(f"fixes by <code>{esc(summary['coder_agent'])}</code>")
    if summary.get("finished"):
        meta.append(esc(summary["finished"]))
    cases = "".join(_case(o, run_dir, app_dir, fault_of(o["attack"])) for o in outs)

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>droid-monkey · {esc(package)} · {esc(summary['run_id'])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style></head><body>
<div class="frame">
<nav class="rail" aria-label="Attacks"><p class="rail-title">Attacks</p><ol>{index}</ol></nav>
<main>
<header class="top">
  <div class="title">
    <p class="eyebrow">droid-monkey {'attack report' if attack_only else 'run report'}</p>
    <h1>{esc(package)}</h1>
    <p class="meta">{' · '.join(meta)}</p>
  </div>
  <div class="score" title="Share of attacks the app now survives">
    <svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="42" class="track"/>
      <circle cx="50" cy="50" r="42" class="arc" style="stroke-dasharray:{ring * score / 100:.1f} {ring:.1f}"/></svg>
    <div class="score-text"><span class="score-num">{score}</span><span class="score-of">resilience</span></div>
  </div>
</header>
<section class="stats">{stat_html}</section>
<section class="panel">
  <div class="panel-head"><h2>Every attack</h2><div class="filters" role="group" aria-label="Filter attacks">{filter_html}</div></div>
  <div class="scroll"><table class="grid outcomes"><thead><tr><th>Attack</th><th>Fault</th><th>What it saw</th><th>Now</th><th class="num">Time</th></tr></thead>
  <tbody>{table}</tbody></table></div>
</section>
<section class="panel"><div class="panel-head"><h2>Receipt</h2><p class="hint">What this run cost, step by step.</p></div>{_receipt_table(receipt)}</section>
<h2 class="cases-title">Evidence, attack by attack</h2>
{cases}
<footer class="foot">Generated by <a href="https://github.com/shubhasai/droid-monkey" target="_blank" rel="noopener">droid-monkey</a>. Recordings, screenshots and traces live next to this file in the run folder.</footer>
</main></div>
<div class="lightbox" hidden><button type="button" class="close" aria-label="Close">×</button><img alt=""></div>
<script>{JS}</script>
</body></html>"""


CSS = """
/* Layout: a narrow attack index on the left, the report in one reading column; the index becomes a strip on phones. */
:root{
  --bg:#f3f5f7; --surface:#ffffff; --sunken:#e9edf1; --fg:#17202b; --muted:#5b6776; --line:#d5dce3;
  --accent:#1f5f8b; --acc:var(--accent); --ok:#1c7a4a; --bad:#b4342b; --warn:#a96a12; --idle:#c3cbd4;
  --ok-soft:#e1f2e8; --bad-soft:#f8e3e1; --warn-soft:#f6ecdb;
  --display:"IBM Plex Sans Condensed","Arial Narrow",system-ui,sans-serif;
  --body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0f141a; --surface:#161d25; --sunken:#1d2630; --fg:#e4e9ef; --muted:#93a0af; --line:#2a3541;
  --accent:#7db4dc; --ok:#5cc98d; --bad:#f08379; --warn:#e3ad55; --idle:#34404d;
  --ok-soft:#173326; --bad-soft:#3a1d1b; --warn-soft:#3a2c14; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0f141a; --surface:#161d25; --sunken:#1d2630; --fg:#e4e9ef; --muted:#93a0af; --line:#2a3541;
  --accent:#7db4dc; --ok:#5cc98d; --bad:#f08379; --warn:#e3ad55; --idle:#34404d;
  --ok-soft:#173326; --bad-soft:#3a1d1b; --warn-soft:#3a2c14; color-scheme:dark}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{scroll-behavior:smooth;scroll-padding-top:16px}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 var(--body);-webkit-font-smoothing:antialiased}
a{color:var(--accent)} code{font:0.88em var(--mono)}
h1,h2,h3,h4{font-family:var(--display);text-wrap:balance;margin:0;line-height:1.2}
.frame{display:grid;grid-template-columns:220px minmax(0,1fr);gap:28px;max-width:1240px;margin:0 auto;padding-inline:24px;padding-block:28px 48px}
.rail{position:sticky;top:calc(env(safe-area-inset-top,0px) + 20px);align-self:start;max-height:calc(100vh - 40px);overflow:auto}
.rail-title{font:600 11px var(--body);letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:4px 0 8px}
.rail ol{list-style:none;margin:0;padding:0;display:grid;gap:2px}
.rail a{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;color:var(--fg);text-decoration:none;font:13px var(--mono);overflow-wrap:anywhere}
.rail a:hover{background:var(--sunken)}
main{min-width:0;display:grid;gap:20px}
.top{display:flex;justify-content:space-between;align-items:center;gap:24px;flex-wrap:wrap}
.eyebrow{font:600 12px var(--body);letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin:0 0 4px}
h1{font-size:clamp(24px,4vw,34px);font-weight:700;overflow-wrap:anywhere}
.meta{color:var(--muted);margin:6px 0 0;font-size:13px}
.score{position:relative;width:118px;height:118px;flex:none}
.score svg{width:100%;height:100%;transform:rotate(-90deg)}
.score circle{fill:none;stroke-width:9}
.score .track{stroke:var(--sunken)} .score .arc{stroke:var(--accent);stroke-linecap:round}
.score-text{position:absolute;inset:0;display:grid;place-content:center;text-align:center}
.score-num{font:700 34px/1 var(--display);font-variant-numeric:tabular-nums}
.score-of{font-size:11px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;display:grid;gap:2px;min-width:0}
.stat .label{font-size:12px;color:var(--muted);letter-spacing:.04em;text-transform:uppercase}
.stat .value{font:700 28px/1.1 var(--display);font-variant-numeric:tabular-nums}
.stat .sub{font-size:13px;color:var(--muted)}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px 18px;min-width:0}
.panel-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.panel-head h2{font-size:18px}
.filters{display:flex;gap:6px;flex-wrap:wrap}
.filter{font:500 13px var(--body);color:var(--fg);background:var(--sunken);border:1px solid var(--line);border-radius:999px;padding:4px 12px;cursor:pointer}
.filter[aria-pressed="true"]{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.filter .count{font-family:var(--mono);opacity:.7;margin-left:2px}
.scroll{overflow-x:auto;max-width:100%}
.grid{border-collapse:collapse;width:100%;font-size:14px}
.grid th{font:600 12px var(--body);color:var(--muted);text-align:left;letter-spacing:.04em;text-transform:uppercase;padding:6px 10px;border-bottom:1px solid var(--line)}
.grid td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
.grid td:first-child a{font-family:var(--mono);font-size:13px}
.grid .num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.grid tfoot td{font-weight:600;border-bottom:none}
.grid .gone{color:var(--bad);font-style:italic}
.pill{display:inline-block;font:600 12px var(--body);padding:3px 10px;border-radius:999px;white-space:nowrap}
.pill.ok{background:var(--ok-soft);color:var(--ok)} .pill.bad{background:var(--bad-soft);color:var(--bad)}
.pill.warn{background:var(--warn-soft);color:var(--warn)} .pill.muted{background:var(--sunken);color:var(--muted)}
.dot{width:8px;height:8px;border-radius:50%;flex:none;display:inline-block;background:var(--muted)}
.dot.ok{background:var(--ok)} .dot.bad{background:var(--bad)} .dot.warn{background:var(--warn)}
.cases-title{font-size:20px;margin-top:8px}
.case{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px 20px;display:grid;gap:12px;min-width:0}
.case:target{outline:2px solid var(--accent);outline-offset:2px}
.case-head{display:flex;align-items:flex-start;gap:12px}
.badge{width:30px;height:30px;border-radius:8px;display:grid;place-content:center;font-weight:700;flex:none}
.badge.ok{background:var(--ok-soft);color:var(--ok)} .badge.bad{background:var(--bad-soft);color:var(--bad)}
.badge.warn{background:var(--warn-soft);color:var(--warn)} .badge.muted{background:var(--sunken);color:var(--muted)}
.case-title{flex:1;min-width:0;display:grid;gap:6px}
.case-title h3{font-size:19px;font-family:var(--mono);font-weight:500;overflow-wrap:anywhere}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{font:12px var(--mono);background:var(--sunken);color:var(--muted);padding:2px 8px;border-radius:6px;overflow-wrap:anywhere}
.transition{margin:0;display:flex;align-items:center;gap:10px;flex-wrap:wrap;font:600 15px var(--body)}
.transition .was{color:var(--bad)} .transition .arrow{color:var(--muted)}
.transition .now.ok{color:var(--ok)} .transition .now.bad{color:var(--bad)} .transition .now.warn{color:var(--warn)}
.detail{margin:0;color:var(--muted);font:13px/1.5 var(--mono);overflow-wrap:anywhere}
.case-body{display:grid;gap:20px}
.case-body.has-media{grid-template-columns:minmax(0,1fr)}
@media (min-width:1240px){.case-body.has-media{grid-template-columns:minmax(380px,1fr) auto}}
.case-text{min-width:0}
.facts{margin:0;display:grid;grid-template-columns:130px minmax(0,1fr);gap:10px 16px}
.facts dt{font:600 12px var(--body);color:var(--muted);letter-spacing:.04em;text-transform:uppercase;padding-top:3px}
.facts dd{margin:0;min-width:0} .facts p{margin:0;max-width:68ch;overflow-wrap:anywhere}
.note{color:var(--muted);margin:0}
.screen{display:grid;gap:8px;justify-items:start}
.clips{display:flex;gap:12px;flex-wrap:wrap}
.clip{margin:0;display:grid;gap:6px}
.clip video{width:200px;max-width:100%;aspect-ratio:1080/2424;border-radius:14px;border:1px solid var(--line);background:#000}
.clip figcaption{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);max-width:200px}
.btn{font:500 13px var(--body);background:var(--fg);color:var(--bg);border:none;border-radius:8px;padding:6px 12px;cursor:pointer}
.measure{border-top:1px solid var(--line);padding-top:12px;display:grid;gap:4px;min-width:0}
.measure h4{font-size:15px} .hint{margin:0 0 4px;color:var(--muted);font-size:13px}
svg.chart{width:100%;height:auto;display:block}
details{border-top:1px solid var(--line);padding-top:10px}
summary{cursor:pointer;font-weight:600;color:var(--fg);list-style-position:outside}
summary:hover{color:var(--accent)}
details[open] > summary{margin-bottom:10px}
.shots{display:flex;gap:12px;flex-wrap:wrap}
.shot{margin:0;display:grid;gap:4px}
.zoom{padding:0;border:none;background:none;cursor:zoom-in;border-radius:10px}
.shot img{width:130px;max-width:100%;border-radius:10px;border:1px solid var(--line);display:block}
.shot figcaption{font-size:12px;color:var(--muted)}
pre{margin:0;font:12px/1.5 var(--mono);background:var(--sunken);padding:12px;border-radius:8px;white-space:pre}
pre.diff .add{color:var(--ok)} pre.diff .del{color:var(--bad)} pre.diff .hunk{color:var(--accent)} pre.diff .file{color:var(--muted);font-weight:600}
.evidence{margin:0;padding-left:18px;display:grid;gap:4px;font-size:13px}
.foot{color:var(--muted);font-size:13px;padding-top:8px}
.lightbox{position:fixed;inset:0;background:rgba(8,12,16,.86);display:grid;place-items:center;padding:24px;z-index:10}
.lightbox img{max-height:92vh;max-width:100%;border-radius:12px}
.lightbox .close{position:absolute;top:calc(env(safe-area-inset-top,0px) + 12px);right:16px;font-size:28px;background:none;border:none;color:#fff;cursor:pointer}
button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.svg-text{fill:var(--muted)}
@media (max-width:900px){
  .frame{grid-template-columns:minmax(0,1fr);padding-inline:16px;gap:16px}
  .rail{position:static;max-height:none}
  .rail ol{display:flex;flex-wrap:wrap;gap:4px}
  .rail a{background:var(--surface);border:1px solid var(--line)}
  .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
  .case-body.has-media{grid-template-columns:minmax(0,1fr)}
  .facts{grid-template-columns:minmax(0,1fr);gap:2px 0} .facts dd{margin-bottom:10px}
}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""

JS = """
(() => {
  const groups = {problems: s => !['survived', 'unreachable'].includes(s), fixed: s => s === 'fixed',
                  survived: s => s === 'survived', open: s => ['declined', 'unfixed', 'found'].includes(s)};
  document.querySelectorAll('.filter').forEach(btn => btn.addEventListener('click', () => {
    document.querySelectorAll('.filter').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
    const keep = groups[btn.dataset.filter] || (() => true);
    document.querySelectorAll('[data-status]').forEach(el => {
      el.hidden = btn.dataset.filter === 'problems' ? el.dataset.broke !== 'true' : !keep(el.dataset.status);
    });
  }));
  document.querySelectorAll('.play-both').forEach(btn => btn.addEventListener('click', () => {
    btn.closest('.screen').querySelectorAll('video').forEach(v => { v.currentTime = 0; v.play().catch(() => {}); });
  }));
  const box = document.querySelector('.lightbox'), big = box.querySelector('img');
  const close = () => { box.hidden = true; big.removeAttribute('src'); };
  document.querySelectorAll('.zoom').forEach(z => z.addEventListener('click', () => {
    big.src = z.dataset.src; big.alt = z.getAttribute('aria-label') || ''; box.hidden = false; box.querySelector('.close').focus();
  }));
  box.addEventListener('click', e => { if (e.target !== big) close(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !box.hidden) close(); });
})();
"""
