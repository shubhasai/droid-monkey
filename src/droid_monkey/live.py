"""`droid-monkey dashboard`: a local, live dashboard over everything in the app's .droid-monkey folder.

Every page is rendered from the folder when it is opened (nothing is copied or cached), and the page asks the
server every few seconds whether anything in the folder changed; when it did (a run wrote a verdict, explore saved a
screen, the UX audit finished), the page refreshes itself. So the dashboard is always in sync with the folder:
runs and their evidence, the screen map and screenshots, the analysis, the user flows, the saved attacks and the
UX audit. It binds to 127.0.0.1 only and serves files from inside the folder only.

Commands: the Commands page runs any droid-monkey command (forms built from the CLI itself, see jobs.py) as a job
whose output streams into the page; prompts such as "Fix it?" are answered with buttons. Starting, answering or
stopping a job needs the per-server token embedded in the dashboard's pages, and requests from other origins are
refused, so no other web page can run commands on this machine.
"""

import hashlib
import hmac
import html
import secrets
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
        if p.is_file() and not p.name.endswith(("-wal", "-shm", "-journal")) and p.parent.name != "jobs":
            h.update(f"{p.relative_to(root)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:16]


RUN_KINDS = {"attack": "attack", "run": "run", "flows": "flow attacks", "explore": "explore", "ux": "UX", "hunt": "hunt",
             "visuals": "run", "fix": "fix"}


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
    hunted = _json(run / "issues.json")
    fixed = _json(run / "fix.json")
    if fixed:  # `fix ISSUE`: one hunt or UX issue, fixed or not
        info.update(fix=fixed, fixed=int(fixed.get("status") == "fixed"),
                    outcomes=[(fixed.get("title"), fixed.get("status"))])
        return info
    if not summary and not hunted and info["kind"] == "fix":  # still working (or stopped before it wrote fix.json)
        newest = max((p.stat().st_mtime for p in run.rglob("*") if p.is_file()), default=run.stat().st_mtime)
        info.update(fix={}, live=time.time() - newest < 600)
        return info
    if hunted and not summary:  # a hunt: issues in normal use, no attacks
        issues = hunted.get("issues", [])
        info.update(attacks=hunted.get("journeys", 0), broke=len(issues), hunt=True,
                    outcomes=[(x.get("title"), x.get("severity")) for x in issues],
                    cost=(hunted.get("usage") or {}).get("cost_usd") or None)
        return info
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
            "harness_error": "idle", "low": "idle", "medium": "warn",
            "unfixed": "warn", "no result": "idle"}.get(outcome or "", "bad")
    return f'<span class="chip c-{tone}">{esc(str(outcome or "?").replace("_", " "))}</span>'


def _stat(label: str, num, sub: str = "", href: str = "") -> str:
    inner = f'<span class="s-label">{esc(label)}</span><span class="s-num">{esc(str(num))}</span>' \
            f'<span class="s-sub">{esc(sub)}</span>'
    return f'<a class="stat" href="{href}">{inner}</a>' if href else f'<div class="stat">{inner}</div>'


def _runs_table(rs: list[dict]) -> str:
    rows = []
    for r in rs:
        counts = (("fix: " + esc(r["fix"].get("title", "")[:60]) if r["fix"] else "fix: in progress or stopped")
                  if "fix" in r else
                  f"{r['attacks']} journeys · {r['broke']} issues" if r.get("hunt") else
                  f"{r['attacks']} attacks · {r['broke']} broke" + (f" · {r['fixed']} fixed" if r["fixed"] else ""))
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
    if "fix" in info:
        return _fix_head(run_id, info) + _fix_body(run, root, info["fix"])
    if (run / "issues.json").exists():
        return head + _hunt_body(run, root)
    summary = _json(run / "summary.json") or {}
    pr = _pr_bar(run_id, summary["branch"], f'{info["fixed"]} verified fix{"es" if info["fixed"] != 1 else ""} '
                 f'(each replayed the identical attack and passed its regression test)') \
        if summary.get("branch") and info["fixed"] else ""
    if (run / "dashboard.html").exists():
        return head + pr + (f'<p class="actions">{extras}<a class="btn" href="{_f(f"runs/{run_id}/dashboard.html")}" '
                       f'target="_blank">Open full screen</a></p>' if True else "") \
            + f'<iframe class="report" src="{_f(f"runs/{run_id}/dashboard.html")}" title="run report"></iframe>'
    cards = "".join(_evidence_card(run, d, root) for d in _attack_dirs(run))
    rest = [d for d in sorted(run.iterdir()) if d.is_dir() and not (d / "verdict.json").exists()]
    partial = "".join(f'<li><code>{esc(d.name)}</code>: no verdict yet ({len(list(d.iterdir()))} files)</li>' for d in rest)
    return head + (f'<p class="actions">{extras}</p>' if extras else "") + (cards or "") + (
        f'<section class="panel"><h2>Without a verdict</h2><ul>{partial}</ul></section>' if partial else "")


def _pr_bar(run_id: str, branch: str, what: str) -> str:
    """The fix's branch and how to get it into the app: open a PR (asks to confirm), or merge it yourself."""
    return (f'<section class="panel prbar"><div class="panel-head"><h2>Branch</h2>'
            f'<button type="button" class="run prbtn" data-run="{esc(run_id)}">Open PR</button></div>'
            f'<p><code>{esc(branch)}</code> holds {what}. Your branch is unchanged until you merge it.</p>'
            f'<p class="hint">Open PR pushes this branch to GitHub and opens a pull request (it asks you to confirm). Or merge '
            f'it locally: <code>git merge {esc(branch)}</code></p></section>')


def _fix_head(run_id: str, info: dict) -> str:
    f = info["fix"]
    state = _chip(f["status"]) if f else ('<span class="livebadge">running</span>' if info["live"] else _chip("no result"))
    return (f'<header class="top"><p class="eyebrow"><a href="/runs">Runs</a> / fix</p><h1><code>{esc(run_id)}</code></h1>'
            f'<p class="meta">{esc(info["when"])} · {state}' + (f' · {f["seconds"]} s' if f.get("seconds") else "") + '</p></header>')


def _fix_body(run: Path, root: Path, f: dict) -> str:
    """`fix ISSUE`: the issue, what the coding agent changed, how it was checked, and the screen after the fix."""
    rel = run.relative_to(root).as_posix()
    after = run / "recheck" / "after.png"
    img = (f'<figure><img src="{_t(rel + "/recheck/after.png")}" data-full="{_f(rel + "/recheck/after.png")}" alt="after the fix">'
           f'<figcaption>after the fix, re-walked from launch</figcaption></figure>') if after.exists() else ""
    if not f:
        return (f'<section class="panel"><h2>No result yet</h2><p>The fix is still running, or it was stopped before it '
                f'finished. Its output is on the Commands page (recent jobs).</p></section><div class="shots">{img}</div>')
    steps = "".join(f"<li><code>{esc(s)}</code></li>" for s in f.get("steps", [])) or "<li>on launch</li>"
    rows = [("Issue", esc(f.get("title", "")) + f' <span class="hint">({esc(f.get("source", ""))}, id {esc(f.get("issue", ""))})</span>'),
            ("What happened", esc(f.get("detail", ""))), ("Screen", esc(f.get("screen") or "–")),
            ("Result", esc(f.get("message", "")))]
    if f.get("check"):
        rows.append(("Checked", esc(f["check"])))
    if f.get("commit"):
        rows.append(("Commit", f'<code>{esc(f["commit"][:8])}</code> on <code>{esc(f.get("branch", ""))}</code>'
                     + (f' · Entire checkpoint <code>{esc(f["checkpoint"])}</code>' if f.get("checkpoint") else "")))
    if f.get("files"):
        rows.append(("Files", " ".join(f"<code>{esc(x)}</code>" for x in f["files"])))
    table = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    note = f'<section class="panel"><h2>The coding agent</h2><p>{esc(f["agent_note"])}</p></section>' if f.get("agent_note") else ""
    pr = _pr_bar(run.name, f["branch"], "this fix, committed and checked on the device") if f.get("commit") else ""
    return (pr + f'<section class="panel"><div class="scroll"><table class="kv">{table}</table></div></section>{note}'
            f'<div class="two"><section class="panel"><h2>Steps from launch</h2><ol>{steps}</ol></section>'
            f'<div class="shots">{img}</div></div>')


def _hunt_body(run: Path, root: Path) -> str:
    """A hunt's issues: what, where, the steps from launch, and the screenshot of the step where it showed."""
    data = _json(run / "issues.json") or {}
    cards = []
    for x in data.get("issues", []):
        shots = [e for e in x.get("evidence", []) if e.endswith(".png")]
        quotes = [e for e in x.get("evidence", []) if not e.endswith((".png", ".xml", ".txt"))]
        logs = [e for e in x.get("evidence", []) if e.endswith(".txt")]
        rel = run.relative_to(root).as_posix()
        steps = "".join(f"<li><code>{esc(s)}</code></li>" for s in x.get("steps", [])) or "<li>on launch</li>"
        img = "".join(f'<figure><img src="{_t(rel + "/" + s)}" data-full="{_f(rel + "/" + s)}" alt="step" loading="lazy">'
                      f'<figcaption>step {x.get("step", "?")}</figcaption></figure>' for s in shots[:1])
        cards.append(f"""<article class="ev"><div class="ev-head"><h3>{esc(x.get('title', ''))}</h3>{_chip(x.get('severity'))}
<span class="hint">{esc(x.get('kind', ''))} · found by the {esc(x.get('detector', ''))}{' · screen ' + esc(x['screen']) if x.get('screen') else ''}</span></div>
<p>{esc(x.get('detail', ''))}</p>{('<ul class="notes">' + ''.join(f'<li><code>{esc(q)}</code></li>' for q in quotes[:6]) + '</ul>') if quotes else ''}
<div class="two"><div><h3>Steps from launch</h3><ol>{steps}</ol></div><div class="shots">{img}</div></div>
{('<p class="files">' + ' '.join(f'<a href="{_f(rel + "/" + l)}"><code>{esc(l.rsplit("/", 1)[-1])}</code></a>' for l in logs[:2]) + '</p>') if logs else ''}</article>""")
    notes = "".join(f"<li>{esc(n)}</li>" for n in data.get("notes", []))
    u = data.get("usage") or {}
    meta = (f'<p class="actions"><a class="btn" href="{_f(run.relative_to(root).as_posix() + "/HUNT.md")}">HUNT.md</a>'
            f'<span class="hint">{data.get("steps", 0)} steps on {data.get("journeys", 0)} journeys in '
            f'{data.get("seconds", 0):.0f}s, no fault injected'
            + (f' · model ${u.get("cost_usd", 0):.3f}, {u.get("dropped_unquoted", 0)} unquoted claims dropped'
               if u.get("input_tokens") else "") + '</span></p>')
    return meta + ("".join(cards) or '<section class="panel"><h2>No issues in normal use</h2></section>') + (
        f'<section class="panel"><h2>Notes</h2><ul>{notes}</ul></section>' if notes else "")


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


REQUIRES = {"../monkey.yaml": ("monkey.yaml", "init"), "screen-map.json": ("a screen map", "explore"),
            "flows.json": ("the user flows", "analyze")}
USES = {"device": ("Uses the emulator", "dev"), "model": ("Calls a model", "mod"),
        "code": ("Changes code, asks you first", "code"), "publishes": ("Publishes to GitHub", "pub")}
MODE_HELP = {"agent": "A coding agent reads the code and drives the app (recommended)",
             "fast": "No model: quick depth-first tapping", "manual": "You drive the app, droid-monkey records",
             "rules": "No model: taps everything", "claude": "Old name for agent"}


def _options(root: Path, cmd: str, param: str) -> list[tuple[str, str]] | None:
    """Values to pick from instead of typing, for options whose values droid-monkey knows."""
    if (cmd, param) == ("attack", "name"):
        from .specs import BUILTIN_DIR
        builtin = sorted(p.stem for p in BUILTIN_DIR.glob("*.yaml"))
        saved = sorted(p.stem for p in (root / "attacks").glob("*.yaml")) if (root / "attacks").exists() else []
        return ([("builtin", "All built-in attacks"), ("custom", "All of this app's saved attacks"), ("all", "Everything")]
                + [(n, f"Built-in: {n.replace('_', ' ')}") for n in builtin] + [(n, f"Saved: {n}") for n in saved])
    if (cmd, param) == ("attacks show", "name"):
        return [(p.stem, p.stem) for p in sorted((root / "attacks").glob("*.yaml"))] if (root / "attacks").exists() else []
    if param in ("run_id", "run_a", "run_b") and cmd in ("report", "pr", "compare"):
        rs = runs(root)
        if cmd == "report":
            rs = [r for r in rs if (root / "runs" / r["id"] / "summary.json").exists()]
        if cmd == "pr":  # a run or a fix that committed something
            rs = [r for r in rs if r["fixed"] and ((root / "runs" / r["id"] / "summary.json").exists() or r.get("fix"))]
        return [(r["id"], f'{r["id"]} · fix: {r["fix"].get("title", "")[:50]}' if r.get("fix") else
                 f'{r["id"]} · {r["attacks"]} attacks, {r["broke"]} broke, {r["fixed"]} fixed') for r in rs]
    return None


def _field(root: Path, cmd, p, label: str) -> str:
    fid = f"{cmd.path.replace(' ', '-')}-{p.name}"
    hint = f'<span class="fhelp">{esc(p.help)}</span>' if p.help and len(p.help) < 260 else ""
    flag = f'<code class="flagname">{esc(p.label if p.kind != "argument" else p.name.upper())}</code>'
    if p.kind == "flag":
        return (f'<label class="fflag"><input type="checkbox" name="{esc(p.name)}"{" checked" if p.default else ""}>'
                f'<span class="flabel">{esc(label)} {flag}</span>{hint}</label>')
    opts = _options(root, cmd.path, p.name)
    if p.name == "mode" and p.kind == "choice":
        opts = [(c, MODE_HELP.get(c, c)) for c in p.choices if c != "claude"]
    elif opts is None and p.kind == "choice":
        opts = [("", "Default")] + [(c, c) for c in p.choices]
    if opts is not None:
        default = p.default if p.default is not None else (opts[0][0] if p.required and opts else "")
        empty = "" if p.required or any(v == default for v, _ in opts) else '<option value="">Default</option>'
        control = (f'<select name="{esc(p.name)}" id="{fid}">{empty}' + "".join(
            f'<option value="{esc(v)}"{" selected" if v == default else ""}>{esc(t)}</option>' for v, t in opts)
            + "</select>") if opts else '<p class="hint">Nothing to choose yet.</p>'
    else:
        typ = "number" if p.kind in ("int", "float") else "text"
        ph = f' placeholder="default: {esc(str(p.default))}"' if p.default not in (None, "") else ' placeholder="default"'
        control = f'<input type="{typ}"{" step=any" if p.kind == "float" else ""} name="{esc(p.name)}" id="{fid}"{ph}{" required" if p.required else ""}>'
    return f'<label class="ffield" for="{fid}"><span class="flabel">{esc(label)}{" *" if p.required else ""} {flag}</span>{control}{hint}</label>'


def _missing(root: Path, g) -> list[tuple[str, str]]:
    return [REQUIRES.get(r, (r, "")) for r in g.requires if not (root / r).exists()]


def _next_step(root: Path) -> str:
    if not (root.parent / "monkey.yaml").exists():
        return "init"
    if not (root / "flows.json").exists():
        return "analyze"
    if not (root / "screen-map.json").exists():
        return "explore"
    return "hunt"


def page_commands(root: Path, app: Path, jobs=None) -> str:
    from . import jobs as jobs_mod
    cmds = jobs_mod.commands()
    busy = jobs.busy() if jobs else None
    history = jobs.history() if jobs else []
    last = {}
    for j in history:
        last.setdefault(j.command, j)
    titles = {g.path: g.title for g in jobs_mod.GUIDES}
    suggested = _next_step(root)
    nav, panels = [], []
    for stage in jobs_mod.STAGES:
        items = [g for g in jobs_mod.GUIDES if g.stage == stage and g.path in cmds]
        if not items:
            continue
        nav.append(f'<li class="stage">{esc(stage)}</li>')
        for g in items:
            c = cmds[g.path]
            miss = _missing(root, g)
            j = last.get(g.path)
            state = (f'needs {esc(miss[0][0])}' if miss else
                     f'last run {time.strftime("%H:%M", time.localtime(j.started))} · {j.status}' if j else "ready")
            tone = "miss" if miss else (j.status if j else "ready")
            slug = g.path.replace(" ", "-")
            nav.append(f'<li><a href="#{slug}" data-cmd="{slug}" class="navcmd{" suggested" if g.path == suggested else ""}">'
                       f'<span class="ntitle">{esc(g.title)}</span><span class="nstate s-{esc(tone)}">{state}</span></a></li>')
            byname = {p.name: p for p in c.params}
            primary = [byname[n] for n in g.primary if n in byname]
            rest = [p for p in c.params if p.name not in g.primary]
            label = lambda p: g.labels.get(p.name) or p.name.replace("_", " ").capitalize()
            uses = "".join(f'<span class="use u-{USES[u][1]}">{USES[u][0]}</span>' for u in g.uses)
            needs = "".join(f'<p class="need">Needs {esc(what)} first: run <a href="#{esc(by)}">{esc(titles.get(by, by))}</a>.</p>'
                            for what, by in miss)
            blocked = bool(miss) or bool(busy)
            why = ("Another command is running: one at a time, they share the emulator." if busy and not miss else "")
            panels.append(f"""<form class="cmdpanel" id="panel-{slug}" data-cmd="{esc(g.path)}" data-publishes="{1 if c.publishes else 0}" hidden>
<p class="eyebrow">{esc(g.stage)}</p><h2>{esc(g.title)}</h2>
<p class="what">{esc(g.what)}</p>
<div class="uses">{uses}<span class="use u-cost">{esc(g.cost)}</span></div>
{needs}
{'<div class="fields">' + ''.join(_field(root, c, p, label(p)) for p in primary) + '</div>' if primary else ''}
{f'<details class="more"><summary>More options ({len(rest)})</summary><div class="fields">' + ''.join(_field(root, c, p, label(p)) for p in rest) + '</div></details>' if rest else ''}
{'<p class="danger">This pushes the branch and opens a pull request on GitHub: you will be asked to confirm.</p>' if c.publishes else ''}
<div class="runbar"><button type="submit" class="run"{" disabled" if blocked else ""}>Run</button>
<code class="preview"></code></div>{f'<p class="hint">{why}</p>' if why else ''}
{f'<p class="hint">Last run: <a href="/job/{esc(j.id)}">{time.strftime("%d %b %H:%M", time.localtime(j.started))}</a> · {_jstatus(j.status)}</p>' if (j := last.get(g.path)) else ''}
</form>""")
    recent = "".join(f'<li><a href="/job/{esc(j.id)}"><span>{esc(titles.get(j.command, j.command))}</span>'
                     f'{_jstatus(j.status)}<span class="hint">{time.strftime("%H:%M", time.localtime(j.started))} · {_dur(j)}</span></a></li>'
                     for j in history[:8])
    running = (f'<a class="busy" href="/job/{esc(busy.id)}"><span class="spin"></span>Running: '
               f'<b>{esc(titles.get(busy.command, busy.command))}</b> · {esc(busy.status)} → open</a>') if busy else ""
    return f"""<header class="top"><h1>Commands</h1><p class="meta">Pick a step on the left. Defaults are fine: just press Run.
Suggested next: <a href="#{suggested.replace(' ', '-')}">{esc(titles.get(suggested, suggested))}</a>.</p>{running}</header>
<div class="cmdlayout"><nav class="cmdnav"><ol>{''.join(nav)}</ol></nav>
<div class="cmdmain">{''.join(panels)}
<section class="panel recent"><h2>Recent jobs</h2>{'<ul>' + recent + '</ul>' if recent else '<p class="hint">None yet.</p>'}</section></div></div>
<script>const SUGGESTED={json.dumps(suggested.replace(' ', '-'))};</script>"""


def _jstatus(status: str) -> str:
    tone = {"done": "ok", "running": "run", "waiting": "warn", "failed": "bad", "stopped": "idle"}.get(status, "idle")
    return f'<span class="chip c-{tone}">{esc(status)}</span>'


def _dur(j) -> str:
    sec = (j.ended or time.time()) - j.started
    return f"{sec / 60:.1f} min" if sec >= 60 else f"{sec:.0f} s"


def page_job(root: Path, app: Path, job_id: str, jobs=None) -> str:
    job = jobs.get(job_id) if jobs else None
    if not job:
        return "<h1>Not found</h1>"
    return f"""<header class="top"><p class="eyebrow"><a href="/commands">Commands</a> / job</p>
<h1><code>droid-monkey {esc(" ".join(job.args))}</code></h1>
<p class="meta">started {time.strftime("%H:%M:%S", time.localtime(job.started))} · <span id="jstatus">{_jstatus(job.status)}</span>
· <span id="jdur">{_dur(job)}</span></p></header>
<div class="ask" id="ask" hidden><p class="q" id="question"></p><div class="answers">
  <button type="button" data-a="y" class="yes">Yes</button><button type="button" data-a="n">No</button>
  <button type="button" data-a="a">All remaining</button><button type="button" data-a="">Enter</button>
  <input id="free" placeholder="or type an answer"><button type="button" id="send">Send</button></div></div>
<div class="console-bar"><label><input type="checkbox" id="follow" checked> follow output</label>
<button type="button" id="stop" class="stopbtn"{"" if job.status in ("running", "waiting") else " hidden"}>Stop</button></div>
<div class="joblayout"><pre class="console" id="console"></pre>""" + _phone() + f"""</div>
<script>const JOB={json.dumps(job.id)};const NO_RELOAD=true;</script>"""


def page_issues(root: Path, app: Path, jobs=None) -> str:
    """Every open issue, ranked (Jev when it has ranked them; severity otherwise), each with a Fix button."""
    from . import issues as issues_mod
    found = issues_mod.collect(root)
    cache = json.loads((root / "issues-rank.json").read_text()) if (root / "issues-rank.json").exists() else {}
    for x in found:
        r = cache.get(x.id)
        if r:
            x.score, x.rank_reason, x.ranked_by = r["score"], r["reason"], r["by"]
        else:
            x.score, x.rank_reason = issues_mod._fallback(x)
            x.ranked_by = "not ranked yet"
    found.sort(key=lambda x: (x.status == "fixed", -x.score))  # open issues first, most harmful first; then fixed
    new = sum(1 for x in found if x.ranked_by == "not ranked yet" and x.status != "fixed")
    open_n = sum(1 for x in found if x.status != "fixed")
    busy = jobs.busy() if jobs else None
    rows = []
    for n, x in enumerate(found, 1):
        tone = "bad" if x.score >= 0.6 else "warn" if x.score >= 0.4 else "idle"
        how = ("replays the identical attack after the fix" if x.source == "attack" else
               "re-walks the steps on the device after the fix" if x.source == "hunt" else "rebuilds and screenshots")
        ev = "".join(f"<li><code>{esc(e[:160])}</code></li>" for e in x.evidence[:3])
        if x.status == "fixed":
            fixed_line = (f'<p class="fixedon">{_chip("fixed")} on <code>{esc(x.branch)}</code>'
                          + (f' · commit <code>{esc(x.commit[:8])}</code>' if x.commit else "")
                          + f' · <a href="/run/{quote(x.fix_run)}">the fix</a>'
                          + (' · <b>a later run still saw it</b>: the fix is only on that branch until you merge it' if x.seen_after_fix else "")
                          + '</p>')
            action = (f'<button type="button" class="run prbtn" data-run="{esc(x.fix_run)}"{" disabled" if busy else ""}>Open PR</button>'
                      f'<span class="hint">pushes <code>{esc(x.branch)}</code> to GitHub and opens a pull request; asks you to confirm</span>')
        else:
            fixed_line = ""
            action = (f'<button type="button" class="run fixbtn" data-cmd="{esc(x.fix_args[0])}" data-arg="{esc(x.attack if x.source == "attack" else x.id)}"{" disabled" if busy else ""}>Fix</button>'
                      f'<span class="hint">asks you first · {how}</span>')
        rows.append(f"""<article class="issue{' isfixed' if x.status == 'fixed' else ''}" data-src="{esc(x.source)}" data-status="{esc(x.status)}">
<div class="rank"><span class="rn">{n}</span><span class="score s-{tone}">{x.score:.2f}</span></div>
<div class="ibody"><h3>{esc(x.title)}</h3>
<p class="imeta"><span class="chip c-idle">{esc(x.source)}</span> {esc(x.severity)}{' · screen ' + esc(x.screen) if x.screen else ''}
{' · <i>suspected in code</i>' if not x.observed else ''} · found {esc(x.found)} in <code>{esc(x.run)}</code></p>
<p class="why"><b>{'Jev' if x.ranked_by == 'jev' else 'Ranked'}:</b> {esc(x.rank_reason)}</p>
{fixed_line}<p>{esc(x.detail[:400])}</p>{f'<ul class="iev">{ev}</ul>' if ev else ''}</div>
<div class="ifix">{action}</div></article>""")
    return f"""<header class="top"><h1>Issues</h1><p class="meta">{open_n} open and {len(found) - open_n} fixed, from every attack, hunt and UX audit;
open ones first, most harmful first. Jev (TypeSafe) scores each one: expected impact on users × how likely it is a real bug.</p>
<p class="actions"><button type="button" class="btn rankbtn" data-all="0">{f'Rank {new} new with Jev' if new else 'All ranked'}</button>
<button type="button" class="btn rankbtn" data-all="1">Re-rank all</button><span class="hint rankmsg"></span></p>
{('<a class="busy" href="/job/' + esc(busy.id) + '">Running: ' + esc(busy.command) + ' → open</a>') if busy else ''}</header>
<div class="filters" role="group"><button type="button" class="on" data-f="all">All</button><button type="button" data-f="attack">Attacks</button>
<button type="button" data-f="hunt">Hunt</button><button type="button" data-f="ux">UX</button><button type="button" data-f="fixed">Fixed</button></div>
<div class="issues">{''.join(rows) or '<p class="hint">No open issues. Run Find bugs or Attack first.</p>'}</div>"""


# ---- the device, live ----------------------------------------------------------------------------------

SCREEN_GAP = 0.3  # seconds: several open tabs share one capture instead of each running screencap
_screen_lock = threading.Lock()
_screen: dict = {"at": 0.0, "jpg": None, "serial": None}


def device_frame(width: int = 460) -> tuple[bytes | None, str]:
    """The emulator's screen now, as a small JPEG; (None, why) when there is no device. A screencap is read-only:
    it does not touch the app or the run that is driving it."""
    import io
    from PIL import Image
    from .tools import emulator
    from .tools.adb import Adb, AdbError
    with _screen_lock:
        if _screen["jpg"] and time.time() - _screen["at"] < SCREEN_GAP:
            return _screen["jpg"], ""
        try:
            if not _screen["serial"]:
                _screen["serial"] = emulator.running_emulator()
            if not _screen["serial"]:
                return None, "no emulator is running"
            png = Adb(_screen["serial"]).run_bytes("exec-out", "screencap", "-p", timeout=8, retries=0)
            img = Image.open(io.BytesIO(png)).convert("RGB")
        except (AdbError, OSError, ValueError) as e:
            _screen["serial"] = None  # the emulator may have restarted: look again next time
            return None, f"could not read the screen: {str(e)[:160]}"
        if img.width > width:
            img = img.resize((width, round(img.height * width / img.width)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72)
        _screen.update(at=time.time(), jpg=buf.getvalue())
        return _screen["jpg"], ""


def activity_now(root: Path, jobs=None) -> dict:
    """What droid-monkey is doing: a dashboard job's latest output, or else (a run started in a terminal) the run
    folder written most recently and the newest files in it."""
    busy = jobs.busy() if jobs else None
    if busy:
        tail = "\n".join(jobs.log(busy.id).splitlines()[-40:])
        return {"kind": "job", "title": "droid-monkey " + " ".join(busy.args), "href": f"/job/{busy.id}",
                "status": busy.status, "tail": tail}
    d = root / "runs"
    run = max((r for r in d.iterdir() if r.is_dir()), key=lambda r: r.stat().st_mtime, default=None) if d.exists() else None
    if not run:
        return {"kind": "none"}
    files = sorted((p for p in run.rglob("*") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    age = time.time() - (files[0].stat().st_mtime if files else run.stat().st_mtime)
    return {"kind": "run", "title": run.name, "href": f"/run/{run.name}", "status": "writing" if age < 60 else "idle",
            "age": round(age), "tail": "\n".join(str(p.relative_to(run)) for p in files[:12])}


def page_device(root: Path, app: Path, jobs=None) -> str:
    return """<header class="top"><h1>Device</h1><p class="meta">The emulator's screen, live, next to what droid-monkey is
doing: a run you started here or in a terminal. Watching only reads the screen; it never taps or types.</p></header>
<div class="devlayout">""" + _phone() + """<section class="panel now"><div class="panel-head"><h2>Now</h2>
<span id="nowstatus"></span></div><p class="nowtitle"><a id="nowlink" href="#"></a></p>
<pre class="console nowtail" id="nowtail">Looking…</pre></section></div><script>const NO_RELOAD=true;const NOW_POLL=true;</script>"""


def _phone() -> str:
    return """<figure class="phone" data-live><div class="bezel"><img alt="The emulator's screen, live" hidden>
<p class="offline">Connecting to the emulator…</p></div>
<figcaption><span class="dot"></span><span class="fps">live</span><button type="button" class="pausebtn">Pause</button></figcaption></figure>"""


PAGES = {"/": ("Overview", page_overview), "/issues": ("Issues", page_issues), "/commands": ("Commands", page_commands), "/device": ("Device", page_device), "/runs": ("Runs", page_runs), "/screens": ("Screens", page_screens),
         "/analysis": ("Analysis", page_analysis), "/flows": ("Flows", page_flows),
         "/attacks": ("Attacks", page_attacks), "/ux": ("UX audit", page_ux), "/files": ("Files", page_files)}


def layout(path: str, body: str, title: str, ver: str, app: Path, token: str = "", busy=None) -> str:
    nav = "".join(f'<li><a href="{p}"{" class=on" if (path == p or (p != "/" and path.startswith(p))) else ""}>{t}</a></li>'
                  for p, (t, _) in PAGES.items())
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>{esc(title)} · droid-monkey</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style></head><body><div class="frame">
<nav class="rail"><p class="brand">droid-monkey</p><p class="appname">{esc(app.name)}</p><ol>{nav}</ol>
<p class="sync" title="The page checks the .droid-monkey folder every {POLL_MS / 1000:g} s and refreshes when it changes">
<span class="pulse"></span><span class="sync-text">live · in sync</span></p>
<a class="jobpill" href="{f'/job/{esc(busy.id)}' if busy else '#'}"{'' if busy else ' hidden'}><span class="spin"></span>
<span class="jp-cmd">{esc(busy.command) if busy else ''}</span><b class="jp-st">{esc(busy.status) if busy else ''}</b></a></nav>
<main>{body}</main></div>
<div class="lightbox" hidden><button type="button" class="close" aria-label="Close">×</button><img alt=""></div>
<script>const VERSION={json.dumps(ver)};const TOKEN={json.dumps(token)};{JS}{JOB_JS}</script></body></html>"""


# ---- server -------------------------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    root: Path
    app: Path
    jobs = None
    token: str = ""

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
                busy = self.jobs.busy() if self.jobs else None
                return self._send(200, json.dumps({"version": version(self.root), "job": busy.public() if busy else None}
                                                  ).encode(), "application/json")
            if path.startswith("/files/"):
                return self._file(path[len("/files/"):])
            if path.startswith("/thumb/"):
                target = self._inside(path[len("/thumb/"):])
                if not target:
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, thumbnail(target), "image/jpeg")
            if path == "/api/screen":
                if not self._token_ok():
                    return self._send(403, b"forbidden", "text/plain")
                jpg, why = device_frame()
                return self._send(200, jpg, "image/jpeg") if jpg else self._send(503, why.encode(), "text/plain")
            if path == "/api/now":
                return self._send(200, json.dumps(activity_now(self.root, self.jobs)).encode(), "application/json")
            if path.startswith("/api/jobs/"):
                return self._job_poll(path[len("/api/jobs/"):].strip("/"))
            if path.startswith("/run/"):
                body, title = page_run(self.root, self.app, path[len("/run/"):].strip("/")), "Run"
            elif path.startswith("/job/"):
                body, title = page_job(self.root, self.app, path[len("/job/"):].strip("/"), self.jobs), "Job"
            elif path == "/commands":
                body, title = page_commands(self.root, self.app, self.jobs), "Commands"
            elif path == "/device":
                body, title = page_device(self.root, self.app, self.jobs), "Device"
            elif path == "/issues":
                body, title = page_issues(self.root, self.app, self.jobs), "Issues"
            elif path in PAGES:
                title, fn = PAGES[path]
                body = fn(self.root, self.app)
            else:
                return self._send(404, b"not found", "text/plain")
            busy = self.jobs.busy() if self.jobs else None
            self._send(200, layout(path, body, title, version(self.root), self.app, self.token, busy).encode(),
                       "text/html; charset=utf-8")
        except Exception as e:  # a half-written file mid-run: show it, the next refresh will be fine
            self._send(500, f"<pre>{esc(repr(e))}</pre>".encode(), "text/html")

    # ---- commands -----------------------------------------------------------------------------------------
    def _token_ok(self) -> bool:
        """The screen is only for this dashboard's pages (an <img> carries the token in its URL) on our own host."""
        port = self.server.server_address[1]
        q = dict(x.split("=", 1) for x in (urlparse(self.path).query or "").split("&") if "=" in x)
        return (hmac.compare_digest(q.get("t", ""), self.token)
                and self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"})

    def _trusted(self) -> bool:
        """A request from this dashboard's own pages: the per-server token, from our own origin, to our own host
        name (which also defeats DNS rebinding)."""
        port = self.server.server_address[1]
        ours = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        return (hmac.compare_digest(self.headers.get("X-DM-Token", ""), self.token) and self.headers.get("Host") in ours
                and (origin is None or origin.split("://", 1)[-1] in ours))

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(min(n, 65536)) or b"{}")

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if not self.jobs or not self._trusted():
            return self._send(403, b'{"error": "forbidden"}', "application/json")
        try:
            body = self._json_body()
            if path == "/api/jobs":
                from . import jobs as jobs_mod
                cmd = jobs_mod.commands().get(body.get("command", ""))
                if not cmd:
                    raise ValueError("unknown command")
                if cmd.publishes and body.get("confirm") is not True:
                    raise ValueError("this command publishes; confirm it first")
                job = self.jobs.start(cmd.path, jobs_mod.argv(cmd, body.get("values") or {}))
                return self._send(200, json.dumps(job.public()).encode(), "application/json")
            if path == "/api/issues/rank":
                from . import issues as issues_mod
                if body.get("all"):
                    issues_mod.rerank(self.root)
                pkg = (yaml.safe_load((self.app / "monkey.yaml").read_text()) or {}).get("package", self.app.name)
                ranked, note = issues_mod.rank(self.root, issues_mod.collect(self.root), pkg)
                return self._send(200, json.dumps({"ranked": len(ranked), "note": note}).encode(), "application/json")
            m = re.fullmatch(r"/api/jobs/([\w.-]+)/(answer|stop)", path)
            if m:
                if m.group(2) == "answer":
                    self.jobs.answer(m.group(1), str(body.get("text", "")))
                else:
                    self.jobs.stop(m.group(1))
                return self._send(200, b'{"ok": true}', "application/json")
            return self._send(404, b'{"error": "not found"}', "application/json")
        except (ValueError, RuntimeError) as e:
            return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")

    def _job_poll(self, job_id: str) -> None:
        job = self.jobs.get(job_id) if self.jobs else None
        if not job:
            return self._send(404, b'{"error": "not found"}', "application/json")
        q = dict(x.split("=", 1) for x in (urlparse(self.path).query or "").split("&") if "=" in x)
        offset = int(q.get("offset", 0) or 0)
        log = self.jobs.log(job_id)
        return self._send(200, json.dumps({**job.public(), "output": log[offset:], "offset": len(log),
                                           "took": _dur(job)}).encode(), "application/json")

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
    from .jobs import Jobs
    root = paths.data_dir(app_dir)
    handler = type("Handler", (_Handler,), {"root": root, "app": app_dir.resolve(), "jobs": Jobs(app_dir),
                                            "token": secrets.token_urlsafe(24)})
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
.cmdlayout{display:grid;grid-template-columns:280px minmax(0,1fr);gap:18px;align-items:start}
.cmdnav{position:sticky;top:20px;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:8px}
.cmdnav ol{list-style:none;margin:0;padding:0;display:grid;gap:2px}
.cmdnav .stage{font:600 11px var(--body);letter-spacing:.08em;text-transform:uppercase;color:var(--muted);padding:10px 10px 4px}
.navcmd{display:grid;gap:1px;padding:8px 10px;border-radius:8px;text-decoration:none;color:var(--fg);border:1px solid transparent}
.navcmd:hover{background:var(--sunken)}.navcmd.on{background:var(--sunken);border-color:var(--accent)}
.navcmd.suggested .ntitle::after{content:" · next";color:var(--accent);font-weight:600;font-size:12px}
.ntitle{font-weight:500;font-size:14px}.nstate{font-size:12px;color:var(--muted)}
.nstate.s-miss{color:var(--warn)}.nstate.s-done{color:var(--ok)}.nstate.s-failed{color:var(--bad)}.nstate.s-running,.nstate.s-waiting{color:var(--accent)}
.cmdmain{display:grid;gap:16px;min-width:0}
.cmdpanel{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:20px 22px;display:grid;gap:14px;min-width:0}
.cmdpanel h2{font-size:26px}.cmdpanel .eyebrow{margin:0}
.what{margin:0;font-size:16px;max-width:70ch}
.uses{display:flex;flex-wrap:wrap;gap:6px}.use{font-size:12px;border-radius:999px;padding:2px 10px;border:1px solid var(--line);color:var(--muted)}
.u-dev{color:var(--accent);border-color:var(--accent)}.u-mod{color:#7a4fa8;border-color:#7a4fa8}.u-code{color:var(--warn);border-color:var(--warn)}
.u-pub{color:var(--bad);border-color:var(--bad)}.u-cost{border-style:dashed}
.need{margin:0;padding:8px 12px;border-radius:8px;background:var(--bad-soft);color:var(--bad);font-size:14px}
.fields{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px 18px}
.ffield{display:grid;gap:4px;font-size:13px;align-content:start}
.ffield input,.ffield select{font:14px var(--body);padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);min-width:0;width:100%}
.fflag{display:grid;grid-template-columns:auto minmax(0,1fr);gap:3px 10px;align-items:start;font-size:14px;align-content:start;padding-top:4px}
.fflag input{margin-top:3px;width:16px;height:16px}.fflag .fhelp{grid-column:2}
.flabel{font-weight:500}.flagname{font-size:11px;color:var(--muted);font-weight:400;margin-left:4px;white-space:nowrap}
.fhelp{color:var(--muted);font-size:12px;line-height:1.4}
details.more summary{cursor:pointer;color:var(--accent);font-size:14px;font-weight:500}details.more[open] summary{margin-bottom:12px}
.runbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:14px}
button.run{font:600 15px var(--body);background:var(--accent);color:var(--bg);border:0;border-radius:8px;padding:10px 28px;cursor:pointer}
button.run:disabled{opacity:.45;cursor:not-allowed}
.preview{font-size:12px;color:var(--muted);background:var(--sunken);border-radius:6px;padding:6px 10px;overflow-wrap:anywhere}
.danger{color:var(--bad);font-size:13px;margin:0}.err{color:var(--bad);font-size:13px;margin:0}
.busy{display:inline-flex;align-items:center;gap:8px;margin-top:8px;padding:8px 12px;border:1px solid var(--accent);border-radius:8px;text-decoration:none;color:var(--fg);font-size:14px;width:fit-content}
.recent ul{list-style:none;margin:0;padding:0;display:grid;gap:4px}.recent a{display:flex;gap:10px;align-items:center;text-decoration:none;color:var(--fg);padding:6px 4px;border-bottom:1px solid var(--line)}
.recent a span:first-child{flex:1}
@media (max-width:900px){.cmdlayout{grid-template-columns:minmax(0,1fr)}.cmdnav{position:static}}
.c-run{color:var(--accent)}.c-warn{color:var(--warn)}code.args{color:var(--muted)}
.jobpill{display:flex;align-items:center;gap:8px;margin-top:10px;padding:8px 10px;border:1px solid var(--accent);border-radius:8px;text-decoration:none;color:var(--fg);font:12px var(--mono)}
.jobpill b{margin-left:auto;font:600 11px var(--body);color:var(--accent)}
.spin{width:10px;height:10px;border-radius:50%;border:2px solid var(--accent);border-right-color:transparent;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.issue.isfixed{opacity:.86}.fixedon{margin:2px 0;font-size:13px}
.kv th{width:140px;text-transform:none;letter-spacing:0;font-size:13px;vertical-align:top}
.joblayout{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px;align-items:start}
.devlayout{display:grid;grid-template-columns:minmax(260px,380px) minmax(0,1fr);gap:20px;align-items:start}
.phone{margin:0;display:grid;gap:8px;position:sticky;top:calc(env(safe-area-inset-top,0px) + 20px)}
.bezel{background:#0b0f14;border-radius:26px;padding:10px;border:1px solid var(--line);aspect-ratio:9/19.5;display:grid;place-items:center;overflow:hidden}
.bezel img{width:100%;height:100%;object-fit:contain;border-radius:16px;display:block}
.offline{color:#93a0af;font-size:13px;text-align:center;padding:16px;margin:0}
.phone figcaption{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
.phone .dot{width:8px;height:8px;border-radius:50%;background:var(--idle)}.phone.on .dot{background:var(--bad);animation:blink 1.2s infinite}
.pausebtn{margin-left:auto;font:12px var(--body);background:var(--surface);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:3px 10px;cursor:pointer}
.nowtitle{margin:0;font:13px var(--mono)}.nowtail{height:auto;min-height:200px;max-height:calc(100vh - 260px)}
@media (max-width:1000px){.joblayout,.devlayout{grid-template-columns:minmax(0,1fr)}.phone{position:static;max-width:320px}}
.console-bar{display:flex;justify-content:space-between;align-items:center;font-size:13px;color:var(--muted)}
.console{height:calc(100vh - 290px);min-height:360px;overflow:auto;background:#0b0f14;color:#d6dde6;border-radius:10px;padding:14px 16px;font:12.5px/1.5 var(--mono);white-space:pre-wrap;word-break:break-word}
.stopbtn{font:600 13px var(--body);color:var(--bad);border:1px solid var(--bad);background:none;border-radius:7px;padding:5px 14px;cursor:pointer}
.ask{border:2px solid var(--warn);background:var(--surface);border-radius:12px;padding:14px 16px;display:grid;gap:10px}
.ask .q{margin:0;font:600 15px var(--body)}.answers{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.answers button{font:600 13px var(--body);border:1px solid var(--line);background:var(--surface);color:var(--fg);border-radius:7px;padding:6px 16px;cursor:pointer}
.answers button.yes{background:var(--ok);color:var(--bg);border-color:var(--ok)}
.answers input{font:14px var(--body);padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
.filters{display:flex;gap:6px;flex-wrap:wrap}.filters button{font:500 13px var(--body);border:1px solid var(--line);background:var(--surface);color:var(--fg);border-radius:999px;padding:4px 14px;cursor:pointer}
.filters button.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.issues{display:grid;gap:10px}.issue{display:grid;grid-template-columns:64px minmax(0,1fr) 170px;gap:16px;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;align-items:start}
.rank{display:grid;justify-items:center;gap:4px}.rn{font:600 12px var(--mono);color:var(--muted)}
.score{font:700 20px var(--display);font-variant-numeric:tabular-nums}.score.s-bad{color:var(--bad)}.score.s-warn{color:var(--warn)}.score.s-idle{color:var(--muted)}
.ibody{min-width:0;display:grid;gap:4px}.ibody p{margin:0;font-size:14px}.imeta{color:var(--muted);font-size:13px!important}
.why{font-size:13px!important}.iev{margin:2px 0 0;padding-left:16px;font-size:12.5px}
.ifix{display:grid;gap:6px;justify-items:stretch}.ifix .hint{font-size:12px}
@media (max-width:760px){.issue{grid-template-columns:48px minmax(0,1fr)}.ifix{grid-column:1/-1}}
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
  try{const r=await fetch('/api/version',{cache:'no-store'});const d=await r.json(),v=d.version;
    const pill=document.querySelector('.jobpill');
    if(pill){pill.hidden=!d.job;if(d.job){pill.href='/job/'+d.job.id;pill.querySelector('.jp-cmd').textContent=d.job.command;
      pill.querySelector('.jp-st').textContent=d.job.status}}
    sync.classList.remove('stale');
    if(typeof NO_RELOAD!=='undefined'){setTimeout(check,""" + str(POLL_MS) + """);return}
    if(v!==VERSION&&v!==seen){seen=v;syncText.textContent='changing…';setTimeout(check,1500);return}  // still being written
    if(v!==VERSION){syncText.textContent='updated · refreshing';
      try{sessionStorage.setItem('dm-scroll:'+location.pathname,String(scrollY))}catch(e){}
      location.reload();return}
    syncText.textContent='live · in sync'}
  catch(e){sync.classList.add('stale');syncText.textContent='server stopped'}
  setTimeout(check,""" + str(POLL_MS) + """)}
setTimeout(check,""" + str(POLL_MS) + """);
// The device, live: fetch the next frame when the last one has arrived; stop while the tab is hidden or paused.
document.querySelectorAll('.phone[data-live]').forEach(ph=>{
  const img=ph.querySelector('img'),off=ph.querySelector('.offline'),fps=ph.querySelector('.fps'),pb=ph.querySelector('.pausebtn');
  let paused=false,frames=0,since=Date.now(),timer=null;
  function next(delay){clearTimeout(timer);timer=setTimeout(grab,delay)}
  async function grab(){
    if(paused||document.hidden)return;
    try{const r=await fetch('/api/screen?t='+encodeURIComponent(TOKEN),{cache:'no-store'});
      if(!r.ok){off.textContent=await r.text();off.hidden=false;img.hidden=true;ph.classList.remove('on');fps.textContent='offline';return next(3000)}
      const url=URL.createObjectURL(await r.blob()),old=img.src;img.src=url;img.hidden=false;off.hidden=true;ph.classList.add('on');
      if(old.startsWith('blob:'))setTimeout(()=>URL.revokeObjectURL(old),1000);
      frames++;const s=(Date.now()-since)/1000;if(s>=2){fps.textContent='live · '+(frames/s).toFixed(1)+' fps';frames=0;since=Date.now()}
      next(120)}
    catch(e){fps.textContent='offline';next(3000)}}
  pb.addEventListener('click',()=>{paused=!paused;pb.textContent=paused?'Resume':'Pause';
    if(paused){ph.classList.remove('on');fps.textContent='paused'}else grab()});
  img.addEventListener('click',()=>{boxImg.src=img.src;box.hidden=false});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!paused)grab()});
  grab()});
if(typeof NOW_POLL!=='undefined'){
  const t=document.getElementById('nowtail'),l=document.getElementById('nowlink'),s=document.getElementById('nowstatus');
  async function now(){try{const d=await (await fetch('/api/now',{cache:'no-store'})).json();
    if(d.kind==='none'){l.textContent='';t.textContent='Nothing has run yet. Start one from Commands or a terminal.'}
    else{l.textContent=d.title;l.href=d.href;s.innerHTML='<span class="chip c-'+(d.status==='idle'?'idle':'ok')+'">'+
      (d.kind==='run'?(d.status==='idle'?'last run · idle':'run writing · '+d.age+' s ago'):d.status)+'</span>';
      const atEnd=t.scrollTop+t.clientHeight>=t.scrollHeight-8;
      t.textContent=d.kind==='run'?'Newest files:\\n'+d.tail:d.tail;if(atEnd)t.scrollTop=t.scrollHeight}}catch(e){}
    setTimeout(now,1500)}
  now()}
const box=document.querySelector('.lightbox'),boxImg=box.querySelector('img');
box.addEventListener('click',e=>{if(e.target!==boxImg)box.hidden=true});
document.addEventListener('keydown',e=>{if(e.key==='Escape')box.hidden=true});
document.querySelectorAll('.sshot img,.shots img,.mini img').forEach(i=>i.addEventListener('click',e=>{
  if(i.closest('.mini'))return;e.preventDefault();boxImg.src=i.dataset.full||i.src;box.hidden=false}));
document.querySelectorAll('.scard').forEach(c=>c.querySelectorAll('.seg button').forEach(b=>b.addEventListener('click',()=>{
  c.querySelectorAll('.seg button').forEach(x=>x.classList.toggle('on',x===b));
  c.querySelector('.shot').hidden=b.dataset.show!=='shot';c.querySelector('.heat').hidden=b.dataset.show!=='heat'})));
"""

JOB_JS = r"""
async function api(url, body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-DM-Token':TOKEN},body:JSON.stringify(body||{})});
  const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.statusText);return j}
// Commands page: one step at a time, a live preview of the exact command, and Run
function values(form){const v={};form.querySelectorAll('input,select').forEach(i=>{if(i.name)v[i.name]=i.type==='checkbox'?i.checked:i.value});return v}
function preview(form){const parts=['droid-monkey',form.dataset.cmd],args=[];
  form.querySelectorAll('input,select').forEach(i=>{const f=i.closest('label')&&i.closest('label').querySelector('.flagname');if(!f)return;
    const flag=f.textContent;
    if(i.type==='checkbox'){if(i.checked!==i.defaultChecked)parts.push(i.checked?flag:(flag.startsWith('--')?flag.replace(/^--(?!no-)/,'--no-'):flag))}
    else if(i.value&&!(i.tagName==='SELECT'&&i.querySelector('option[selected]')&&i.value===i.querySelector('option[selected]').value)){
      if(flag===flag.toUpperCase())args.push(/\s/.test(i.value)?JSON.stringify(i.value):i.value);else parts.push(flag,i.value)}});
  const pv=form.querySelector('.preview');if(pv)pv.textContent=parts.concat(args).join(' ')}
const panels=[...document.querySelectorAll('form.cmdpanel')];
function show(){if(!panels.length)return;let id=location.hash.slice(1)||SUGGESTED;
  if(!document.getElementById('panel-'+id))id=panels[0].id.slice(6);
  panels.forEach(f=>f.hidden=f.id!=='panel-'+id);
  document.querySelectorAll('.navcmd').forEach(a=>a.classList.toggle('on',a.dataset.cmd===id))}
window.addEventListener('hashchange',()=>{show();const p=document.querySelector('form.cmdpanel:not([hidden])');
  if(p&&innerWidth<900)p.scrollIntoView({block:'start'})});show();
panels.forEach(f=>{preview(f);f.addEventListener('input',()=>preview(f));f.addEventListener('change',()=>preview(f));
  f.addEventListener('submit',async e=>{e.preventDefault();const b=f.querySelector('button.run');
    let confirm=false;
    if(f.dataset.publishes==='1'){
      if(!f.querySelector('.confirm')){const c=document.createElement('p');c.className='confirm danger';
        c.innerHTML='This pushes to GitHub and opens a pull request. Press <b>Run</b> again to confirm.';b.parentNode.after(c);return}
      confirm=true}
    b.disabled=true;b.textContent='Starting…';
    try{const j=await api('/api/jobs',{command:f.dataset.cmd,values:values(f),confirm});location.href='/job/'+j.id}
    catch(err){b.disabled=false;b.textContent='Run';let m=f.querySelector('.err');if(!m){m=document.createElement('p');m.className='err';b.parentNode.after(m)}m.textContent=err.message}})});
// Issues page: rank with Jev, filter, and Fix (starts the fix as a job; it asks you before changing code)
document.querySelectorAll('.rankbtn').forEach(b=>b.addEventListener('click',async()=>{
  const msg=document.querySelector('.rankmsg');document.querySelectorAll('.rankbtn').forEach(x=>x.disabled=true);
  msg.textContent='Jev is reading the issues…';
  try{const r=await api('/api/issues/rank',{all:b.dataset.all==='1'});msg.textContent=r.note||('ranked '+r.ranked);location.reload()}
  catch(e){msg.textContent=e.message;document.querySelectorAll('.rankbtn').forEach(x=>x.disabled=false)}}));
document.querySelectorAll('.issues ~ * , .filters [data-f]').forEach(()=>{});
document.querySelectorAll('[data-f]').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('[data-f]').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('.issue').forEach(i=>i.hidden=b.dataset.f==='fixed'?i.dataset.status!=='fixed':
    b.dataset.f!=='all'&&i.dataset.src!==b.dataset.f)}));
document.querySelectorAll('.prbtn').forEach(b=>b.addEventListener('click',async()=>{
  const box=b.closest('.ifix,.prbar');
  if(!box.querySelector('.confirm')){const c=document.createElement('p');c.className='confirm danger';
    c.innerHTML='This pushes the branch to GitHub and opens a pull request. Press <b>Open PR</b> again to confirm.';box.append(c);return}
  b.disabled=true;b.textContent='Starting…';
  try{const j=await api('/api/jobs',{command:'pr',values:{run_id:b.dataset.run},confirm:true});location.href='/job/'+j.id}
  catch(e){b.disabled=false;b.textContent='Open PR';box.querySelector('.confirm').textContent=e.message}}));
document.querySelectorAll('.fixbtn').forEach(b=>b.addEventListener('click',async()=>{
  b.disabled=true;b.textContent='Starting…';
  const values=b.dataset.cmd==='run'?{only:b.dataset.arg}:{issue_id:b.dataset.arg};
  try{const j=await api('/api/jobs',{command:b.dataset.cmd,values});location.href='/job/'+j.id}
  catch(e){b.disabled=false;b.textContent='Fix';alert(e.message)}}));
// Job page: stream the output, show the question, answer it, stop
if(typeof JOB!=='undefined'){
  const con=document.getElementById('console'),ask=document.getElementById('ask'),q=document.getElementById('question'),
        st=document.getElementById('jstatus'),dur=document.getElementById('jdur'),stop=document.getElementById('stop'),
        follow=document.getElementById('follow');let off=0,last='';
  const tone={done:'ok',running:'run',waiting:'warn',failed:'bad',stopped:'idle'};
  async function poll(){
    try{const r=await fetch('/api/jobs/'+encodeURIComponent(JOB)+'?offset='+off,{cache:'no-store'});const j=await r.json();
      if(j.output){con.textContent+=j.output;off=j.offset;if(follow.checked)con.scrollTop=con.scrollHeight}
      if(j.status!==last){st.innerHTML='<span class="chip c-'+(tone[j.status]||'idle')+'">'+j.status+'</span>';last=j.status}
      dur.textContent=j.took;
      ask.hidden=j.status!=='waiting';if(j.status==='waiting')q.textContent=j.waiting;
      stop.hidden=!(j.status==='running'||j.status==='waiting');
      if(j.status==='running'||j.status==='waiting')setTimeout(poll,700)}
    catch(e){setTimeout(poll,2000)}}
  poll();
  async function answer(t){ask.hidden=true;try{await api('/api/jobs/'+encodeURIComponent(JOB)+'/answer',{text:t})}catch(e){alert(e.message)}}
  ask.querySelectorAll('button[data-a]').forEach(b=>b.addEventListener('click',()=>answer(b.dataset.a)));
  document.getElementById('send').addEventListener('click',()=>{const i=document.getElementById('free');answer(i.value);i.value=''});
  stop.addEventListener('click',async()=>{stop.disabled=true;stop.textContent='Stopping…';
    try{await api('/api/jobs/'+encodeURIComponent(JOB)+'/stop')}catch(e){stop.disabled=false;stop.textContent='Stop'}});
}
"""
