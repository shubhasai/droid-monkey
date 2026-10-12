"""ux-audit.html: the UX audit as one page a developer or designer can read top to bottom.

Headline numbers, the top issues, then every finding with its screen (switchable between the screenshot and the
predicted-attention heatmap), what it rests on (screenshot, measurement, prediction, code), the change and the
files. Then the user journeys with the measured wait on every step, the slow steps against the response-time
limits, the attention gallery (filterable), the suspicions from code kept apart, and how every number was made.
Same visual language as the attack dashboard (dashboard.py); images are referenced next to the file.
"""

import html
import re
import time
from pathlib import Path

from . import attention as att_mod
from . import paths

esc = html.escape
SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
BAND_TONE = {"instant": "ok", "noticeable": "ok", "breaks flow": "warn", "loses attention": "bad", "not measured": "idle"}


def basis(evidence: str) -> str:
    """What one evidence line rests on: code, predicted attention, a measurement or the screenshot."""
    t = evidence.lower()
    if t.startswith("code") or re.search(r"\.(kt|java|xml):\d", t):
        return "code"
    if "predicted" in t or "attention" in t:
        return "predicted"
    if re.search(r"\d+(\.\d+)?\s?(s|ms|dp|taps?|x\d+)\b", t) or "measured" in t:
        return "measured"
    return "screenshot"


BASIS_LABEL = {"screenshot": "seen on screen", "measured": "measured", "predicted": "predicted attention",
               "code": "from code"}


EMBED_WIDTH = 480   # px: sharp enough to read a phone screenshot, ~30-50 KB each as JPEG
_cache: dict[Path, str] = {}   # path -> "#img<n>": each image is embedded once, every use points at it
_data: dict[str, str] = {}     # "#img<n>" -> data URI


def _img(base: Path, rel: str) -> str:
    """A reference to the image, embedded once as a JPEG data URI (see _data), so the report is one file that can
    be moved or shared. The page's script swaps the reference for the data when it loads."""
    path = base / rel
    if not path.exists():
        return ""
    if path not in _cache:
        import base64
        import io
        from PIL import Image
        img = Image.open(path).convert("RGB")
        if img.width > EMBED_WIDTH:
            img = img.resize((EMBED_WIDTH, round(img.height * EMBED_WIDTH / img.width)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72, optimize=True)
        key = f"#img{len(_data)}"
        _data[key] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        _cache[path] = key
    return _cache[path]


def _viewer(base: Path, sid: str, title: str = "") -> str:
    shot, heat = _img(base, f"screens/{sid}.png"), _img(base, f"attention/{sid}.jpg")
    if not shot and not heat:
        return f'<div class="noshot">no screenshot of {esc(sid)}</div>'
    tabs = ""
    if shot and heat:
        tabs = ('<div class="seg" role="group" aria-label="View"><button type="button" class="on" data-show="shot">'
                'Screen</button><button type="button" data-show="heat">Attention</button></div>')
    imgs = (f'<img class="shot" src="{esc(shot)}" alt="{esc(sid)} screenshot" loading="lazy">' if shot else "") + (
        f'<img class="heat"{" hidden" if shot else ""} src="{esc(heat)}" alt="{esc(sid)} predicted attention" '
        f'loading="lazy">' if heat else "")
    return (f'<figure class="viewer">{tabs}<div class="phone">{imgs}</div>'
            f'<figcaption><code>{esc(sid)}</code>{" · " + esc(title) if title else ""}</figcaption></figure>')


def _finding(f, base: Path, titles: dict, n: int) -> str:
    kinds = sorted({basis(e) for e in f.evidence}, key=list(BASIS_LABEL).index)
    chips = "".join(f'<span class="chip b-{k}">{BASIS_LABEL[k]}</span>' for k in kinds)
    ev = "".join(f'<li><span class="dot b-{basis(e)}" title="{BASIS_LABEL[basis(e)]}"></span>{esc(e)}</li>'
                 for e in f.evidence)
    first = f.screens[0] if f.screens else None
    shown = [s for s in f.screens[:4] if _img(base, f"screens/{s}.png") or _img(base, f"attention/{s}.jpg")]
    more = "".join(f'<button type="button" class="thumb{" on" if i == 0 else ""}" title="{esc(s)}" '
                   f'data-shot="{esc(_img(base, f"screens/{s}.png"))}" data-heat="{esc(_img(base, f"attention/{s}.jpg"))}" '
                   f'data-cap="{esc(s)}{" · " + esc(titles.get(s, "")) if titles.get(s) else ""}">'
                   f'<img src="{esc(_img(base, f"screens/{s}.png") or _img(base, f"attention/{s}.jpg"))}" '
                   f'alt="{esc(s)}" loading="lazy"></button>' for i, s in enumerate(shown)) if len(shown) > 1 else ""
    status = {"applied": "ok", "declined": "idle", "failed": "bad"}.get(f.status, "")
    after = ""
    if f.after:
        before_src = _img(Path(f.before).parent, Path(f.before).name) if f.before else ""
        after_src = _img(Path(f.after).parent, Path(f.after).name)
        after = (f'<div class="ba"><figure><img src="{esc(before_src)}" alt="before"><figcaption>before</figcaption>'
                 f'</figure><figure><img src="{esc(after_src)}" alt="after"><figcaption>after</figcaption></figure></div>')
    return f"""<article class="finding sev-{esc(f.severity)}" id="f-{esc(f.id)}" data-sev="{esc(f.severity)}">
  <div class="f-text">
    <p class="f-num">{n:02d}</p>
    <h3>{esc(f.title)}</h3>
    <p class="badges"><span class="sev">{esc(f.severity)}</span><span class="kind">{esc(f.kind)}</span>
      {f'<span class="flow">flow: {esc(f.flow)}</span>' if f.flow else ''}
      {f'<span class="status s-{status}">{esc(f.status)}</span>' if f.status != "proposed" else ''}</p>
    <p class="problem">{esc(f.problem)}</p>
    <div class="rests"><span class="label">Rests on</span>{chips}</div>
    <ul class="evidence">{ev}</ul>
    <p class="why"><span class="label">Why it matters</span>{esc(f.why)}</p>
    <div class="change"><span class="label">Suggested change</span><p>{esc(f.change)}</p>
      {('<p class="files">' + ' '.join(f'<code>{esc(p)}</code>' for p in f.files) + '</p>') if f.files else ''}</div>
    {f'<p class="note">{esc(f.note)}</p>' if f.note else ''}
    {f'<p class="commit">commit <code>{esc(f.commit[:8])}</code></p>' if f.commit else ''}
  </div>
  <div class="f-media">{_viewer(base, first, titles.get(first, "")) if first else ''}
    {f'<div class="thumbs">{more}</div>' if more else ''}{after}</div>
</article>"""


def _journeys(waits: dict, base: Path, titles: dict) -> str:
    routes = waits.get("routes") or {}
    out = []
    for flow, info in (waits.get("per_flow") or {}).items():
        target = max(info.get("screens", []), key=lambda s: len((routes.get(s) or {}).get("steps", [])), default=None)
        r = routes.get(target)
        if not r:
            continue
        cells, prev = [], r["steps"][0]["from_screen"] if r["steps"] else target
        cold = r.get("cold_start_s")
        cells.append(f'<div class="node start"><span class="launch">launch</span>'
                     + (f'<span class="t {"warn" if cold and cold >= 1 else "ok"}">{cold:.1f}s</span>' if cold else "")
                     + '</div>')
        for st in r["steps"]:
            tone = BAND_TONE.get(st["band"], "ok")
            cells.append(f'<div class="edge {tone}"><span class="lbl" title="{esc(st["label"])}">{esc(st["label"][:28])}'
                         f'</span><span class="t {tone}" title="{esc(st["band"])}">'
                         + (f'{st["seconds"]:.1f}s' if st["seconds"] >= 0 else "–") + '</span></div>')
            img = _img(base, f"screens/{st['to_screen']}.png")
            cells.append(f'<div class="node"><button type="button" class="jshot" data-src="{esc(img)}">'
                         + (f'<img src="{esc(img)}" alt="{esc(st["to_screen"])}" loading="lazy">' if img else "")
                         + f'</button><code>{esc(st["to_screen"])}</code></div>')
        first = f'<div class="node"><code>{esc(prev)}</code></div>' if not r["steps"] else ""
        total = r.get("wait_s", 0) + (cold or 0)
        out.append(f'<div class="journey"><div class="j-head"><h3>{esc(flow)}</h3><p>{len(r["steps"])} taps · '
                   f'{r.get("wait_s", 0):.1f}s of waiting{f" + {cold:.1f}s cold start" if cold else ""} = '
                   f'<b>{total:.1f}s</b> to <code>{esc(target)}</code></p></div>'
                   f'<div class="strip">{first}{"".join(cells)}</div></div>')
    return "".join(out) or '<p class="hint">No flow reached a mapped screen.</p>'


def _wait_chart(waits: dict) -> str:
    rows = waits.get("slow_list") or []
    if not rows:
        return '<p class="hint">No step took 1 s or more.</p>'
    top = max(3.0, max(r["seconds"] for r in rows) * 1.1)
    out = []
    for r in rows:
        tone = BAND_TONE.get(r["band"], "warn")
        where = f'{r["from_screen"]} → {r["to_screen"]}' if r["to_screen"] != r["from_screen"] else r["from_screen"]
        out.append(f'<div class="bar-row"><div class="bar-label"><b>{esc(r["label"][:44])}</b><code>{esc(where)}</code>'
                   f'</div><div class="bar-track"><div class="bar {tone}" style="width:{100 * r["seconds"] / top:.1f}%">'
                   f'</div><span class="limit" style="left:{100 / top:.2f}%" title="1 s: the limit for keeping the '
                   f'user\'s flow"></span></div><span class="bar-val {tone}">{r["seconds"]:.1f}s</span></div>')
    return (f'<div class="bars">{"".join(out)}</div><p class="axis">The dashed line is 1 s, the limit for keeping '
            f'the user\'s flow of thought. 0.1 s feels instant; 10 s loses their attention.</p>')


def _gallery(attention: dict, base: Path, titles: dict) -> str:
    cards = []
    for sid, a in attention.items():
        fl = att_mod.flags(a)
        top = a.elements[:5]
        peak = max((e.share for e in top), default=0) or 1
        bars = "".join(f'<li class="{"prim" if e.label == a.primary else ""}"><span class="c-name">{esc(e.label[:30])}'
                       f'</span><span class="c-bar"><i style="width:{100 * e.share / peak:.0f}%"></i></span>'
                       f'<span class="c-val">{e.share:.0%}</span></li>' for e in top)
        cards.append(f"""<figure class="acard" data-flag="{1 if fl else 0}" data-prim="{1 if a.primary else 0}">
  <button type="button" class="aimg" data-src="{esc(_img(base, f'attention/{sid}.jpg'))}">
    <img src="{esc(_img(base, f'attention/{sid}.jpg'))}" alt="{esc(sid)} predicted attention" loading="lazy"></button>
  <figcaption><code>{esc(sid)}</code>
    {f'<p class="prim-line">primary action <b>{esc(a.primary)}</b> · #{a.primary_rank} · {a.primary_share:.0%}</p>' if a.primary else '<p class="prim-line muted">no primary action detected</p>'}
    {''.join(f'<p class="aflag">{esc(x)}</p>' for x in fl)}
    <ul class="controls">{bars}</ul></figcaption></figure>""")
    return "".join(cards)


def render(app_dir: Path, package: str, findings: list, usage: dict, attention: dict, waits: dict) -> str:
    import json
    _cache.clear()
    _data.clear()
    page = _page(app_dir, package, findings, usage, attention, waits)
    page = page.replace(' src="#img', ' data-img="#img').replace(' data-src="#img', ' data-img-src="#img')
    return page.replace("/*IMAGES*/", "const IMG=" + json.dumps(_data) + ";")


def _page(app_dir: Path, package: str, findings: list, usage: dict, attention: dict, waits: dict) -> str:
    base = paths.data_dir(app_dir)
    titles = waits.get("titles") or {}
    seen = sorted([f for f in findings if f.observed], key=lambda f: SEV_ORDER.get(f.severity, 3))
    suspected = [f for f in findings if not f.observed]
    sev = {k: sum(1 for f in seen if f.severity == k) for k in ("high", "medium", "low")}
    flagged = sum(1 for a in attention.values() if att_mod.flags(a))
    slow = waits.get("slow_list") or []
    cold = waits.get("cold_start_s")
    stats = [
        ("Screens audited", str(len(attention) or len(titles)), "every mapped screen, with a screenshot"),
        ("Findings", str(len(seen)), f"{sev['high']} high · {sev['medium']} medium · {sev['low']} low"),
        ("Attention flags", str(flagged), "primary action easy to miss"),
        ("Steps over 1 s", str(len(slow)), "measured, tap to settled screen"),
        ("Cold start", f"{cold:.2f} s" if cold else "–", "median, slow_startup attack"),
        ("Review cost", f"${(usage.get('cost_usd') or 0):.2f}", esc(str(usage.get("model") or ""))),
    ]
    stat_html = "".join(f'<div class="stat"><span class="s-label">{a}</span><span class="s-num">{b}</span>'
                        f'<span class="s-sub">{c}</span></div>' for a, b, c in stats)
    top = "".join(f'<li><a href="#f-{esc(f.id)}"><span class="sev sev-{esc(f.severity)}">{esc(f.severity)}</span>'
                  f'{esc(f.title)}</a></li>' for f in seen[:3])
    cards = "".join(_finding(f, base, titles, i + 1) for i, f in enumerate(seen))
    sus = "".join(_finding(f, base, titles, i + 1) for i, f in enumerate(suspected))
    rail = "".join(f'<li><a href="#{h}">{t}</a></li>' for h, t in [
        ("findings", f"Findings ({len(seen)})"), ("journeys", "Journeys"), ("waiting", "Waiting time"),
        ("attention", f"Attention ({len(attention)})")] + ([("suspected", f"Suspected ({len(suspected)})")] if suspected
                                                           else []) + [("method", "How this was made")])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>UX audit · {esc(package)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style></head><body>
<div class="frame">
<nav class="rail" aria-label="Sections"><p class="rail-title">UX audit</p><ol>{rail}</ol></nav>
<main>
<header class="top"><div>
  <p class="eyebrow">droid-monkey UX audit</p><h1>{esc(package)}</h1>
  <p class="meta">{time.strftime('%d %b %Y, %H:%M')} · review by {esc(str(usage.get('model')))} ·
    {usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out tokens</p></div></header>
<section class="stats">{stat_html}</section>
{f'<section class="panel topissues"><h2>Fix these first</h2><ol>{top}</ol></section>' if top else ''}

<section id="findings" class="block"><div class="block-head"><h2>Findings</h2>
  <div class="filters" role="group" aria-label="Filter findings">
    <button type="button" class="on" data-sev="all">All {len(seen)}</button>
    {''.join(f'<button type="button" data-sev="{k}">{k.title()} {v}</button>' for k, v in sev.items() if v)}</div></div>
  <p class="hint">Each finding rests on a screen the review saw or a measurement. Switch a screen between its screenshot
  and its predicted attention.</p>
  {cards or '<p class="hint">No findings.</p>'}</section>

<section id="journeys" class="block"><div class="block-head"><h2>User journeys</h2></div>
  <p class="hint">The fewest taps from launch to each flow's deepest mapped screen, with the time every step took to
  settle on the device.</p>{_journeys(waits, base, titles)}</section>

<section id="waiting" class="block"><div class="block-head"><h2>Waiting time</h2></div>
  {_wait_chart(waits)}
  <p class="bench">{esc(waits.get('benchmark', ''))}</p></section>

<section id="attention" class="block"><div class="block-head"><h2>Predicted attention</h2>
  <div class="filters" role="group" aria-label="Filter screens">
    <button type="button" class="on" data-att="all">All {len(attention)}</button>
    <button type="button" data-att="flag">Flagged {flagged}</button>
    <button type="button" data-att="prim">With a primary action {sum(1 for a in attention.values() if a.primary)}</button></div></div>
  <p class="hint">Where eyes are likely to go first: red is most, blue least; the white box is the screen's likely
  primary action. Shares are of the whole screen's predicted attention.</p>
  <div class="agrid">{_gallery(attention, base, titles)}</div></section>

{f'''<section id="suspected" class="block"><details><summary><h2>Suspected in code, not observed ({len(suspected)})</h2>
  <span class="hint">Seen in the code only, on screens nobody reached. An attack has to reproduce them before they are
  findings.</span></summary>{sus}</details></section>''' if suspected else ''}

<section id="method" class="block method"><h2>How this was made</h2><dl>
  <dt>Screens</dt><dd>Every screen droid-monkey's explorer mapped on the running app, with the screenshot and UI dump
  taken there.</dd>
  <dt>Measured</dt><dd>Touch targets in dp (Android's 48 dp minimum), labels a screen reader would read, taps from
  launch, and the time from each tap until the screen stopped changing (including up to ~0.3 s of polling).</dd>
  <dt>Predicted attention</dt><dd>Spectral-residual image salience (Hou &amp; Zhang, 2007) blended with the UI
  structure: element size, contrast with its surroundings, and the top-left reading bias. A prediction, not eye
  tracking: compare controls within a screen rather than reading the numbers as fact.</dd>
  <dt>Limits and benchmark</dt><dd>0.1 s / 1 s / 10 s response-time limits (Nielsen, 1993). Conversion impact is an
  industry benchmark, not a forecast for this app: real numbers need its funnel analytics.</dd>
  <dt>Review</dt><dd>A vision model read every screenshot, the measurements, the predictions, the user flows and the
  code. Findings it could not tie to a screen it saw are listed apart as suspected.</dd></dl></section>
<footer class="foot">Generated by droid-monkey. Images are embedded, so this file can be shared on its own; full-size
screenshots and heatmaps are in <code>.droid-monkey/screens</code> and <code>.droid-monkey/attention</code>.</footer>
</main></div>
<div class="lightbox" hidden><button type="button" class="close" aria-label="Close">×</button><img alt=""></div>
<script>{JS}</script></body></html>"""


CSS = """
:root{
  --bg:#f3f5f7; --surface:#ffffff; --sunken:#e9edf1; --fg:#17202b; --muted:#5b6776; --line:#d5dce3;
  --accent:#1f5f8b; --ok:#1c7a4a; --bad:#b4342b; --warn:#a96a12; --idle:#8a96a3;
  --ok-soft:#e1f2e8; --bad-soft:#f8e3e1; --warn-soft:#f6ecdb; --acc-soft:#e1ecf5;
  --b-screen:#1f5f8b; --b-measured:#1c7a4a; --b-predicted:#7a4fa8; --b-code:#8a96a3;
  --display:"IBM Plex Sans Condensed","Arial Narrow",system-ui,sans-serif;
  --body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0f141a; --surface:#161d25; --sunken:#1d2630; --fg:#e4e9ef; --muted:#93a0af; --line:#2a3541;
  --accent:#7db4dc; --ok:#5cc98d; --bad:#f08379; --warn:#e3ad55; --idle:#6f7d8c;
  --ok-soft:#173326; --bad-soft:#3a1d1b; --warn-soft:#3a2c14; --acc-soft:#16283a;
  --b-screen:#7db4dc; --b-measured:#5cc98d; --b-predicted:#b996e0; --b-code:#6f7d8c; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0f141a; --surface:#161d25; --sunken:#1d2630; --fg:#e4e9ef; --muted:#93a0af; --line:#2a3541;
  --accent:#7db4dc; --ok:#5cc98d; --bad:#f08379; --warn:#e3ad55; --idle:#6f7d8c;
  --ok-soft:#173326; --bad-soft:#3a1d1b; --warn-soft:#3a2c14; --acc-soft:#16283a;
  --b-screen:#7db4dc; --b-measured:#5cc98d; --b-predicted:#b996e0; --b-code:#6f7d8c; color-scheme:dark}
*{box-sizing:border-box} [hidden]{display:none!important}
html{scroll-behavior:smooth;scroll-padding-top:16px}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 var(--body);-webkit-font-smoothing:antialiased}
a{color:var(--accent)} code{font:.86em var(--mono);overflow-wrap:anywhere}
h1,h2,h3{font-family:var(--display);text-wrap:balance;margin:0;line-height:1.2}
h2{font-size:22px} h3{font-size:19px}
.frame{display:grid;grid-template-columns:190px minmax(0,1fr);gap:28px;max-width:1280px;margin:0 auto;padding-inline:24px;padding-block:28px 56px}
.rail{position:sticky;top:calc(env(safe-area-inset-top,0px) + 20px);align-self:start}
.rail-title{font:600 11px var(--body);letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:4px 0 8px}
.rail ol{list-style:none;margin:0;padding:0;display:grid;gap:2px}
.rail a{display:block;padding:6px 8px;border-radius:6px;color:var(--fg);text-decoration:none;font-size:14px}
.rail a:hover{background:var(--sunken)}
main{min-width:0;display:grid;gap:22px}
.eyebrow{font:600 12px var(--body);letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin:0 0 4px}
h1{font-size:clamp(24px,4vw,34px);font-weight:700;overflow-wrap:anywhere}
.meta,.hint,.axis,.bench{color:var(--muted);font-size:13px;margin:4px 0 0}
.stats{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:grid;gap:2px;min-width:0}
.s-label{font:600 11px var(--body);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.s-num{font:700 28px/1.1 var(--display);font-variant-numeric:tabular-nums}
.s-sub{font-size:12px;color:var(--muted)}
.panel,.block{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px 20px;min-width:0}
.topissues ol{margin:10px 0 0;padding-left:20px;display:grid;gap:6px}
.topissues a{color:var(--fg);text-decoration:none;font-weight:500} .topissues a:hover{color:var(--accent)}
.block-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.filters{display:flex;gap:6px;flex-wrap:wrap}
.filters button,.seg button{font:500 13px var(--body);border:1px solid var(--line);background:var(--surface);color:var(--fg);border-radius:999px;padding:4px 12px;cursor:pointer}
.filters button.on,.seg button.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.sev{display:inline-block;font:600 11px var(--body);letter-spacing:.06em;text-transform:uppercase;padding:2px 8px;border-radius:999px;margin-right:8px;background:var(--sunken);color:var(--muted)}
.sev-high .sev,.sev.sev-high{background:var(--bad-soft);color:var(--bad)}
.sev-medium .sev,.sev.sev-medium{background:var(--warn-soft);color:var(--warn)}
.finding{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:24px;border-top:1px solid var(--line);padding-top:20px;margin-top:20px;border-left:3px solid var(--idle);padding-left:18px}
.finding.sev-high{border-left-color:var(--bad)} .finding.sev-medium{border-left-color:var(--warn)}
.f-num{font:600 12px var(--mono);color:var(--muted);margin:0}
.f-text{min-width:0;display:grid;gap:10px;align-content:start}
.badges{margin:0;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.kind,.flow,.status{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:1px 8px}
.status.s-ok{color:var(--ok);border-color:var(--ok)} .status.s-bad{color:var(--bad);border-color:var(--bad)}
.problem{margin:0;font-size:16px}
.label{display:block;font:600 11px var(--body);letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-bottom:2px}
.rests{display:flex;flex-wrap:wrap;gap:6px;align-items:center} .rests .label{margin:0 4px 0 0;display:inline}
.chip{font-size:12px;border-radius:999px;padding:0 9px;border:1px solid currentColor;background:var(--surface)}
.chip.b-screenshot{color:var(--b-screen)} .chip.b-measured{color:var(--b-measured)} .chip.b-predicted{color:var(--b-predicted)} .chip.b-code{color:var(--b-code)}
.dot.b-screenshot{background:var(--b-screen)} .dot.b-measured{background:var(--b-measured)} .dot.b-predicted{background:var(--b-predicted)} .dot.b-code{background:var(--b-code)}
.evidence{margin:0;padding:0;list-style:none;display:grid;gap:6px;font-size:14px}
.evidence li{display:grid;grid-template-columns:10px minmax(0,1fr);gap:8px;align-items:baseline;overflow-wrap:anywhere}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;transform:translateY(-1px)}
.why{margin:0} .change{background:var(--acc-soft);border-radius:8px;padding:10px 12px} .change p{margin:0}
.files{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px!important}
.files code{background:var(--surface);border:1px solid var(--line);border-radius:4px;padding:1px 6px;font-size:12px}
.note{color:var(--muted);font-size:13px;font-style:italic;margin:0}
.f-media{display:grid;gap:10px;align-content:start}
.viewer{margin:0;display:grid;gap:8px}
.seg{display:flex;gap:4px}
.phone{border:1px solid var(--line);border-radius:14px;overflow:hidden;background:var(--sunken);cursor:zoom-in;max-height:620px}
.phone img{display:block;width:100%;height:auto}
.viewer figcaption{font-size:12px;color:var(--muted)}
.thumbs{display:flex;gap:6px} .thumb{border:2px solid var(--line);border-radius:6px;padding:0;background:none;cursor:pointer;width:58px;overflow:hidden}
.thumb.on{border-color:var(--accent)} .t.idle{color:var(--muted)}
.thumb img{display:block;width:100%}
.noshot{border:1px dashed var(--line);border-radius:12px;padding:40px 12px;text-align:center;color:var(--muted);font-size:13px}
.ba{display:grid;grid-template-columns:1fr 1fr;gap:6px} .ba figure{margin:0} .ba img{width:100%;border-radius:6px}
.ba figcaption{font-size:11px;color:var(--muted);text-align:center}
.journey{border-top:1px solid var(--line);padding-top:14px;margin-top:14px}
.j-head{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:baseline}
.j-head p{margin:0;font-size:13px;color:var(--muted)} .j-head b{color:var(--fg)}
.strip{display:flex;align-items:center;gap:6px;overflow-x:auto;padding:12px 2px 6px}
.node{flex:none;display:grid;gap:4px;justify-items:center;width:92px}
.node code{font-size:10.5px;color:var(--muted);text-align:center;max-width:92px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.node.start{width:auto;padding:0 6px}
.launch{font:600 11px var(--body);text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.jshot{padding:0;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--sunken);cursor:zoom-in;width:92px;height:184px}
.jshot img{width:100%;display:block}
.edge{flex:none;display:grid;justify-items:center;gap:2px;width:96px;position:relative;padding-top:4px}
.edge::before{content:"";position:absolute;top:0;left:4px;right:4px;border-top:2px solid var(--line)}
.edge.warn::before{border-color:var(--warn)} .edge.bad::before{border-color:var(--bad)}
.lbl{font-size:11px;color:var(--muted);text-align:center;max-width:96px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.t{font:600 12px var(--mono);font-variant-numeric:tabular-nums} .t.ok,.bar-val.ok{color:var(--ok)} .t.warn,.bar-val.warn{color:var(--warn)} .t.bad,.bar-val.bad{color:var(--bad)}
.bars{display:grid;gap:10px;margin-top:12px}
.bar-row{display:grid;grid-template-columns:minmax(0,260px) minmax(0,1fr) 52px;gap:12px;align-items:center}
.bar-label{display:grid;min-width:0;font-size:13px} .bar-label b{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-label code{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-track{position:relative;height:14px;background:var(--sunken);border-radius:4px}
.bar{height:100%;border-radius:4px;background:var(--warn)} .bar.bad{background:var(--bad)} .bar.ok{background:var(--ok)}
.limit{position:absolute;top:-4px;bottom:-4px;border-left:2px dashed var(--fg);opacity:.55}
.bar-val{font:600 13px var(--mono);text-align:right;font-variant-numeric:tabular-nums}
.agrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;margin-top:14px}
.acard{margin:0;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--bg);display:grid;align-content:start}
.aimg{padding:0;border:0;background:none;cursor:zoom-in;display:block;max-height:420px;overflow:hidden}
.aimg img{width:100%;display:block}
.acard figcaption{padding:10px 12px;display:grid;gap:6px;font-size:13px}
.prim-line{margin:0} .muted{color:var(--muted)}
.aflag{margin:0;color:var(--bad);font-size:12px}
.controls{list-style:none;margin:0;padding:0;display:grid;gap:3px}
.controls li{display:grid;grid-template-columns:minmax(0,1fr) 60px 34px;gap:6px;align-items:center;font-size:12px}
.c-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.c-bar{height:6px;background:var(--sunken);border-radius:3px;overflow:hidden} .c-bar i{display:block;height:100%;background:var(--accent)}
.controls li.prim .c-name{font-weight:600} .controls li.prim .c-bar i{background:var(--bad)}
.c-val{text-align:right;font:12px var(--mono);font-variant-numeric:tabular-nums}
details summary{cursor:pointer;list-style:none;display:grid;gap:4px} details summary::-webkit-details-marker{display:none}
details summary h2::before{content:"▸ ";color:var(--muted)} details[open] summary h2::before{content:"▾ "}
.method dl{display:grid;grid-template-columns:180px minmax(0,1fr);gap:8px 16px;margin:12px 0 0;font-size:14px}
.method dt{font-weight:600} .method dd{margin:0;color:var(--muted)}
.foot{color:var(--muted);font-size:12px}
.lightbox{position:fixed;inset:0;background:rgba(10,14,18,.86);display:grid;place-items:center;z-index:9;padding:24px}
.lightbox img{max-width:100%;max-height:92vh;border-radius:12px}
.lightbox .close{position:absolute;top:calc(env(safe-area-inset-top,0px) + 12px);right:16px;font-size:30px;color:#fff;background:none;border:0;cursor:pointer}
@media (max-width:1000px){.stats{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:860px){.frame{grid-template-columns:minmax(0,1fr)}.rail{position:static}.rail ol{display:flex;flex-wrap:wrap;gap:4px}
  .finding{grid-template-columns:minmax(0,1fr);padding-left:12px} .f-media{width:100%;max-width:340px;min-width:0}
  .bar-row{grid-template-columns:minmax(0,1fr) 48px}.bar-track{grid-column:1/-1;order:3}.method dl{grid-template-columns:minmax(0,1fr)}}
@media (max-width:520px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.frame{padding-inline:16px}
  .panel,.block{padding:14px 12px}}
"""

JS = """
/*IMAGES*/
document.querySelectorAll('img[data-img]').forEach(i=>{i.src=IMG[i.dataset.img]});
const box=document.querySelector('.lightbox'),boxImg=box.querySelector('img');
function zoom(src){if(!src)return;boxImg.src=src;box.hidden=false}
box.addEventListener('click',e=>{if(e.target!==boxImg)box.hidden=true});
document.addEventListener('keydown',e=>{if(e.key==='Escape')box.hidden=true});
document.querySelectorAll('.viewer').forEach(v=>{
  v.querySelectorAll('.seg button').forEach(b=>b.addEventListener('click',()=>{
    v.querySelectorAll('.seg button').forEach(x=>x.classList.toggle('on',x===b));
    v.querySelector('.shot').hidden=b.dataset.show!=='shot';v.querySelector('.heat').hidden=b.dataset.show!=='heat'}));
  v.querySelector('.phone').addEventListener('click',()=>{const i=[...v.querySelectorAll('.phone img')].find(x=>!x.hidden);zoom(i&&i.src)})});
document.querySelectorAll('.thumbs').forEach(t=>t.querySelectorAll('.thumb').forEach(b=>b.addEventListener('click',()=>{
  const v=t.closest('.f-media').querySelector('.viewer'),shot=v.querySelector('.shot'),heat=v.querySelector('.heat');
  if(shot&&b.dataset.shot)shot.src=IMG[b.dataset.shot]||b.dataset.shot;if(heat&&b.dataset.heat)heat.src=IMG[b.dataset.heat]||b.dataset.heat;
  v.querySelector('figcaption').innerHTML='<code>'+b.dataset.cap+'</code>';
  t.querySelectorAll('.thumb').forEach(x=>x.classList.toggle('on',x===b))})));
document.querySelectorAll('.jshot img,.aimg img,.ba img').forEach(i=>i.addEventListener('click',e=>{e.stopPropagation();zoom(i.src)}));
function filt(group,attr,cards,match){document.querySelectorAll(group+' button').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll(group+' button').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll(cards).forEach(c=>c.hidden=!match(c,b.dataset[attr]))}))}
filt('#findings .filters','sev','#findings .finding',(c,v)=>v==='all'||c.dataset.sev===v);
filt('#attention .filters','att','.acard',(c,v)=>v==='all'||(v==='flag'&&c.dataset.flag==='1')||(v==='prim'&&c.dataset.prim==='1'));
"""
