"""`droid-monkey dashboard`: a local, live dashboard over everything in the app's .droid-monkey folder.

Every page is rendered from the folder when it is opened (nothing is copied or cached), and the page asks the
server every few seconds whether anything in the folder changed; when it did (a run wrote a verdict, explore saved a
screen, the UX audit finished), the page refreshes itself. So the dashboard is always in sync with the folder:
runs and their evidence, the screen map and screenshots, the analysis, the user flows, the saved attacks and the
UX audit. It binds to 127.0.0.1 only and serves files from inside the folder only.
"""

import hashlib
import html
import json
import mimetypes
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import yaml

from . import paths

esc = html.escape
POLL_MS = 2500


# ---- reading the folder -------------------------------------------------------------------------------

def _json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def version(root: Path) -> str:
    """A fingerprint of every file's path, size and modification time: changes whenever anything in the folder
    changes."""
    h = hashlib.sha1()
    for p in sorted(root.rglob("*")):
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_file() and not p.name.endswith(("-wal", "-shm", "-journal")):
            h.update(f"{p.relative_to(root)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:16]


RUN_KINDS = {"attack": "attack", "run": "run", "flows": "flow attacks", "explore": "explore", "ux": "UX",
             "visuals": "run"}


def _kind(run_id: str) -> str:
    return RUN_KINDS.get(run_id.split("-", 1)[0], "run")


def _when(run_id: str, path: Path) -> str:
    m = re.search(r"(\d{8})-(\d{6})", run_id)
    if m:
        d, t = m.groups()
        return f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))


def _attack_dirs(run: Path) -> list[Path]:
    return sorted([d for d in run.iterdir() if d.is_dir() and (d / "verdict.json").exists()], key=lambda d: d.name)


def run_info(run: Path) -> dict:
    """What a run did, from whatever it wrote: summary.json (a full run), results.json (attacks), or the verdicts."""
    info = {"id": run.name, "kind": _kind(run.name), "when": _when(run.name, run), "attacks": 0, "broke": 0,
            "fixed": 0, "outcomes": [], "cost": None, "live": False, "dashboard": (run / "dashboard.html").exists()}
    summary = _json(run / "summary.json")
    if summary:
        outs = summary.get("outcomes", [])
        info.update(attacks=len(outs), fixed=sum(1 for o in outs if o.get("status") == "fixed"),
                    broke=sum(1 for o in outs if o.get("before") not in ("survived", None)),
                    outcomes=[(o.get("attack"), o.get("status")) for o in outs])
        from .report import receipt_data
        r = receipt_data(summary)
        info["cost"] = round(r["reasoning_cost"] + r["diag_cost"] + r["coder_cost"], 2)
    else:
        verdicts = [(_json(d / "verdict.json") or {}) for d in _attack_dirs(run)]
        results = _json(run / "results.json") or verdicts
        info.update(attacks=len(results), broke=sum(1 for v in results if v.get("outcome") not in ("survived", None)),
                    outcomes=[(v.get("attack"), v.get("outcome")) for v in results])
    newest = max((p.stat().st_mtime for p in run.rglob("*") if p.is_file()), default=run.stat().st_mtime)
    info["live"] = time.time() - newest < 90 and not summary and not (run / "results.json").exists()
    return info


def runs(root: Path) -> list[dict]:
    d = root / "runs"
    return sorted((run_info(r) for r in d.iterdir() if r.is_dir()), key=lambda i: i["when"], reverse=True) \
        if d.exists() else []


# ---- a small markdown renderer (ANALYSIS.md, UX_AUDIT.md, receipts) --------------------------------------------

def _inline(t: str) -> str:
    t = esc(t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![*\w])\*([^*]+)\*(?!\w)|(?<!\w)_([^_]+)_(?!\w)", lambda m: f"<i>{m.group(1) or m.group(2)}</i>", t)
    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', t)


def markdown(text: str) -> str:
    out, para, lst, code, table = [], [], None, None, []

    def flush():
        nonlocal para, lst, table
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para = []
        if lst:
            out.append(f"<{lst[0]}>" + "".join(f"<li>{_inline(i)}</li>" for i in lst[1]) + f"</{lst[0]}>")
            lst = None
        if table:
            rows = [r for r in table if not re.fullmatch(r"\|?[\s:|-]+\|?", r)]
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            if cells:
                out.append('<div class="scroll"><table><thead><tr>' + "".join(f"<th>{_inline(c)}</th>" for c in cells[0])
                           + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r)
                                                              + "</tr>" for r in cells[1:]) + "</tbody></table></div>")
            table = []

    for line in text.splitlines():
        if code is not None:
            if line.startswith("```"):
                out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.startswith("```"):
            flush()
            code = []
        elif m := re.match(r"(#{1,4})\s+(.*)", line):
            flush()
            n = min(len(m.group(1)) + 1, 4)
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
        elif m := re.match(r"\s*([-*]|\d+\.)\s+(.*)", line):
            if para or table:
                flush()
            kind = "ol" if m.group(1)[0].isdigit() else "ul"
            if lst and lst[0] != kind:
                flush()
            lst = lst or (kind, [])
            lst[1].append(m.group(2))
        elif line.strip().startswith("|"):
            if para or lst:
                flush()
            table.append(line.strip())
        elif not line.strip():
            flush()
        elif lst and line.startswith("  "):
            lst[1][-1] += " " + line.strip()
        else:
            if lst or table:
                flush()
            para.append(line.strip())
    flush()
    return "\n".join(out)


# ---- pages ---------------------------------------------------------------------------------------------

def _f(rel: str) -> str:
    return "/files/" + quote(rel)


def _t(rel: str) -> str:
    """A resized copy for grids and strips; the full image is one click away."""
    return "/thumb/" + quote(rel)


_thumbs: dict[tuple, bytes] = {}


def thumbnail(path: Path, width: int = 360) -> bytes:
    key = (str(path), path.stat().st_mtime_ns, width)
    if key not in _thumbs:
        import io
        from PIL import Image
        img = Image.open(path).convert("RGB")
        if img.width > width:
            img = img.resize((width, round(img.height * width / img.width)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        _thumbs[key] = buf.getvalue()
    return _thumbs[key]


def _chip(outcome: str | None) -> str:
    tone = {"survived": "ok", "fixed": "ok", "skipped": "idle", "declined": "idle", "unreachable": "idle",
            "harness_error": "idle"}.get(outcome or "", "bad")
    return f'<span class="chip c-{tone}">{esc(str(outcome or "?").replace("_", " "))}</span>'


def _stat(label: str, num, sub: str = "", href: str = "") -> str:
    inner = f'<span class="s-label">{esc(label)}</span><span class="s-num">{esc(str(num))}</span>' \
            f'<span class="s-sub">{esc(sub)}</span>'
    return f'<a class="stat" href="{href}">{inner}</a>' if href else f'<div class="stat">{inner}</div>'


def _runs_table(rs: list[dict]) -> str:
    rows = []
    for r in rs:
        counts = f"{r['attacks']} attacks · {r['broke']} broke" + (f" · {r['fixed']} fixed" if r["fixed"] else "")
        chips = "".join(_chip(o) for _, o in r["outcomes"][:8]) + ("…" if len(r["outcomes"]) > 8 else "")
        live = '<span class="livebadge">running</span>' if r["live"] else ""
        rows.append(f'<tr><td><a href="/run/{quote(r["id"])}"><code>{esc(r["id"])}</code></a>{live}</td>'
                    f'<td>{esc(r["kind"])}</td><td class="num">{esc(r["when"])}</td><td>{counts}</td>'
                    f'<td class="chips">{chips}</td><td class="num">'
                    f'{"$%.2f" % r["cost"] if r["cost"] else "–"}</td></tr>')
    return ('<div class="scroll"><table><thead><tr><th>Run</th><th>Kind</th><th class="num">When</th><th>What happened</th>'
            '<th>Outcomes</th><th class="num">Cost</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
            ) if rows else '<p class="hint">No runs yet. Try <code>droid-monkey attack</code>.</p>'


def page_overview(root: Path, app: Path) -> str:
    cfg = yaml.safe_load((app / "monkey.yaml").read_text()) if (app / "monkey.yaml").exists() else {}
    smap = _json(root / "screen-map.json") or _json(root / "screen-map.partial.json") or {}
    checklist = _json(root / "screens.json") or []
    flows = _json(root / "flows.json") or []
    flows = flows.get("flows", flows) if isinstance(flows, dict) else flows
    rs = runs(root)
    ux = _json(root / "ux-audit.json") or {}
    prof = _json(root / "profile.json") or {}
    analysis_cost = (_json(root / "analysis-run.json") or {}).get("cost_usd")
    broke = sum(r["broke"] for r in rs)
    seen = [f for f in ux.get("findings", []) if f.get("observed", True)]
    stats = "".join([
        _stat("Runs", len(rs), f"{sum(1 for r in rs if r['live'])} running now", "/runs"),
        _stat("Screens mapped", len(smap.get("screens", {})), f"of {len(checklist)} in the code" if checklist else "",
              "/screens"),
        _stat("Things that broke", broke, "across all runs", "/runs"),
        _stat("User flows", len(flows), f"{sum(len(f.get('risks', [])) for f in flows)} risks", "/flows"),
        _stat("UX findings", len(seen) if ux else "–", "from the last audit" if ux else "run droid-monkey ux", "/ux"),
        _stat("Risk signals", len(prof.get("signals", [])) if prof else "–",
              f"analysis ${analysis_cost:.2f}" if analysis_cost else "", "/analysis"),
    ])
    first = ""
    if (root / "ANALYSIS.md").exists():
        m = re.search(r"##[^\n]*what the app does[^\n]*\n+(.+?)(\n#|\Z)", (root / "ANALYSIS.md").read_text(), re.S | re.I)
        first = f'<section class="panel"><h2>What the app does</h2>{markdown(m.group(1).strip()) if m else ""}' \
                f'<p><a href="/analysis">Full analysis →</a></p></section>' if m else ""
    shots = sorted((root / "screens").glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[:8] \
        if (root / "screens").exists() else []
    strip = "".join(f'<a class="mini" href="/screens#{esc(p.stem)}"><img src="{_t("screens/" + p.name)}" alt="{esc(p.stem)}" '
                    f'loading="lazy"><code>{esc(p.stem)}</code></a>' for p in shots)
    return f"""<header class="top"><p class="eyebrow">droid-monkey · live</p><h1>{esc(cfg.get('package', app.name))}</h1>
<p class="meta">{esc(str(app))} · variant {esc(str(cfg.get('variant', '?')))} · module {esc(str(cfg.get('module', 'app')))}</p></header>
<section class="stats">{stats}</section>
<section class="panel"><div class="panel-head"><h2>Latest runs</h2><a href="/runs">All runs →</a></div>{_runs_table(rs[:6])}</section>
{first}
{f'<section class="panel"><div class="panel-head"><h2>Recently mapped screens</h2><a href="/screens">All screens →</a></div><div class="ministrip">{strip}</div></section>' if strip else ''}"""


def page_runs(root: Path, app: Path) -> str:
    rs = runs(root)
    return f'<header class="top"><h1>Runs</h1><p class="meta">{len(rs)} runs in <code>.droid-monkey/runs</code>, ' \
           f'newest first.</p></header><section class="panel">{_runs_table(rs)}</section>'


def _evidence_card(run: Path, d: Path, root: Path) -> str:
    v = _json(d / "verdict.json") or {}
    rel = d.relative_to(root).as_posix()
    imgs = "".join(f'<figure><img src="{_t(rel + "/" + n)}" data-full="{_f(rel + "/" + n)}" alt="{n}" loading="lazy"><figcaption>{n[:-4]}</figcaption></figure>'
                   for n in ("before.png", "fault.png", "after.png") if (d / n).exists())
    video = f'<video controls preload="none" src="{_f(rel + "/recording.mp4")}"></video>' if (d / "recording.mp4").exists() else ""
    files = " ".join(f'<a href="{_f(rel + "/" + p.name)}"><code>{esc(p.name)}</code></a>'
                     for p in sorted(d.iterdir()) if p.is_file())
    notes = "".join(f"<li>{esc(str(n))[:300]}</li>" for n in (v.get("notes") or [])[:8])
    return f"""<article class="ev"><div class="ev-head"><h3>{esc(d.name)}</h3>{_chip(v.get('outcome'))}
<span class="hint">{esc(str(v.get('screen', '')))} · {v.get('seconds', '?')} s</span></div>
<p>{esc(str(v.get('exception') or v.get('reason') or ''))}</p>
{f'<ul class="notes">{notes}</ul>' if notes else ''}<div class="shots">{imgs}{video}</div><p class="files">{files}</p></article>"""


def page_run(root: Path, app: Path, run_id: str) -> str:
    run = root / "runs" / run_id
    if not run.is_dir():
        return '<h1>Not found</h1>'
    info = run_info(run)
    if (run / "summary.json").exists() and not (run / "dashboard.html").exists():
        try:  # a full run that never wrote its visual report: build it now, from the same data
            from . import report as report_mod
            cfg = yaml.safe_load((app / "monkey.yaml").read_text())
            report_mod.dashboard(_json(run / "summary.json"), cfg["package"], run, run / "dashboard.html", app)
        except Exception:
            pass
    head = (f'<header class="top"><p class="eyebrow"><a href="/runs">Runs</a> / {esc(info["kind"])}</p>'
            f'<h1><code>{esc(run_id)}</code></h1><p class="meta">{esc(info["when"])} · {info["attacks"]} attacks · '
            f'{info["broke"]} broke' + (f' · {info["fixed"]} fixed' if info["fixed"] else "")
            + (' · <span class="livebadge">running</span>' if info["live"] else "") + '</p></header>')
    extras = "".join(f'<a class="btn" href="{_f(f"runs/{run_id}/{n}")}">{n}</a>'
                     for n in ("receipt.md", "log.txt", "summary.json", "results.json") if (run / n).exists())
    if (run / "dashboard.html").exists():
        return head + (f'<p class="actions">{extras}<a class="btn" href="{_f(f"runs/{run_id}/dashboard.html")}" '
                       f'target="_blank">Open full screen</a></p>' if True else "") \
            + f'<iframe class="report" src="{_f(f"runs/{run_id}/dashboard.html")}" title="run report"></iframe>'
    cards = "".join(_evidence_card(run, d, root) for d in _attack_dirs(run))
    rest = [d for d in sorted(run.iterdir()) if d.is_dir() and not (d / "verdict.json").exists()]
    partial = "".join(f'<li><code>{esc(d.name)}</code>: no verdict yet ({len(list(d.iterdir()))} files)</li>' for d in rest)
    return head + (f'<p class="actions">{extras}</p>' if extras else "") + (cards or "") + (
        f'<section class="panel"><h2>Without a verdict</h2><ul>{partial}</ul></section>' if partial else "")


def page_screens(root: Path, app: Path) -> str:
    smap = _json(root / "screen-map.json") or _json(root / "screen-map.partial.json") or {"screens": {}}
    checklist = _json(root / "screens.json") or []
    screens = smap.get("screens", {})
    unreached = {u.get("screen"): u.get("reason") for u in smap.get("unreached", [])}
    cards = []
    for sid, s in screens.items():
        shot = root / "screens" / f"{sid}.png"
        heat = root / "attention" / f"{sid}.jpg"
        tabs = ('<div class="seg"><button type="button" class="on" data-show="shot">Screen</button>'
                '<button type="button" data-show="heat">Attention</button></div>') if shot.exists() and heat.exists() else ""
        img = (f'<img class="shot" src="{_t("screens/" + shot.name)}" data-full="{_f("screens/" + shot.name)}" alt="{esc(sid)}" loading="lazy">' if shot.exists() else
               '<div class="noshot">no screenshot</div>') + (
            f'<img class="heat" hidden src="{_t("attention/" + heat.name)}" data-full="{_f("attention/" + heat.name)}" alt="{esc(sid)} attention" loading="lazy">'
            if heat.exists() else "")
        acts = "".join(f'<li>{esc(a.get("label", "")[:40])} <span class="hint">{esc(a.get("effect", ""))}'
                       f'{" → " + esc(a["to_screen"]) if a.get("to_screen") else ""}'
                       f'{" · %.1fs" % a["settle_seconds"] if a.get("settle_seconds") else ""}</span></li>'
                       for a in s.get("actions", [])[:8])
        broken = unreached.get(sid)
        cards.append(f"""<figure class="scard" id="{esc(sid)}">{tabs}<div class="sshot">{img}</div><figcaption>
<b>{esc(s.get('title', sid))}</b><code>{esc(sid)}</code>{f'<span class="chip c-bad">path broken</span>' if broken else ''}
<p class="hint">{esc(s.get('description', '')[:160])}</p>
<p class="hint">{len(s.get('path', []))} steps from launch{' · ' + esc(s.get('activity', '')) if s.get('activity') else ''}</p>
{f'<ul class="acts">{acts}</ul>' if acts else ''}
{('<p class="files">' + ' '.join(f'<code>{esc(x.rsplit("/", 1)[-1])}</code>' for x in s.get('files', [])[:4]) + '</p>') if s.get('files') else ''}
</figcaption></figure>""")
    reached = {s.get("title", "").lower() for s in screens.values()} | {k.lower() for k in screens}
    miss = [c for c in checklist if c.get("kind") in ("activity", "screen") and not c.get("note")
            and c.get("name", "").lower() not in reached]
    return f"""<header class="top"><h1>Screens</h1><p class="meta">{len(screens)} mapped
{f'· {len(checklist)} in the code checklist' if checklist else ''} · mode {esc(str(smap.get('mode', '?')))}
· explored in {smap.get('seconds', 0):.0f}s</p></header>
<div class="sgrid">{''.join(cards) or '<p class="hint">No screens yet: run <code>droid-monkey explore</code>.</p>'}</div>
{f'<section class="panel"><h2>Not reached</h2><ul class="cols">' + ''.join(f'<li><code>{esc(u)}</code> <span class="hint">{esc(str(r)[:120])}</span></li>' for u, r in unreached.items()) + '</ul></section>' if unreached else ''}
{f'<details class="panel"><summary><h2>In the code, not mapped ({len(miss)})</h2></summary><ul class="cols">' + ''.join(f'<li><code>{esc(c["name"])}</code> <span class="hint">{esc(c.get("kind", ""))}</span></li>' for c in miss) + '</ul></details>' if miss else ''}"""


def page_analysis(root: Path, app: Path) -> str:
    md = root / "ANALYSIS.md"
    prof = _json(root / "profile.json") or {}
    run = _json(root / "analysis-run.json") or {}
    kinds: dict[str, list] = {}
    for s in prof.get("signals", []):
        kinds.setdefault(s["kind"], []).append(s)
    sig = "".join(f'<details><summary><b>{esc(k)}</b> <span class="hint">{len(v)}</span></summary><ul class="cols">'
                  + "".join(f'<li><code>{esc(x["file"].rsplit("/", 1)[-1])}:{x["line"]}</code> '
                            f'<span class="hint">{esc(x["why"][:90])}</span></li>' for x in v) + "</ul></details>"
                  for k, v in sorted(kinds.items(), key=lambda kv: -len(kv[1])))
    meta = (f'{run.get("seconds", 0):.0f}s · ${run.get("cost_usd", 0):.2f} · {esc(str(run.get("model", "")))}'
            if run else "")
    return f"""<header class="top"><h1>Analysis</h1><p class="meta">{meta}</p></header>
<section class="panel prose">{markdown(md.read_text()) if md.exists() else '<p class="hint">No analysis yet: run <code>droid-monkey analyze</code>.</p>'}</section>
{f'<section class="panel"><h2>Static risk signals ({len(prof.get("signals", []))})</h2><p class="hint">Generic code patterns, not confirmed bugs.</p>{sig}</section>' if sig else ''}"""


def page_flows(root: Path, app: Path) -> str:
    flows = _json(root / "flows.json") or []
    flows = flows.get("flows", flows) if isinstance(flows, dict) else flows
    specs = {}
    for f in sorted((root / "attacks").glob("*.yaml")) if (root / "attacks").exists() else []:
        try:
            d = yaml.safe_load(f.read_text()) or {}
            specs.setdefault(d.get("flow"), []).append(d.get("name", f.stem))
        except yaml.YAMLError:
            pass
    cards = []
    for f in flows:
        risks = "".join(f'<li><span class="chip c-idle">{esc(r.get("fault", "?"))}</span> {esc(r.get("why", ""))}'
                        f' <code>{esc(str(r.get("code", "")))}</code></li>' for r in f.get("risks", []))
        steps = "".join(f"<li>{esc(s)}</li>" for s in f.get("steps", []))
        cover = specs.get(f.get("name"), [])
        cards.append(f"""<article class="panel"><div class="panel-head"><h2>{esc(f.get('name', '?'))}</h2>
<span class="hint">{len(cover)} attack{'s' if len(cover) != 1 else ''}: {', '.join(f'<a href="/attacks#{esc(c)}">{esc(c)}</a>' for c in cover) or 'none yet'}</span></div>
<p>{esc(f.get('description', ''))}</p><div class="two"><div><h3>Steps</h3><ol>{steps}</ol></div><div><h3>Risks</h3><ul class="risks">{risks}</ul></div></div></article>""")
    return f'<header class="top"><h1>User flows</h1><p class="meta">{len(flows)} flows from the analysis</p></header>' \
           + ("".join(cards) or '<p class="hint">No flows yet.</p>')


def page_attacks(root: Path, app: Path) -> str:
    d = root / "attacks"
    items = []
    for f in sorted(d.glob("*.yaml")) if d.exists() else []:
        text = f.read_text()
        try:
            meta = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            meta = {}
        items.append(f'<article class="panel" id="{esc(f.stem)}"><div class="panel-head"><h2><code>{esc(f.stem)}</code></h2>'
                     f'<span class="hint">{esc(str(meta.get("fault_type", "")))} · flow {esc(str(meta.get("flow", "-")))}'
                     f' · screen {esc(str(meta.get("screen", "")))}</span></div><p>{esc(str(meta.get("description", "")))}</p>'
                     f'<pre><code>{esc(text)}</code></pre></article>')
    return f'<header class="top"><h1>Saved attacks</h1><p class="meta">{len(items)} specs in <code>.droid-monkey/attacks</code>' \
           f' (built-in attacks are in droid-monkey itself)</p></header>' + ("".join(items) or
                                                                              '<p class="hint">None yet: try <code>droid-monkey attacks from-flows</code>.</p>')


def page_ux(root: Path, app: Path) -> str:
    if not (root / "ux-audit.html").exists():
        return '<header class="top"><h1>UX audit</h1></header><p class="hint">No audit yet: run <code>droid-monkey ux</code>.</p>'
    return ('<header class="top"><h1>UX audit</h1><p class="actions"><a class="btn" href="/files/ux-audit.html" '
            'target="_blank">Open full screen</a><a class="btn" href="/files/UX_AUDIT.md">UX_AUDIT.md</a></p></header>'
            '<iframe class="report" src="/files/ux-audit.html" title="UX audit"></iframe>')


def page_files(root: Path, app: Path) -> str:
    rows = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        depth = rel.count("/")
        st = p.stat()
        rows.append(f'<tr><td style="padding-left:{8 + depth * 14}px"><a href="{_f(rel)}">{esc(rel)}</a></td>'
                    f'<td class="num">{st.st_size / 1024:,.0f} KB</td><td class="num">'
                    f'{time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime))}</td></tr>')
    return f'<header class="top"><h1>Files</h1><p class="meta">{len(rows)} files in <code>{esc(str(root))}</code></p></header>' \
           f'<section class="panel"><div class="scroll"><table><thead><tr><th>Path</th><th class="num">Size</th>' \
           f'<th class="num">Modified</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>'


PAGES = {"/": ("Overview", page_overview), "/runs": ("Runs", page_runs), "/screens": ("Screens", page_screens),
         "/analysis": ("Analysis", page_analysis), "/flows": ("Flows", page_flows),
         "/attacks": ("Attacks", page_attacks), "/ux": ("UX audit", page_ux), "/files": ("Files", page_files)}


def layout(path: str, body: str, title: str, ver: str, app: Path) -> str:
    nav = "".join(f'<li><a href="{p}"{" class=on" if (path == p or (p != "/" and path.startswith(p))) else ""}>{t}</a></li>'
                  for p, (t, _) in PAGES.items())
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>{esc(title)} · droid-monkey</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style></head><body><div class="frame">
<nav class="rail"><p class="brand">droid-monkey</p><p class="appname">{esc(app.name)}</p><ol>{nav}</ol>
<p class="sync" title="The page checks the .droid-monkey folder every {POLL_MS / 1000:g} s and refreshes when it changes">
<span class="pulse"></span><span class="sync-text">live · in sync</span></p></nav>
<main>{body}</main></div>
<div class="lightbox" hidden><button type="button" class="close" aria-label="Close">×</button><img alt=""></div>
<script>const VERSION={json.dumps(ver)};{JS}</script></body></html>"""


# ---- server -------------------------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    root: Path
    app: Path

    def log_message(self, *a):  # quiet: the terminal shows the URL, not every request
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/version":
                return self._send(200, json.dumps({"version": version(self.root)}).encode(), "application/json")
            if path.startswith("/files/"):
                return self._file(path[len("/files/"):])
            if path.startswith("/thumb/"):
                target = self._inside(path[len("/thumb/"):])
                if not target:
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, thumbnail(target), "image/jpeg")
            if path.startswith("/run/"):
                body, title = page_run(self.root, self.app, path[len("/run/"):].strip("/")), "Run"
            elif path in PAGES:
                title, fn = PAGES[path]
                body = fn(self.root, self.app)
            else:
                return self._send(404, b"not found", "text/plain")
            self._send(200, layout(path, body, title, version(self.root), self.app).encode(), "text/html; charset=utf-8")
        except Exception as e:  # a half-written file mid-run: show it, the next refresh will be fine
            self._send(500, f"<pre>{esc(repr(e))}</pre>".encode(), "text/html")

    def _inside(self, rel: str) -> Path | None:
        """The file at `rel` if it is inside the folder; None for anything else (../, absolute paths, links out)."""
        target = (self.root / rel).resolve()
        return target if target.is_relative_to(self.root.resolve()) and target.is_file() else None

    def _file(self, rel: str) -> None:
        target = self._inside(rel)
        if not target:
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(target.name)[0] or ("text/plain; charset=utf-8" if target.suffix in
                                                         (".md", ".txt", ".yaml", ".log", ".pbtxt") else "application/octet-stream")
        if target.suffix == ".md":
            body = layout("/files", f'<section class="panel prose">{markdown(target.read_text())}</section>',
                          target.name, version(self.root), self.app).encode()
            return self._send(200, body, "text/html; charset=utf-8")
        data = target.read_bytes()
        rng = self.headers.get("Range")  # videos seek with range requests
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            start = int(m.group(1) or 0)
            end = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            self.wfile.write(data[start:end + 1])
            return
        self._send(200, data, ctype, {"Accept-Ranges": "bytes"})


def serve(app_dir: Path, port: int = 8787, open_browser: bool = True) -> None:
    root = paths.data_dir(app_dir)
    handler = type("Handler", (_Handler,), {"root": root, "app": app_dir.resolve()})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"droid-monkey dashboard for {root.resolve()}\n  {url}  (live: it follows the folder; Ctrl+C to stop)", flush=True)
    if open_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


CSS = """
:root{--bg:#f3f5f7;--surface:#fff;--sunken:#e9edf1;--fg:#17202b;--muted:#5b6776;--line:#d5dce3;--accent:#1f5f8b;
  --ok:#1c7a4a;--bad:#b4342b;--warn:#a96a12;--idle:#8a96a3;--ok-soft:#e1f2e8;--bad-soft:#f8e3e1;
  --display:"IBM Plex Sans Condensed","Arial Narrow",system-ui,sans-serif;--body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f141a;--surface:#161d25;--sunken:#1d2630;--fg:#e4e9ef;
  --muted:#93a0af;--line:#2a3541;--accent:#7db4dc;--ok:#5cc98d;--bad:#f08379;--warn:#e3ad55;--idle:#6f7d8c;--ok-soft:#173326;--bad-soft:#3a1d1b;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0f141a;--surface:#161d25;--sunken:#1d2630;--fg:#e4e9ef;--muted:#93a0af;--line:#2a3541;--accent:#7db4dc;
  --ok:#5cc98d;--bad:#f08379;--warn:#e3ad55;--idle:#6f7d8c;--ok-soft:#173326;--bad-soft:#3a1d1b;color-scheme:dark}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 var(--body);-webkit-font-smoothing:antialiased}
a{color:var(--accent)} code{font:.86em var(--mono);overflow-wrap:anywhere}
h1,h2,h3{font-family:var(--display);margin:0;line-height:1.2;text-wrap:balance} h1{font-size:clamp(24px,4vw,32px)} h2{font-size:20px} h3{font-size:16px}
.frame{display:grid;grid-template-columns:200px minmax(0,1fr);gap:28px;max-width:1320px;margin:0 auto;padding-inline:24px;padding-block:24px 56px}
.rail{position:sticky;top:calc(env(safe-area-inset-top,0px) + 20px);align-self:start;display:grid;gap:6px}
.brand{font:700 18px var(--display);margin:0}.appname{margin:0 0 8px;color:var(--muted);font:12px var(--mono);overflow-wrap:anywhere}
.rail ol{list-style:none;margin:0;padding:0;display:grid;gap:2px}
.rail a{display:block;padding:7px 10px;border-radius:7px;color:var(--fg);text-decoration:none}
.rail a:hover{background:var(--sunken)}.rail a.on{background:var(--fg);color:var(--bg)}
.sync{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);margin-top:12px}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 0 var(--ok);animation:pulse 2.5s infinite}
.sync.stale .pulse{background:var(--warn);animation:none}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 60%,transparent)}70%{box-shadow:0 0 0 8px transparent}100%{box-shadow:0 0 0 0 transparent}}
main{min-width:0;display:grid;gap:18px;align-content:start}
.top{display:grid;gap:4px}.eyebrow{font:600 12px var(--body);letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin:0}
.eyebrow a{text-decoration:none}.meta,.hint{color:var(--muted);font-size:13px;margin:0}
.stats{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:grid;gap:2px;min-width:0;color:var(--fg);text-decoration:none}
a.stat:hover{border-color:var(--accent)}
.s-label{font:600 11px var(--body);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.s-num{font:700 28px/1.1 var(--display);font-variant-numeric:tabular-nums}.s-sub{font-size:12px;color:var(--muted)}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px 18px;min-width:0;display:grid;gap:10px}
.panel-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font:600 11px var(--body);letter-spacing:.06em;text-transform:uppercase;color:var(--muted);padding:8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid var(--line);vertical-align:top}td:first-child code{white-space:nowrap}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.chips{display:flex;flex-wrap:wrap;gap:4px}.chip{font-size:11.5px;border-radius:999px;padding:1px 8px;border:1px solid currentColor;white-space:nowrap}
.c-ok{color:var(--ok)}.c-bad{color:var(--bad)}.c-idle{color:var(--idle)}
.livebadge{margin-left:8px;font:600 11px var(--body);color:var(--ok);border:1px solid var(--ok);border-radius:999px;padding:0 7px;animation:blink 1.6s infinite}
@keyframes blink{50%{opacity:.4}}
.ministrip{display:flex;gap:10px;overflow-x:auto;padding-bottom:4px}.mini{flex:none;width:110px;display:grid;gap:4px;text-decoration:none;color:var(--muted)}
.mini img{width:110px;border-radius:8px;border:1px solid var(--line)}.mini code{font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.scard{margin:0;background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:grid;align-content:start}
.scard:target{outline:3px solid var(--accent)}
.scard .seg{display:flex;gap:4px;padding:8px 8px 0}.seg button{font:500 12px var(--body);border:1px solid var(--line);background:var(--surface);color:var(--fg);border-radius:999px;padding:2px 10px;cursor:pointer}
.seg button.on{background:var(--fg);color:var(--bg)}
.sshot{padding:8px;cursor:zoom-in}.sshot img{width:100%;border-radius:8px;display:block;max-height:440px;object-fit:cover;object-position:top}
.noshot{border:1px dashed var(--line);border-radius:8px;padding:40px 8px;text-align:center;color:var(--muted);font-size:12px}
.scard figcaption{padding:0 12px 12px;display:grid;gap:4px;font-size:13px}.scard figcaption code{color:var(--muted);font-size:11px}
.acts,.notes{margin:0;padding-left:16px;font-size:12.5px}.files{display:flex;flex-wrap:wrap;gap:6px;margin:0}
.files code,.files a code{font-size:11px;background:var(--sunken);border-radius:4px;padding:1px 6px}
.ev{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;display:grid;gap:8px}
.ev-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.ev p{margin:0}
.shots{display:flex;gap:10px;overflow-x:auto}.shots figure{margin:0;flex:none;width:150px}.shots img{width:150px;border-radius:8px;border:1px solid var(--line);cursor:zoom-in}
.shots figcaption{font-size:11px;color:var(--muted);text-align:center}.shots video{height:300px;border-radius:8px;background:#000}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 0}.btn{font-size:13px;border:1px solid var(--line);border-radius:7px;padding:4px 10px;text-decoration:none;background:var(--surface)}
iframe.report{width:100%;height:calc(100vh - 150px);min-height:600px;border:1px solid var(--line);border-radius:12px;background:var(--surface)}
.prose{max-width:860px}.prose h2{margin-top:10px}.prose h3{margin-top:6px}.prose p,.prose li{line-height:1.6}
pre{background:var(--sunken);border-radius:8px;padding:12px;overflow-x:auto;font-size:12.5px;margin:0}
.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}.two ol,.risks{margin:6px 0 0;padding-left:18px;display:grid;gap:4px;font-size:14px}
.risks{list-style:none;padding:0}.cols{columns:2 280px;font-size:13px;padding-left:16px}
details summary{cursor:pointer;list-style:none}details summary::-webkit-details-marker{display:none}details summary h2{display:inline}
.lightbox{position:fixed;inset:0;background:rgba(10,14,18,.86);display:grid;place-items:center;z-index:9;padding:24px}
.lightbox img{max-width:100%;max-height:92vh;border-radius:12px}.lightbox .close{position:absolute;top:12px;right:16px;font-size:30px;color:#fff;background:none;border:0;cursor:pointer}
@media (max-width:1050px){.stats{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:820px){.frame{grid-template-columns:minmax(0,1fr);padding-inline:16px}.rail{position:static}.rail ol{display:flex;flex-wrap:wrap}
  .two{grid-template-columns:minmax(0,1fr)}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
"""

JS = """
// Live sync: ask the server for the folder's fingerprint; when it changes, refresh (keeping the scroll position).
const sync=document.querySelector('.sync'),syncText=document.querySelector('.sync-text');
try{const y=sessionStorage.getItem('dm-scroll:'+location.pathname);if(y){scrollTo(0,+y);sessionStorage.removeItem('dm-scroll:'+location.pathname)}}catch(e){}
let seen=VERSION;
async function check(){
  try{const r=await fetch('/api/version',{cache:'no-store'});const v=(await r.json()).version;
    sync.classList.remove('stale');
    if(v!==VERSION&&v!==seen){seen=v;syncText.textContent='changing…';setTimeout(check,1500);return}  // still being written
    if(v!==VERSION){syncText.textContent='updated · refreshing';
      try{sessionStorage.setItem('dm-scroll:'+location.pathname,String(scrollY))}catch(e){}
      location.reload();return}
    syncText.textContent='live · in sync'}
  catch(e){sync.classList.add('stale');syncText.textContent='server stopped'}
  setTimeout(check,""" + str(POLL_MS) + """)}
setTimeout(check,""" + str(POLL_MS) + """);
const box=document.querySelector('.lightbox'),boxImg=box.querySelector('img');
box.addEventListener('click',e=>{if(e.target!==boxImg)box.hidden=true});
document.addEventListener('keydown',e=>{if(e.key==='Escape')box.hidden=true});
document.querySelectorAll('.sshot img,.shots img,.mini img').forEach(i=>i.addEventListener('click',e=>{
  if(i.closest('.mini'))return;e.preventDefault();boxImg.src=i.dataset.full||i.src;box.hidden=false}));
document.querySelectorAll('.scard').forEach(c=>c.querySelectorAll('.seg button').forEach(b=>b.addEventListener('click',()=>{
  c.querySelectorAll('.seg button').forEach(x=>x.classList.toggle('on',x===b));
  c.querySelector('.shot').hidden=b.dataset.show!=='shot';c.querySelector('.heat').hidden=b.dataset.show!=='heat'})));
"""
