"""Small inline SVG charts for the run dashboard: no dependencies, colours from the page's CSS variables, so they
follow light and dark mode. Each returns an <svg> string, or "" when there is nothing to draw."""

import html

W = 880  # drawing width in px; the SVGs scale down to the card width


def _svg(height: int, body: str, label: str) -> str:
    return (f'<svg class="chart" viewBox="0 0 {W} {height}" role="img" aria-label="{html.escape(label)}" '
            f'xmlns="http://www.w3.org/2000/svg">{body}</svg>')


def _text(x, y, s, anchor="start", cls="muted", size=12) -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" font-size="{size}" '
            f'style="fill:var(--{cls})">{html.escape(str(s))}</text>')


def _seconds_axis(y: float, total_ms: float, x0: float, width: float) -> str:
    step = next((s for s in (1, 2, 5, 10, 20, 30, 60) if total_ms / 1000 / s <= 10), 120)
    out = []
    t = 0
    while t * 1000 <= total_ms:
        x = x0 + width * t * 1000 / total_ms
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y - 4}" y2="{y}" style="stroke:var(--line)"/>'
                   + _text(x, y + 13, f"{t}s", "middle", size=11))
        t += step
    return "".join(out)


def main_thread(timeline: dict, long_slices: list[dict]) -> str:
    """What the main thread did over the trace, with every long block marked: the picture of a freeze or an ANR."""
    bins = (timeline or {}).get("bins") or []
    if not bins:
        return ""
    width_ms = timeline["bin_ms"]
    total = width_ms * len(bins)
    x0, plot_w, top, h = 8, W - 16, 34, 44
    bw = plot_w / len(bins)
    colors = ("ok", "warn", "idle", "bad")  # running, waiting for a CPU, asleep or blocked, I/O
    body = []
    for i, b in enumerate(bins):
        y = top + h
        for g in (2, 1, 3, 0):
            if b[g] <= 0:
                continue
            seg = h * b[g] / width_ms
            y -= seg
            body.append(f'<rect x="{x0 + i * bw:.2f}" y="{y:.2f}" width="{bw + 0.3:.2f}" height="{seg:.2f}" '
                        f'style="fill:var(--{colors[g]})"/>')
    for s in long_slices or []:
        if s.get("start_ms") is None or s["ms"] < 300:
            continue
        x, w = x0 + plot_w * s["start_ms"] / total, max(2.0, plot_w * s["ms"] / total)
        asleep = s.get("states", {}).get("S", 0)
        label = f"{s['ms']:.0f} ms" + (f" ({asleep:.0f} asleep)" if asleep > s["ms"] / 2 else "")
        body.append(f'<rect x="{x:.1f}" y="{top - 6}" width="{w:.1f}" height="{h + 12}" rx="3" '
                    f'style="fill:none;stroke:var(--bad);stroke-width:2"/>' + _text(x, top - 11, label, cls="bad"))
    legend = [("running", "ok"), ("waiting for a CPU", "warn"), ("asleep or blocked", "idle"), ("disk I/O", "bad")]
    lx = 8
    for name, c in legend:
        body.append(f'<rect x="{lx}" y="4" width="10" height="10" rx="2" style="fill:var(--{c})"/>'
                    + _text(lx + 14, 13, name))
        lx += 26 + 7 * len(name)
    body.append(_seconds_axis(top + h + 4, total, x0, plot_w))
    return _svg(top + h + 22, "".join(body), "main thread over time")


def frame_times(histogram: list[list[int]]) -> str:
    """How long frames took: smooth (16 ms or less), janky, very slow, and frozen (over 700 ms, Play vitals)."""
    if not histogram:
        return ""
    groups = [("≤ 16 ms", 0, 16, "ok"), ("17–33 ms", 17, 33, "warn"), ("34–100 ms", 34, 100, "warn"),
              ("101–700 ms", 101, 699, "bad"), ("frozen ≥ 700 ms", 700, 10 ** 9, "bad")]
    counts = [sum(n for ms, n in histogram if lo <= ms <= hi) for _, lo, hi, _ in groups]
    peak = max(counts) or 1
    rows, body = len(groups), []
    for i, ((name, _, _, c), n) in enumerate(zip(groups, counts)):
        y = 6 + i * 24
        w = 0 if not n else max(3, (W - 260) * n / peak)
        body.append(_text(130, y + 13, name, "end", "fg") + f'<rect x="140" y="{y}" width="{w:.1f}" height="16" rx="3" '
                    f'style="fill:var(--{c})"/>' + _text(146 + w, y + 13, f"{n} frame{'s' if n != 1 else ''}"))
    return _svg(rows * 24 + 8, "".join(body), "frame times")


def launches(runs: dict, limits: dict) -> str:
    """Every timed launch against the Play vitals limit for its kind."""
    items = [(kind, r["ms"]) for kind in ("cold", "hot") for r in runs.get(kind, [])]
    if not items:
        return ""
    scale = max(max(ms for _, ms in items), max(limits.get(k, 0) for k, _ in items)) * 1.1
    body, y = [], 6
    for kind in ("cold", "hot"):
        mine = [ms for k, ms in items if k == kind]
        if not mine:
            continue
        limit = limits.get(kind)
        for j, ms in enumerate(mine):
            w = (W - 230) * ms / scale
            c = "bad" if limit and ms >= limit else "ok"
            body.append(_text(110, y + 12, f"{kind} #{j + 1}", "end", "fg")
                        + f'<rect x="120" y="{y}" width="{w:.1f}" height="14" rx="3" style="fill:var(--{c})"/>'
                        + _text(126 + w, y + 12, f"{ms} ms"))
            y += 20
        if limit:
            x = 120 + (W - 230) * limit / scale
            top = y - 20 * len(mine)
            body.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top - 2}" y2="{y - 4}" '
                        f'style="stroke:var(--bad);stroke-dasharray:4 3"/>' + _text(x + 4, top + 10, f"limit {limit} ms", cls="bad"))
        y += 8
    return _svg(y + 2, "".join(body), "launch times")


def memory(before: dict, after: dict) -> str:
    """Live Activities and Java heap after a forced GC, before and after the attack."""
    if not before or not after:
        return ""
    pairs = [("Activities alive", before["activities"], after["activities"], ""),
             ("Java heap", round(before["java_heap_kb"] / 1024, 1), round(after["java_heap_kb"] / 1024, 1), " MB")]
    body, y = [], 6
    for name, b, a, unit in pairs:
        peak = max(b, a) or 1
        for label, v, c in (("before", b, "acc"), ("after", a, "bad" if a > b else "ok")):
            w = (W - 300) * v / peak
            body.append(_text(150, y + 12, f"{name}, {label}", "end", "fg")
                        + f'<rect x="160" y="{y}" width="{max(w, 2):.1f}" height="14" rx="3" style="fill:var(--{c})"/>'
                        + _text(166 + w, y + 12, f"{v}{unit}"))
            y += 20
        y += 8
    return _svg(y, "".join(body), "memory before and after")


def rss(series: list[list[float]]) -> str:
    """The app's resident memory over the trace."""
    if len(series or []) < 2:
        return ""
    t_end = series[-1][0] or 1
    lo, hi = min(v for _, v in series), max(v for _, v in series)
    span = (hi - lo) or 1
    pts = " ".join(f"{8 + (W - 80) * t / t_end:.1f},{58 - 46 * (v - lo) / span:.1f}" for t, v in series)
    body = (f'<polyline points="{pts}" style="fill:none;stroke:var(--acc);stroke-width:2"/>'
            + _text(W - 66, 16, f"{hi:.0f} MB") + _text(W - 66, 60, f"{lo:.0f} MB") + _seconds_axis(66, t_end, 8, W - 80))
    return _svg(84, body, "resident memory over time")
