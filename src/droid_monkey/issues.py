"""Every issue droid-monkey has found for an app, in one ranked list, each with a way to fix it.

Sources, newest first and de-duplicated: attacks that broke the app (runs/*/results.json and full runs' outcomes),
bugs in normal use (runs/hunt-*/issues.json) and the UX audit (ux-audit.json; findings it could only suspect from
the code are kept, marked as such).

Ranking: Jev (TypeSafe's System One model, see jev.py) reads each issue and gives the probability of each impact
level for the app's users, and the probability that it is a real bug rather than a test artefact. Score = expected
impact x how real it is. Rankings are saved in issues-rank.json and only new issues are sent to Jev. Without a
TYPESAFE_API_KEY the list falls back to a severity-based score, and says so.

How each kind is fixed: an attack failure through `run --attacks <name>` (fix, replay the identical attack, commit);
a hunt or UX issue through `fix <id>` (fix, re-check on the device, commit).
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

IMPACT = {"critical": "the app crashes, loses the user's data or money, or a main flow becomes unusable",
          "high": "a main flow works wrongly or shows wrong information (prices, totals, dates, counts)",
          "medium": "annoying, confusing or inaccessible, but the user can still finish what they came to do",
          "low": "cosmetic, rare, or only visible to a few users"}
IMPACT_WEIGHT = {"critical": 1.0, "high": 0.7, "medium": 0.4, "low": 0.15}
SEVERITY_WEIGHT = {"crashed": 0.95, "anr": 0.95, "high": 0.75, "lost_state": 0.7, "leaked": 0.6, "froze": 0.6,
                   "no_feedback": 0.5, "slow_start": 0.45, "medium": 0.45, "low": 0.2}
NOT_PROBLEMS = {"survived", "skipped", "unreachable", "harness_error", "fixed", "declined"}


@dataclass
class Issue:
    id: str
    source: str                 # attack | hunt | ux
    title: str
    detail: str
    severity: str               # the source's own severity or outcome
    screen: str = ""
    evidence: list[str] = field(default_factory=list)
    run: str = ""               # the run it was found in
    found: str = ""             # when
    attack: str = ""            # for attack issues: the attack's name
    steps: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    observed: bool = True       # False: suspected from the code only (UX)
    key: str = ""               # what identifies the same issue in a later run (hunt key, attack name, ux id)
    status: str = "open"        # open | fixed (committed on its own branch)
    branch: str = ""            # the fix's branch, when fixed
    commit: str = ""
    fix_run: str = ""           # the run that fixed it: `pr <fix_run>` opens its pull request
    seen_after_fix: bool = False  # a later run still saw it: the fix is only on its branch until merged
    score: float = 0.0
    rank_reason: str = ""
    ranked_by: str = ""

    @property
    def fix_args(self) -> list[str]:
        if self.status == "fixed":
            return ["pr", self.fix_run]
        return ["run", "--attacks", self.attack] if self.source == "attack" else ["fix", self.id]


def _id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


def _when(path: Path) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))


def collect(root: Path) -> list[Issue]:
    """Every issue, newest sighting kept. A fixed one stays in the list, marked fixed with its branch (a fix lives on
    its own branch until you merge it, so a later run on your branch may still see the issue: that is noted)."""
    found: dict[str, Issue] = {}
    seen: dict[str, Issue] = {}           # never forgets: a fixed attack that later survives is still shown, as fixed
    fixes: dict[str, dict] = {}           # issue key -> the fix that committed it
    seen_at: dict[str, float] = {}
    runs = sorted((root / "runs").iterdir(), key=lambda p: p.stat().st_mtime) if (root / "runs").exists() else []

    def sight(k: str, x: Issue, when: float) -> None:
        found[k] = seen[k] = x
        seen_at[k] = when

    for run in runs:  # oldest first, so a newer sighting replaces an older one
        when = run.stat().st_mtime
        done = run / "fix.json"
        if done.exists():
            f = json.loads(done.read_text())
            if f.get("status") == "fixed":
                fixes[f"{f.get('source')}:{f.get('key')}"] = {"branch": f.get("branch", ""), "commit": f.get("commit", ""),
                                                              "run": run.name, "at": when}
        hunt = run / "issues.json"
        if hunt.exists():
            for x in json.loads(hunt.read_text()).get("issues", []):
                key = x.get("key") or x.get("title", "")
                sight(f"hunt:{key}", Issue(
                    id=_id("hunt", key), source="hunt", title=x.get("title", ""), detail=x.get("detail", ""),
                    severity=x.get("severity", "medium"), screen=x.get("screen", ""), run=run.name, found=_when(hunt),
                    evidence=[e for e in x.get("evidence", []) if not e.endswith((".png", ".xml", ".txt"))][:6],
                    steps=x.get("steps", []), key=key), when)
        results = run / "results.json"
        summary = run / "summary.json"
        rows = []
        if results.exists():
            rows = [(r.get("attack"), r.get("outcome"), r.get("crash_signature") or r.get("reason", ""), r.get("screen", ""))
                    for r in json.loads(results.read_text())]
        elif summary.exists():
            s = json.loads(summary.read_text())
            for o in s.get("outcomes", []):
                rows.append((o.get("attack"), o.get("before"), o.get("before_detail", ""),
                             (o.get("metrics") or {}).get("screen", "")))
                if o.get("status") == "fixed" and o.get("attack"):
                    fixes[f"attack:{o['attack']}"] = {"branch": s.get("branch", ""), "commit": o.get("commit", ""),
                                                      "run": run.name, "at": when}
        for name, outcome, detail, screen in rows:
            if not name or not outcome:
                continue
            if outcome in NOT_PROBLEMS:
                found.pop(f"attack:{name}", None)  # survived since: no longer open (a fixed one comes back below)
                continue
            sight(f"attack:{name}", Issue(
                id=_id("attack", name), source="attack", title=f"{name.replace('_', ' ')}: {outcome.replace('_', ' ')}",
                detail=str(detail)[:400], severity=outcome, screen=screen, run=run.name,
                found=_when(results if results.exists() else summary), attack=name, key=name), when)
    ux = root / "ux-audit.json"
    if ux.exists():
        for f in json.loads(ux.read_text()).get("findings", []):
            if f.get("status") == "applied":
                continue
            sight(f"ux:{f['id']}", Issue(
                id=_id("ux", f["id"]), source="ux", title=f.get("title", ""),
                detail=(f.get("problem", "") + " Suggested: " + f.get("change", "")).strip(),
                severity=f.get("severity", "medium"), screen=(f.get("screens") or [""])[0],
                evidence=f.get("evidence", [])[:6], files=f.get("files", []), observed=f.get("observed", True),
                run="ux audit", found=_when(ux), key=f["id"]), ux.stat().st_mtime)
    for k, fx in fixes.items():
        x = seen.get(k)
        if not x:
            continue
        x.status, x.branch, x.commit, x.fix_run = "fixed", fx["branch"], fx["commit"], fx["run"]
        x.seen_after_fix = seen_at.get(k, 0) > fx["at"] and x.source != "ux"
        found[k] = x
    return list(found.values())


# ---- ranking ---------------------------------------------------------------------------------------

def _rank_file(root: Path) -> Path:
    return root / "issues-rank.json"


def _fallback(x: Issue) -> tuple[float, str]:
    score = SEVERITY_WEIGHT.get(x.severity, 0.4) * (0.5 if not x.observed else 1.0)
    return round(score, 3), f"by severity ({x.severity})" + ("" if x.observed else ", suspected only")


def _state(x: Issue, app: str) -> str:
    return (f"Android app: {app}\nIssue found by droid-monkey ({x.source}"
            + ("" if x.observed else ", suspected from the code only, not seen on a device") + f"): {x.title}\n"
            f"What happens: {x.detail}\nOn screen: {x.screen or 'unknown'}\nEvidence: " + "; ".join(x.evidence[:5]))


def rank(root: Path, issues: list[Issue], app: str = "", use_jev: bool = True) -> tuple[list[Issue], str]:
    """Scores from the cache, Jev for new issues, severity when Jev is unavailable. Returns (sorted, note)."""
    cache = json.loads(_rank_file(root).read_text()) if _rank_file(root).exists() else {}
    note, jev = "", None
    todo = [x for x in issues if x.id not in cache and x.status != "fixed"]  # a fixed issue needs no ranking
    if todo and use_jev:
        try:
            from .jev import Jev, JevUnavailable
            from .tracing import load_keys
            load_keys()
            jev = Jev()
        except Exception as e:  # noqa: BLE001 - no key, no SDK: rank by severity and say so
            note = f"Jev unavailable ({e}); ranked by severity"
    for x in todo:
        if jev is not None:
            try:
                impact = jev.choose(_state(x, app), "How badly does this issue hurt the app's users?", IMPACT)
                real = jev.judge(_state(x, app), {"real": "This is a real bug users would hit, not a test artefact "
                                                          "or intended behaviour."})["real"]
                level = max(impact, key=impact.get) if impact else "medium"
                score = sum(IMPACT_WEIGHT[k] * p for k, p in impact.items() if k in IMPACT_WEIGHT) * (0.4 + 0.6 * real)
                cache[x.id] = {"score": round(score, 3), "by": "jev",
                               "reason": f"{level} impact ({impact.get(level, 0):.0%}); real bug {real:.0%}"}
                continue
            except Exception as e:  # noqa: BLE001
                note = f"Jev failed on some issues ({str(e)[:80]}); those ranked by severity"
        score, reason = _fallback(x)
        cache[x.id] = {"score": score, "by": "severity", "reason": reason}
    _rank_file(root).write_text(json.dumps(cache, indent=1))
    for x in issues:
        r = cache.get(x.id) or {}
        x.score, x.rank_reason, x.ranked_by = r.get("score", 0.0), r.get("reason", ""), r.get("by", "")
    return sorted(issues, key=lambda x: (x.status == "fixed", -x.score)), note


def rerank(root: Path) -> None:
    """Forget cached scores so every issue is ranked again."""
    _rank_file(root).unlink(missing_ok=True)


def as_dict(x: Issue) -> dict:
    return {**asdict(x), "fix_args": x.fix_args}
