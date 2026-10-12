"""`explore --mode jev`: fast exploration with TypeSafe's Jev, a "System One" model that picks from a list in
70-500 ms instead of generating text.

The model-free explorer taps every element of every screen and relaunches the app before each tap. On a real app
(Wikipedia: onboarding, hundreds of rows) that is slow and wanders. Here:
- the explorer stays on the screen it is exploring and only replays the path when the app ended up elsewhere
  (after trying Back first);
- for each new screen, one Jev call ranks every tappable element by how likely it leads into the user flows found
  by `analyze`, and only the top few are tried;
- in the same call, Jev judges whether tapping each of those would act on the live backend (sign up, log in,
  publish, pay...), on top of the word list in safety.py; anything judged so is never tapped.

Without TYPESAFE_API_KEY (or if Jev fails), elements keep their screen order: the same faster explorer, unranked.
"""

import os
import time

from .driver import NavigationError
from .explorer import ActionInfo, Explorer, ScreenInfo, _group, _selector, _similar
from .tools import ui
from .tools.adb import AdbError

OUTWARD_THRESHOLD = 0.5


class Jev:
    """The two questions droid-monkey asks Jev about a screen: what to tap next, and what never to tap."""

    def __init__(self, flows_text: str):
        from typesafe_sdk import TypeSafeClient
        self.client = TypeSafeClient(timeout=20)
        self.flows = flows_text
        self.calls, self.seconds = 0, 0.0

    def _ask(self, state: str, questions: dict):
        start = time.time()
        resp = self.client.system_one(state=state, questions=questions)
        self.calls += 1
        self.seconds += time.time() - start
        return resp

    def rank(self, screen_title: str, options: dict[str, str]) -> dict[str, float]:
        """Probability that tapping each option leads into one of the user flows."""
        from typesafe_sdk import Choice
        resp = self._ask(f"Screen: {screen_title}", {"next": Choice(
            instructions="A tester is mapping this Android app's screens. Which element should they tap next to reach "
                         "screens in these user flows? Prefer navigation into new screens over list rows that all "
                         "lead to the same kind of screen.\n" + self.flows,
            criteria=options)})
        return dict(resp.answers["next"].probabilities or {})

    def outward(self, screen_title: str, labels: dict[str, str]) -> dict[str, float]:
        """Probability that tapping each element acts on the live backend."""
        from typesafe_sdk import Noul
        resp = self._ask(f"Screen: {screen_title}", {key: Noul(instructions=(
            f"Tapping '{label}' acts on the real world through the app's live backend: it creates an account, signs "
            "up, logs in, publishes or submits content, sends a message, posts, pays, buys, donates or deletes "
            "something.")) for key, label in labels.items()})
        return {key: float(resp.answers[key].noul or 0.0) for key in labels}


def flows_text(flows: list[dict]) -> str:
    return "\n".join(f"- {f.get('name')}: {f.get('description', '')} Steps: {'; '.join(f.get('steps', []))}"
                     for f in flows[:10]) or "- the app's main features"


class JevExplorer(Explorer):
    def __init__(self, ctx, flows: list[dict], top_k: int = 3, max_screens: int = 30, budget_seconds: float = 1500,
                 max_depth: int = 10, save_path=None):
        super().__init__(ctx, max_screens=max_screens, max_actions=255, budget_seconds=budget_seconds)
        self.top_k, self.max_depth, self.save_path = top_k, max_depth, save_path
        self.jev = None
        if os.environ.get("TYPESAFE_API_KEY"):
            try:
                self.jev = Jev(flows_text(flows))
            except Exception as e:  # SDK missing or misconfigured: explore unranked
                self.notes.append(f"jev unavailable ({e}); exploring without ranking")
        else:
            self.notes.append("TYPESAFE_API_KEY not set: exploring without Jev ranking")
        self.replays = 0  # relaunches needed to get back to a screen (what made breadth-first exploring loop)

    # Stay on the screen; only replay when the app is elsewhere (Back first, it is cheaper).
    def go_to(self, info: ScreenInfo) -> ui.Screen:
        screen = ui.dump(self.adb)
        if _similar(screen.fingerprint(self.cfg.package), frozenset(info.fingerprint)):
            return screen
        self.adb.shell("input keyevent 4")
        screen, _, _ = self.d.settle(timeout=4)
        if _similar(screen.fingerprint(self.cfg.package), frozenset(info.fingerprint)):
            return screen
        self.replays += 1
        return super().go_to(info)

    def candidates(self, info: ScreenInfo, screen: ui.Screen) -> list[ActionInfo]:
        nodes = self.clickables(screen)
        actions = [ActionInfo(selector=_selector(n, screen), label=self._label(screen, n), group=_group(n)) for n in nodes]
        if not self.jev or len(actions) <= 1:
            return actions[: self.top_k]
        options = {f"e{i}": f"{a.label[:80]} [{a.selector[:60]}]" for i, a in enumerate(actions)}
        try:
            probs = self.jev.rank(info.title, options)
            ranked = sorted(range(len(actions)), key=lambda i: -probs.get(f"e{i}", 0.0))
            shortlist = ranked[: self.top_k + 3]
            outward = self.jev.outward(info.title, {f"e{i}": actions[i].label[:80] for i in shortlist})
        except Exception as e:
            self.notes.append(f"{info.id}: jev failed ({str(e)[:120]}); taking elements in screen order")
            return actions[: self.top_k]
        chosen = []
        for i in shortlist:
            a = actions[i]
            if outward.get(f"e{i}", 0.0) >= OUTWARD_THRESHOLD:
                a.effect = "blocked"  # never tapped, never an attack trigger
                self.notes.append(f"{info.id}: jev judged '{a.label[:40]}' a live-backend action; not tapped")
                continue
            chosen.append(a)
            if len(chosen) == self.top_k:
                break
        blocked = [a for a in actions if a.effect == "blocked"]
        return chosen + blocked

    def explore(self):
        """Depth first: a tap that opens a new screen is explored right away, from where the app already is, so the
        app is only relaunched and the path replayed when Back cannot return to a screen."""
        self.d.fresh(clear_data=self.clear_data)
        self.d.settle()
        root, self.baseline, _ = self.d.settle()
        self.map.baseline_seconds = self.baseline
        self.map.mode = "jev" if self.jev else "fast"
        self.explored: set[str] = set()
        self.replays = 0
        self._visit(self.register(root, []), depth=0)
        self.map.seconds = round(time.time() - self.started, 1)
        self.notes.append(f"{len(self.map.screens)} screens, {self.replays} relaunches to replay a path")
        if self.jev:
            self.notes.append(f"jev: {self.jev.calls} calls, {self.jev.seconds / max(1, self.jev.calls):.2f}s each")
        self._save()
        try:
            self.device.restore()
        except AdbError as e:
            self.notes.append(f"restore: {e}")
        return self.map

    def _save(self) -> None:
        if self.save_path:
            self.map.seconds = round(time.time() - self.started, 1)
            self.map.save(self.save_path)

    def _visit(self, sid: str, depth: int) -> None:
        if sid in self.explored or depth > self.max_depth or len(self.explored) >= self.max_screens \
                or time.time() - self.started > self.budget:
            return
        self.explored.add(sid)
        info = self.map.screens[sid]
        try:
            screen = self.go_to(info)
        except (NavigationError, AdbError) as e:
            self.notes.append(f"{sid}: {e}")
            return
        info.actions = self.candidates(info, screen)
        self._save()
        for action in info.actions:
            if action.effect == "blocked" or time.time() - self.started > self.budget:
                continue
            try:
                new_sid = self.try_action(info, action)
            except NavigationError as e:  # includes a tap the live-backend guard refused
                action.effect = "blocked" if "live backend" in str(e) else "not_found"
                self.notes.append(f"{info.id} -> {action.label[:40]}: {str(e)[:120]}")
                continue
            except AdbError as e:
                self.notes.append(f"{info.id} -> {action.label[:40]}: {str(e)[:120]}")
                continue
            if new_sid and new_sid not in self.explored:
                self._visit(new_sid, depth + 1)  # the app is on the new screen now: no relaunch needed
