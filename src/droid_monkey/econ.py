"""`droid-monkey econ`: unit economics from droid-monkey's own runs, for the cfo.ai business plan.

It reads the runs of one or more apps (.droid-monkey/runs: each run's summary.json, every attack's verdict.json,
the hunt's issues.json) plus the one-time costs (analysis-run.json, ux-audit.json). These are the numbers each step
reported, the same costs that are on its neatlogs spans. It writes:

- drivers.csv: one row per number (value, unit, measured yes/no, source). Measured rows name the run they came
  from; assumptions are marked measured=no. This is the file cfo.ai's Ari imports.
- unit_economics.json: the drivers plus a fix budget per tier. The plan's margin floor sets it:
      fix budget per run = price / runs per month x (1 - margin floor) - compute per run
  `droid-monkey run --tier NAME` loads it and stops fixing (it keeps attacking) once a run has spent it.

Assumptions (prices, runner cost, tiers) come from inputs.json in the output folder when it exists, so the founder
edits them there and reruns econ; defaults are written on the first run.
"""

import csv
import json
import statistics
import time
from pathlib import Path

from . import paths

DEFAULT_INPUTS = {
    "margin_floor": 0.7,
    "runner_price_per_minute": 0.008,   # assumption: a Linux CI runner with KVM; check current prices
    "run_overhead_minutes": 5.0,        # assumption: emulator boot + build + install per run
    "attacks_per_run": 8,
    "fix_attempt_share": 0.5,           # assumption: share of failures the developer lets droid-monkey fix
    "price_per_verified_fix": 3.0,      # assumption: usage price above a tier's included fixes
    "tiers": {
        "free": {"price": 0.0, "runs_per_month": 10, "fixes": False},
        "team": {"price": 99.0, "runs_per_month": 30, "fixes": True},
        "scale": {"price": 499.0, "runs_per_month": 300, "fixes": True},
    },
}
BROKE = {"crashed", "anr", "lost_state", "leaked", "froze", "no_feedback", "slow_start"}


def _q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else 0.0


def _json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def measure(app_dirs: list[Path]) -> dict:
    """Every measured number, with where it came from."""
    fixes, fixed, reasoning, run_secs, attack_secs, outcomes, setup, ux, hunts = [], 0, [], [], [], [], [], [], []
    for app in app_dirs:
        root = paths.data_dir(app)
        for run in sorted((root / "runs").glob("*")) if (root / "runs").exists() else []:
            s = _json(run / "summary.json")
            if s and s.get("mode", "droid-monkey") == "droid-monkey":
                if (s.get("reasoning") or {}).get("cost_usd") is not None and s.get("outcomes"):
                    reasoning.append(float(s["reasoning"]["cost_usd"] or 0))
                    run_secs.append(float(s.get("seconds") or 0))
                for o in s.get("outcomes", []):
                    m = o.get("metrics") or {}
                    spend = float(m.get("diag_cost_usd") or 0) + float(m.get("coder_cost_usd") or 0)
                    if float(m.get("coder_cost_usd") or 0) > 0:
                        fixes.append((spend, f"{app.name} {run.name} {o.get('attack')}"))
                        fixed += o.get("status") == "fixed"
            for v in run.glob("*/verdict.json"):
                d = _json(v) or {}
                if d.get("seconds"):
                    attack_secs.append(float(d["seconds"]))
                if d.get("outcome"):
                    outcomes.append(d["outcome"])
            h = _json(run / "issues.json")
            if h and (h.get("usage") or {}).get("cost_usd") is not None:
                hunts.append(float(h["usage"]["cost_usd"]))
        a = _json(root / "analysis-run.json")
        if a and a.get("cost_usd"):
            setup.append((float(a["cost_usd"]), app.name))
        u = _json(root / "ux-audit.json")
        if u and (u.get("cost_usd") or (u.get("usage") or {}).get("cost_usd")):
            ux.append((float(u.get("cost_usd") or u["usage"]["cost_usd"]), app.name))
    costs = [c for c, _ in fixes]
    src = {round(c, 6): s for c, s in fixes}

    def at(p):
        v = _q(costs, p)
        return v, src.get(round(v, 6), "")
    broke = sum(1 for o in outcomes if o in BROKE)
    return {
        "apps": [a.name for a in app_dirs], "fix_attempts": len(costs), "verified_fixes": fixed,
        "cost_per_fix_attempt_mean": statistics.mean(costs) if costs else 0.0,
        "cost_per_fix_attempt_p50": at(0.5), "cost_per_fix_attempt_p90": at(0.9), "cost_per_fix_attempt_max": at(1.0),
        "cost_per_verified_fix": sum(costs) / fixed if fixed else 0.0,
        "reasoning_cost_per_run": statistics.mean(reasoning) if reasoning else 0.0, "runs": len(reasoning),
        "attack_seconds_mean": statistics.mean(attack_secs) if attack_secs else 0.0,
        "attack_seconds_p90": _q(attack_secs, 0.9), "attacks": len(attack_secs),
        "attack_failure_rate": broke / len(outcomes) if outcomes else 0.0,
        "setup_cost_per_app": setup, "ux_audit_cost_per_app": ux,
        "hunt_cost_mean": statistics.mean(hunts) if hunts else 0.0, "hunts": len(hunts),
    }


def economics(m: dict, inputs: dict) -> dict:
    """Per-run cost and a fix budget per tier that keeps the margin floor."""
    attack_min = inputs["attacks_per_run"] * m["attack_seconds_mean"] / 60
    compute = inputs["runner_price_per_minute"] * (inputs["run_overhead_minutes"] + attack_min)
    fixes_per_run = inputs["attacks_per_run"] * m["attack_failure_rate"] * inputs["fix_attempt_share"]
    fix_cost = m["cost_per_fix_attempt_mean"]
    tiers = {}
    for name, t in inputs["tiers"].items():
        revenue = t["price"] / t["runs_per_month"] if t["runs_per_month"] else 0.0
        budget = max(0.0, revenue * (1 - inputs["margin_floor"]) - compute - m["reasoning_cost_per_run"]) if t["fixes"] else 0.0
        expected = compute + m["reasoning_cost_per_run"] + (fixes_per_run * fix_cost if t["fixes"] else 0.0)
        tiers[name] = {**t, "revenue_per_run": round(revenue, 4), "fix_budget_per_run": round(budget, 4),
                       "fixes_within_budget": round(budget / fix_cost, 2) if fix_cost else 0.0,
                       "expected_cost_per_run": round(expected, 4),
                       "gross_margin_expected": round(1 - expected / revenue, 3) if revenue else None}
    fix_price = inputs["price_per_verified_fix"]
    return {"compute_per_run": round(compute, 4), "expected_fix_attempts_per_run": round(fixes_per_run, 2),
            "verified_fix_margin": round(1 - m["cost_per_verified_fix"] / fix_price, 3) if fix_price else None,
            "tiers": tiers}


def drivers(m: dict, inputs: dict, e: dict) -> list[dict]:
    def row(driver, value, unit, measured, source):
        return {"driver": driver, "value": round(value, 5) if isinstance(value, float) else value, "unit": unit,
                "measured": "yes" if measured else "no", "source": source}
    runs_src = f"droid-monkey run receipts ({', '.join(m['apps'])}); the same costs are on the runs' neatlogs spans"
    rows = [
        row("fix_attempts_measured", m["fix_attempts"], "fix attempts", True, runs_src),
        row("verified_fixes_measured", m["verified_fixes"], "fixes", True, runs_src),
        row("cost_per_fix_attempt_mean", m["cost_per_fix_attempt_mean"], "USD", True, runs_src + "; diagnosis + coding agent"),
    ]
    for k in ("p50", "p90", "max"):
        v, s = m[f"cost_per_fix_attempt_{k}"]
        rows.append(row(f"cost_per_fix_attempt_{k}", v, "USD", True, f"run {s}" if s else runs_src))
    rows += [
        row("cost_per_verified_fix", m["cost_per_verified_fix"], "USD", True, "all fix spend / verified fixes, " + runs_src),
        row("reasoning_cost_per_run", m["reasoning_cost_per_run"], "USD", True, f"{m['runs']} runs, plan + decide"),
        row("attack_seconds_mean", m["attack_seconds_mean"], "seconds", True, f"{m['attacks']} attack verdicts"),
        row("attack_seconds_p90", m["attack_seconds_p90"], "seconds", True, f"{m['attacks']} attack verdicts"),
        row("attack_failure_rate", m["attack_failure_rate"], "fraction", True, f"{m['attacks']} attack verdicts, broke / all; test apps, some with planted bugs, so higher than a typical app"),
        row("hunt_cost_mean", m["hunt_cost_mean"], "USD", True, f"{m['hunts']} hunts"),
    ]
    rows += [row(f"setup_cost_{app}", c, "USD per app, once", True, f"{app} analysis-run.json") for c, app in m["setup_cost_per_app"]]
    rows += [row(f"ux_audit_cost_{app}", c, "USD per app", True, f"{app} ux-audit.json") for c, app in m["ux_audit_cost_per_app"]]
    rows += [
        row("runner_price_per_minute", inputs["runner_price_per_minute"], "USD", False, "assumption: Linux CI runner with KVM"),
        row("run_overhead_minutes", inputs["run_overhead_minutes"], "minutes", False, "assumption: emulator boot + build"),
        row("attacks_per_run", inputs["attacks_per_run"], "attacks", False, "assumption"),
        row("fix_attempt_share", inputs["fix_attempt_share"], "fraction", False, "assumption: failures the developer lets it fix"),
        row("compute_per_run", e["compute_per_run"], "USD", False, "runner price x (overhead + attacks x attack_seconds_mean)"),
        row("expected_fix_attempts_per_run", e["expected_fix_attempts_per_run"], "fixes", False,
            "attacks_per_run x attack_failure_rate x fix_attempt_share"),
        row("margin_floor", inputs["margin_floor"], "fraction", False, "assumption, set by the founder"),
        row("price_per_verified_fix", inputs["price_per_verified_fix"], "USD", False, "assumption: usage price"),
    ]
    for name, t in e["tiers"].items():
        rows += [row(f"{name}_price", float(t["price"]), "USD per month", False, "assumption"),
                 row(f"{name}_runs_per_month", t["runs_per_month"], "runs", False, "assumption"),
                 row(f"{name}_fix_budget_per_run", t["fix_budget_per_run"], "USD", False,
                     "price / runs x (1 - margin_floor) - compute - reasoning" if t["fixes"] else "attack-only tier: no fixes")]
    return rows


def run(app_dirs: list[Path], out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    inp_file = out / "inputs.json"
    inputs = {**DEFAULT_INPUTS, **(_json(inp_file) or {})}
    if not inp_file.exists():
        inp_file.write_text(json.dumps(DEFAULT_INPUTS, indent=1))
    m = measure(app_dirs)
    e = economics(m, inputs)
    rows = drivers(m, inputs, e)
    with (out / "drivers.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["driver", "value", "unit", "measured", "source"])
        w.writeheader()
        w.writerows(rows)
    doc = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "inputs": inputs, "measured": m, **e}
    (out / "unit_economics.json").write_text(json.dumps(doc, indent=1, default=str))
    home = Path.home() / ".droid-monkey"
    home.mkdir(exist_ok=True)
    (home / "unit_economics.json").write_text(json.dumps(doc, indent=1, default=str))  # what `run --tier` reads
    return doc


def tier_budget(name: str, path: Path | None = None) -> float:
    """The fix budget per run for a tier, from the last `droid-monkey econ`."""
    p = path or Path.home() / ".droid-monkey" / "unit_economics.json"
    doc = _json(p)
    if not doc:
        raise ValueError(f"no unit economics at {p}: run `droid-monkey econ` first")
    tiers = doc.get("tiers", {})
    if name not in tiers:
        raise ValueError(f"no tier {name!r} in {p} (tiers: {', '.join(tiers)})")
    return float(tiers[name]["fix_budget_per_run"])
