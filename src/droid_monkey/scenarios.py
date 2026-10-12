"""Turn a screen map into built-in attack candidates, with a reason each, and fill the built-in templates.

The rules are generic: forms and deep screens for process death, in-flight work and forms for rotation, form submits
and slow actions for network loss, permission-asking actions for permission revoke, busy and deep screens for
low memory (leaks), the slowest action under CPU load for performance, and the launch screen for startup. The planner reorders them.
"""

from dataclasses import dataclass

from .explorer import ScreenMap


@dataclass
class Candidate:
    attack: str
    screen: str
    action: str | None
    work_seconds: float
    score: float
    reason: str
    permission: str | None = None

    def values(self) -> dict:
        """Placeholder values for this attack's built-in template."""
        in_flight = round(min(2.0, max(0.3, self.work_seconds / 4)), 1) if self.action else None
        return {
            "screen": self.screen,
            "action": self.action,
            "permission": self.permission,
            "in_flight": in_flight,
            "settle": round(min(30.0, max(4.0, self.work_seconds + 3)), 1),
        }


def generate(smap: ScreenMap) -> list[Candidate]:
    out: list[Candidate] = []
    for s in smap.screens.values():
        depth = len([p for p in s.path if p.startswith("tap")])

        # Forms always come first; path depth only orders the rest (recorded paths can be long, so it is capped).
        if s.has_form:
            out.append(Candidate("process_death", s.id, None, 0, 100 + len(s.fields),
                                 f"'{s.title}' has {len(s.fields)} input fields that must survive the app being killed"))
        elif depth >= 1:
            out.append(Candidate("process_death", s.id, None, 0, 2 + min(depth, 4),
                                 f"'{s.title}' is {depth} taps deep; the app should return here after being killed"))

        if s.has_form:
            out.append(Candidate("rotation", s.id, None, 0, 50 + len(s.fields),
                                 f"'{s.title}' form must keep {len(s.fields)} fields across recreation"))
        for a in s.actions:
            if a.effect == "blocked":
                continue
            if a.showed_progress or a.settle_seconds >= 1.5:
                out.append(Candidate("rotation", s.id, a.selector, a.settle_seconds,
                                     60 + a.settle_seconds,  # in-flight work outranks forms: callbacks into a dead screen
                                     f"'{a.label}' starts {a.settle_seconds:.0f}s of background work; rotating mid-flight "
                                     "recreates the Activity under it"))

        for a in s.actions:
            if a.effect in ("crash", "left_app", "permission", "blocked"):
                continue
            score = (6 if s.has_form else 0) + (4 if a.showed_progress else 0) + min(a.settle_seconds, 5)
            if score >= 4:
                out.append(Candidate("network_loss", s.id, a.selector, a.settle_seconds, score,
                                     f"'{a.label}' " + ("submits a form" if s.has_form else "does slow work")
                                     + " and may need the network"))

        for a in s.actions:
            if a.effect == "permission" and a.permissions:
                perm = a.permissions[0]
                out.append(Candidate("permission_revoke", s.id, a.selector, 1, 10,
                                     f"'{a.label}' asks for {perm.rsplit('.', 1)[-1]}; using it again after a revoke "
                                     "must not crash", permission=perm))

        # Leaks: screens doing async work hold callbacks that can outlive the Activity; deeper screens hold more.
        busy = any(a.showed_progress or a.settle_seconds >= 1.5 for a in s.actions)
        out.append(Candidate("low_memory", s.id, None, 0, 3 + min(depth, 4) + (3 if busy else 0) + (2 if s.has_form else 0),
                             f"'{s.title}' is recreated 3 times and trimmed; destroyed copies of it must be freed"
                             + (" (it runs background work that can hold on to them)" if busy else "")))

        # Performance: the slowest thing a user can do on the screen, under CPU load; lists scroll too.
        usable = [a for a in s.actions if a.effect not in ("crash", "left_app", "permission", "not_found", "blocked")]
        slow = max(usable, key=lambda a: (a.showed_progress, a.settle_seconds), default=None)
        work = slow.settle_seconds if slow else 0
        out.append(Candidate("performance", s.id, slow.selector if slow else None, work,
                             1 + min(work, 5) * 2 + (2 if slow and slow.showed_progress else 0) + min(len(s.actions), 10) / 5,
                             f"'{s.title}': scroll and " + (f"'{slow.label}' ({work:.1f}s of work)" if slow else "idle")
                             + " under CPU load must not freeze the UI"))

    # Startup: one candidate, the screen the app opens on (the shortest recorded path).
    if smap.screens:
        first = min(smap.screens.values(), key=lambda s: len(s.path))
        out.append(Candidate("slow_startup", first.id, None, 0, 10,
                             f"the app opens on '{first.title}'; cold and hot starts must reach its first frame quickly"))

    return sorted(out, key=lambda c: (c.attack, -c.score))


def best(candidates: list[Candidate], attack: str) -> Candidate | None:
    return next((c for c in candidates if c.attack == attack), None)


def top(candidates: list[Candidate], attack: str, n: int) -> list[Candidate]:
    """The n best candidates for an attack, on n different screens."""
    out, seen = [], set()
    for c in candidates:
        if c.attack == attack and c.screen not in seen:
            out.append(c)
            seen.add(c.screen)
            if len(out) == n:
                break
    return out


def attack_name(base: str, screen: str, rank: int) -> str:
    """The best screen keeps the attack's own name; the others are named after their screen."""
    if rank == 0:
        return base
    import re
    return re.sub(r"[^a-z0-9_]", "_", f"{base}_on_{screen}".lower())[:60]


def builtin_attacks(smap: ScreenMap, known, screens_per_attack: int = 2) -> list[dict]:
    """Every built-in attack rendered for this app on its best `screens_per_attack` screens:
    [{name, base, screen, reason, spec}]. Startup has a single target, the launch screen."""
    from . import specs as specs_mod
    candidates = generate(smap)
    items = []
    for base, template in specs_mod.builtin_templates().items():
        for rank, c in enumerate(top(candidates, base, screens_per_attack)):
            try:
                spec = specs_mod.render(template, c.values(), known)
            except specs_mod.SpecError:
                continue
            spec.name = attack_name(base, c.screen, rank)
            items.append({"name": spec.name, "base": base, "screen": c.screen, "reason": c.reason, "spec": spec})
    return items


def named_attack(smap: ScreenMap, known, name: str) -> dict | None:
    """A built-in attack by the name a report gave it, even when the current map ranks different screens first:
    `low_memory_on_detailscreen` is low_memory on detailscreen, whatever the best screens are today."""
    import re
    from . import specs as specs_mod
    templates = specs_mod.builtin_templates()
    base = next((b for b in sorted(templates, key=len, reverse=True) if name == b or name.startswith(b + "_on_")), None)
    if not base:
        return None
    wanted = name[len(base) + 4:] if name != base else None
    norm = lambda x: re.sub(r"[^a-z0-9_]", "_", x.lower())
    options = [c for c in generate(smap) if c.attack == base and (wanted is None or norm(c.screen) == wanted)]
    for c in sorted(options, key=lambda c: -c.score):
        try:
            spec = specs_mod.render(templates[base], c.values(), known)
        except specs_mod.SpecError:
            continue
        spec.name = name
        return {"name": name, "base": base, "screen": c.screen, "reason": c.reason, "spec": spec}
    return None

