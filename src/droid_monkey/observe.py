"""The verdict for one attack. Deterministic only: crash buffer and field diffs. A model may explain it, never decide it."""

from dataclasses import dataclass, field

from .tools.logcat import Crash

SURVIVED, CRASHED, LOST_STATE, SKIPPED = "survived", "crashed", "lost_state", "skipped"


@dataclass
class Verdict:
    outcome: str
    reason: str
    crash: Crash | None = None
    lost_fields: dict[str, tuple[str, str]] = field(default_factory=dict)


def classify(
    crashes: list[Crash],
    before: dict[str, str],
    after: dict[str, str],
    screen_restored: bool = True,
) -> Verdict:
    if crashes:
        c = crashes[0]
        return Verdict(CRASHED, f"{c.exception}: {c.message}"[:300], crash=c)

    lost = {k: (v, after.get(k, "")) for k, v in before.items() if v and after.get(k, "") != v}
    if lost:
        return Verdict(LOST_STATE, f"{len(lost)} of {sum(1 for v in before.values() if v)} filled fields changed or emptied",
                       lost_fields=lost)
    if not screen_restored:
        return Verdict(LOST_STATE, "the app came back on a different screen than the user left")
    return Verdict(SURVIVED, "no crash, no ANR, and every filled field kept its value")
