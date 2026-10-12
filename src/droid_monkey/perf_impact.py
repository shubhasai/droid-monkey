"""How long users wait on the way to each screen, from timings droid-monkey measured, and what that is known to
cost. Measured: each step's time until the screen settled (from exploring) and the app's cold start (from the
slow_startup attack, when it has run). Judged against the classic response-time limits for people using software
(Nielsen, "Usability Engineering", 1993): 0.1 s feels instant, 1 s keeps the user's flow of thought, 10 s loses
their attention. The conversion impact is an industry benchmark, not a forecast for this app: it shows the size of
the effect, and real numbers need the app's own analytics.
"""

import json
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths
from .explorer import ScreenMap

INSTANT_S, FLOW_S, ATTENTION_S = 0.1, 1.0, 10.0
BENCHMARK = ("Industry benchmark (Deloitte for Google, 'Milliseconds Make Millions', 2020, mobile sites): a 0.1 s "
             "faster experience raised retail conversions by 8.4% and travel conversions by 10.1%. Indicative of the "
             "effect's size only; this app's real impact needs its own funnel analytics.")


@dataclass
class Step:
    label: str
    to_screen: str
    seconds: float
    band: str            # instant | noticeable | breaks flow | loses attention
    from_screen: str = ""


@dataclass
class Route:
    screen: str
    steps: list[Step] = field(default_factory=list)
    wait_s: float = 0.0          # waiting along the route (excluding the launch)
    cold_start_s: float | None = None

    @property
    def total_s(self) -> float:
        return round(self.wait_s + (self.cold_start_s or 0), 2)

    def slow(self) -> list[Step]:
        return [s for s in self.steps if s.seconds >= FLOW_S]


def band(seconds: float) -> str:
    if seconds < INSTANT_S:
        return "instant"
    if seconds < FLOW_S:
        return "noticeable"
    if seconds < ATTENTION_S:
        return "breaks flow"
    return "loses attention"


def cold_start(app_dir: Path) -> float | None:
    """Median cold start (s) from the most recent slow_startup attack, if one ran."""
    runs = sorted(paths.data_dir(app_dir).glob("runs/*/slow_startup*/verdict.json"), key=lambda p: p.stat().st_mtime)
    for v in reversed(runs):
        try:
            cold = [r["ms"] for r in (json.loads(v.read_text()).get("metrics") or {}).get("startup", {}).get("cold", [])]
        except (ValueError, KeyError, TypeError):
            continue
        if cold:
            cold.sort()
            return round(cold[len(cold) // 2] / 1000, 2)
    return None


def routes(app_dir: Path, smap: ScreenMap) -> dict[str, Route]:
    """For every screen, the fewest-taps route from launch through recorded actions, with each step's wait."""
    start = cold_start(app_dir)
    roots = [sid for sid, s in smap.screens.items() if not s.path] or list(smap.screens)[:1]
    out = {r: Route(screen=r, cold_start_s=start) for r in roots}
    links: dict[str, dict[str, tuple[str, float]]] = {}   # from -> to -> (label, seconds)
    for sid, info in smap.screens.items():
        for a in info.actions:
            if a.to_screen in smap.screens:
                links.setdefault(sid, {}).setdefault(a.to_screen, (a.label, a.settle_seconds or 0.0))
    for e in smap.edges:  # links recorded without an action timing (e.g. by the agent explorer)
        if e.get("from") in smap.screens and e.get("to") in smap.screens:
            links.setdefault(e["from"], {}).setdefault(e["to"], (e.get("action", ""), 0.0))
    queue = deque(roots)
    while queue:
        cur = queue.popleft()
        for nxt, (label, seconds) in links.get(cur, {}).items():
            if nxt in out:
                continue
            step = Step(label=str(label)[:50], to_screen=nxt, seconds=round(seconds, 2), band=band(seconds),
                        from_screen=cur)
            prev = out[cur]
            out[nxt] = Route(screen=nxt, steps=prev.steps + [step], wait_s=round(prev.wait_s + step.seconds, 2),
                             cold_start_s=start)
            queue.append(nxt)
    return out


def facts(app_dir: Path, smap: ScreenMap, flows: list[dict]) -> dict:
    """What the review gets: per screen the measured wait to reach it and its slow steps; per flow the same for the
    screens behind it; the limits and the benchmark to judge them by."""
    from .designer import flow_reach
    rs = routes(app_dir, smap)
    per_screen = {sid: {"taps": len(r.steps), "wait_s": r.wait_s, "with_cold_start_s": r.total_s,
                        "slow_steps": [f"{s.label} -> {s.to_screen}: {s.seconds:.1f}s ({s.band})" for s in r.slow()]}
                  for sid, r in rs.items()}
    per_flow = {}
    for f in flows:
        reached = [sid for sid in flow_reach(f, smap) if sid in rs]
        if reached:
            worst = max((s for sid in reached for s in rs[sid].slow()), key=lambda s: s.seconds, default=None)
            per_flow[f.get("name", "?")] = {
                "screens": reached, "max_wait_to_reach_s": max(rs[sid].wait_s for sid in reached),
                "slowest_step": f"{worst.label} -> {worst.to_screen}: {worst.seconds:.1f}s ({worst.band})" if worst else None}
    return {"limits": {"instant_s": INSTANT_S, "keeps_flow_s": FLOW_S, "loses_attention_s": ATTENTION_S},
            "cold_start_s": cold_start(app_dir), "benchmark": BENCHMARK, "per_flow": per_flow,
            "per_screen": {k: v for k, v in per_screen.items() if v["slow_steps"] or v["wait_s"] >= FLOW_S},
            "slow_steps": [describe(st) for _, st in slow_steps(app_dir, smap)[:15]],
            "note": "step times are measured from the tap until the screen stopped changing, so they include up to "
                    "~0.3 s of droid-monkey's polling"}


def slow_steps(app_dir: Path, smap: ScreenMap) -> list[tuple[str, Step]]:
    """Every distinct step that breaks the user's flow (>= 1 s), slowest first."""
    seen, out = set(), []
    for sid, r in routes(app_dir, smap).items():
        for s in r.slow():
            if (s.label, s.to_screen) not in seen:
                seen.add((s.label, s.to_screen))
                out.append((sid, s))
    for sid, info in smap.screens.items():  # slow actions that stay on their screen (a load, a submit)
        for a in info.actions:
            target = a.to_screen or sid
            if (a.settle_seconds or 0) >= FLOW_S and (a.label[:50], target) not in seen:
                seen.add((a.label[:50], target))
                out.append((sid, Step(label=a.label[:50], to_screen=target, seconds=round(a.settle_seconds, 2),
                                      band=band(a.settle_seconds), from_screen=sid)))
    return sorted(out, key=lambda x: -x[1].seconds)


def describe(st: Step) -> str:
    where = f"on {st.from_screen}, " if st.from_screen else ""
    goes = "" if st.to_screen == st.from_screen else f" -> {st.to_screen}"
    return f"{where}'{st.label}'{goes}: {st.seconds:.1f}s ({st.band})"


def as_dict(route: Route) -> dict:
    return {**asdict(route), "total_s": route.total_s}
