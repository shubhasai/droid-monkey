"""Turn a screen map into built-in attack candidates, with a reason each, and fill the built-in templates.

The rules are generic: forms and deep screens for process death, in-flight work and forms for rotation, form submits
and slow actions for network loss, permission-asking actions for permission revoke. The planner reorders them.
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

        if s.has_form:
            out.append(Candidate("process_death", s.id, None, 0, 10 + len(s.fields),
                                 f"'{s.title}' has {len(s.fields)} input fields that must survive the app being killed"))
        elif depth >= 1:
            out.append(Candidate("process_death", s.id, None, 0, 2 + depth,
                                 f"'{s.title}' is {depth} taps deep; the app should return here after being killed"))

        if s.has_form:
            out.append(Candidate("rotation", s.id, None, 0, 5 + len(s.fields),
                                 f"'{s.title}' form must keep {len(s.fields)} fields across recreation"))
        for a in s.actions:
            if a.showed_progress or a.settle_seconds >= 1.5:
                out.append(Candidate("rotation", s.id, a.selector, a.settle_seconds,
                                     8 + a.settle_seconds,
                                     f"'{a.label}' starts {a.settle_seconds:.0f}s of background work; rotating mid-flight "
                                     "recreates the Activity under it"))

        for a in s.actions:
            if a.effect in ("crash", "left_app", "permission"):
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

    return sorted(out, key=lambda c: (c.attack, -c.score))


def best(candidates: list[Candidate], attack: str) -> Candidate | None:
    return next((c for c in candidates if c.attack == attack), None)
