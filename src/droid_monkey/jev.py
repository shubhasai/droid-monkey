"""TypeSafe's Jev: a "System One" model that picks from a list or judges a statement in 70-500 ms, without
generating text (TYPESAFE_API_KEY, `pip install typesafe-sdk`).

Kept for scoring and prioritising (which attack to run first, how likely a finding is real, whether a button acts on
a live backend). It was tried as the explorer's tap chooser and removed there: labels alone were not enough to pick
good taps, and fast screen reads gave most of the speed-up.
"""

import os
import time


class JevUnavailable(RuntimeError):
    pass


class Jev:
    def __init__(self, timeout: float = 20):
        if not os.environ.get("TYPESAFE_API_KEY"):
            raise JevUnavailable("TYPESAFE_API_KEY is not set: add it to ~/.droid-monkey/.env")
        try:
            from typesafe_sdk import TypeSafeClient
        except ImportError as e:
            raise JevUnavailable("typesafe-sdk is not installed") from e
        self.client = TypeSafeClient(timeout=timeout)
        self.calls, self.seconds = 0, 0.0

    def _ask(self, state: str, questions: dict):
        start = time.time()
        resp = self.client.system_one(state=state, questions=questions)
        self.calls += 1
        self.seconds += time.time() - start
        return resp

    def choose(self, state: str, instructions: str, options: dict[str, str]) -> dict[str, float]:
        """Probability of each option (up to 255), e.g. which attack is most likely to expose a real bug."""
        from typesafe_sdk import Choice
        resp = self._ask(state, {"q": Choice(instructions=instructions, criteria=options)})
        return dict(resp.answers["q"].probabilities or {})

    def judge(self, state: str, statements: dict[str, str]) -> dict[str, float]:
        """Probability that each statement is true about `state`, evaluated in parallel."""
        from typesafe_sdk import Noul
        resp = self._ask(state, {k: Noul(instructions=v) for k, v in statements.items()})
        return {k: float(resp.answers[k].noul or 0.0) for k in statements}
