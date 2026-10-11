"""Turn a screen map into attack scenarios, with a reason each. The planner reorders these from Block 3 on."""

from dataclasses import dataclass

from .config import Target
from .explorer import ScreenMap


@dataclass
class Candidate:
    attack: str
    screen: str
    action: str | None
    settle_seconds: float
    score: float
    reason: str

    def target(self, smap: ScreenMap) -> Target:
        info = smap.screens[self.screen]
        return Target(name=self.screen, steps=list(info.path), fill=info.has_form)


def generate(smap: ScreenMap) -> list[Candidate]:
    out: list[Candidate] = []
    for s in smap.screens.values():
        depth = len([p for p in s.path if p.startswith("tap")])

        # Process death: forms first (most to lose), then deep screens (does the app come back here?).
        if s.has_form:
            out.append(Candidate("process_death", s.id, None, 2, 10 + len(s.fields),
                                 f"'{s.title}' has {len(s.fields)} input fields that must survive the app being killed"))
        elif depth >= 1:
            out.append(Candidate("process_death", s.id, None, 2, 2 + depth,
                                 f"'{s.title}' is {depth} taps deep; the app should return here after being killed"))

        # Rotation: forms (state across recreation) and work started by a tap (callbacks into a dead Activity).
        if s.has_form:
            out.append(Candidate("rotation", s.id, None, 3, 5 + len(s.fields),
                                 f"'{s.title}' form must keep {len(s.fields)} fields across recreation"))
        for a in s.actions:
            if a.showed_progress or a.settle_seconds >= 1.5:
                out.append(Candidate("rotation", s.id, f"tap {a.selector}", a.settle_seconds + 2, 8 + a.settle_seconds,
                                     f"'{a.label}' starts {a.settle_seconds:.0f}s of background work; rotating mid-flight "
                                     "recreates the Activity under it"))

        # Network loss: actions on forms (submits) and actions that do slow work.
        for a in s.actions:
            if a.effect in ("crash", "left_app", "permission"):
                continue
            score = (6 if s.has_form else 0) + (4 if a.showed_progress else 0) + min(a.settle_seconds, 5)
            if score >= 4:
                out.append(Candidate("network_loss", s.id, f"tap {a.selector}", max(4, a.settle_seconds + 2), score,
                                     f"'{a.label}' " + ("submits a form" if s.has_form else "does slow work")
                                     + " and may need the network"))

        # Permission revoke: anything that asked for a runtime permission.
        for a in s.actions:
            if a.effect == "permission":
                perms = ", ".join(p.rsplit(".", 1)[-1] for p in a.permissions) or "a runtime permission"
                out.append(Candidate("permission_revoke", s.id, f"tap {a.selector}", 4, 10,
                                     f"'{a.label}' asks for {perms}; using it again after a revoke must not crash"))

    return sorted(out, key=lambda c: (c.attack, -c.score))


def best(candidates: list[Candidate], attack: str) -> Candidate | None:
    return next((c for c in candidates if c.attack == attack), None)
