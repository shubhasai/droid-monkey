"""`droid-monkey run`: the agent loop as a LangGraph graph.

    setup -> plan -> pick -> attack -> decide -> diagnose -> approve -> fix -> verify -> commit -> pick ... -> report

Before the first fix of each attack the run pauses at `approve` (a LangGraph interrupt) with the issue, the measured
evidence and the suggested fix; the developer answers yes, no or all. `--auto-fix` skips the question.

The model plans the order of attacks and makes the decide step (diagnose or move on; after a failed fix, retry with a
revised diagnosis or give up). Code enforces the caps, decides every verdict (crash buffer, field diffs, replay of the
identical spec, the new test), and makes every commit. Progress is checkpointed to SQLite after every node, so a
killed run can resume. Every node, model call and tool call is traced in neatlogs.
"""

import json
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import TypedDict

from . import paths, scenarios as scenarios_mod, specs as specs_mod, tracing
from .driver import Context
from .explorer import ScreenMap
from .profile import AppProfile
from .runner import SpecRunner
from .tools import gradle, git

MAX_FIX_ATTEMPTS = 2


class RunState(TypedDict, total=False):
    run_id: str
    branch: str
    last_good: str
    queue: list[dict]          # attack items: {name, reason, spec_yaml}
    index: int
    current: dict
    result: dict
    diagnosis: dict | None
    coder_session: str | None
    coder_cost: float
    attempts: int
    failure_note: str
    decision: str
    decision_reason: str
    changed: list[str]
    tests: list[str]
    verify: dict
    forced_failure_done: bool
    attack_started: float
    diag_tokens: int
    coder_tokens: int
    first_diagnosis: dict
    outcomes: list[dict]
    approval: str
    auto_fix_all: bool
    log: list[str]


def _subject(summary: str, limit: int = 72) -> str:
    """Commit subject: the summary cut at a word boundary, without a trailing period."""
    text = " ".join(summary.split()).rstrip(".")
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:")


class Loop:
    def __init__(self, ctx: Context, smap: ScreenMap, profile: AppProfile, saved: dict, known: specs_mod.Known,
                 only: list[str] | None, coder_model: str, echo=print, fail_first_fix: bool = False,
                 naive: bool = False, compile_tool: bool = True, auto_fix: bool = False):
        self.compile_tool = compile_tool
        self.auto_fix = auto_fix
        self.ctx, self.cfg, self.smap, self.profile = ctx, ctx.cfg, smap, profile
        self.naive = naive  # baseline for the before/after comparison: no diagnosis, the coder gets only the failure
        self.fail_first_fix = fail_first_fix
        self.saved, self.known, self.only = saved, known, only
        self.coder_model = coder_model
        self.echo = echo
        self._llm = None

    # ---- helpers ---------------------------------------------------------------------------------

    def llm(self):
        if self._llm is None:
            from .llm import chat_model
            self._llm = chat_model(self.cfg.llm)
        return self._llm

    def ask_json(self, prompt: str) -> dict:
        reply = self.llm().invoke(prompt)
        m = re.search(r"\{.*\}|\[.*\]", reply.text, flags=re.S)
        return json.loads(m.group(0)) if m else {}

    def with_watchdog(self, state: RunState, what: str, fn):
        """Run fn(); if adb or the emulator hangs, restart the emulator, reinstall the app, and retry once."""
        from .tools import emulator
        from .tools.adb import AdbHang
        from .tools.device import Device
        try:
            return fn(), []
        except AdbHang as e:
            log = self.note(state, f"watchdog: {what} hung ({e}); restarting the emulator")
            adb = emulator.restart(self.ctx.adb, self.cfg.avd or emulator.list_avds()[0])
            self.ctx.adb, self.ctx.device = adb, Device(adb, self.cfg.package)
            build = gradle.build(self.cfg.app_dir, self.cfg.module, self.cfg.variant, java_home=self.cfg.java_home)
            if build.ok and build.apk:
                gradle.install(adb, build.apk, package=self.cfg.package, source_dir=self.cfg.app_dir)
            log = log + [f"{time.strftime('%H:%M:%S')} watchdog: emulator back, app reinstalled; retrying {what}"]
            self.echo(log[-1])
            return fn(), log[len(state.get('log', [])):]

    def spec(self, item: dict) -> specs_mod.Spec:
        return specs_mod.parse(item["spec_yaml"], self.known)

    def note(self, state: RunState, line: str) -> list[str]:
        self.echo(line)
        return state.get("log", []) + [f"{time.strftime('%H:%M:%S')} {line}"]

    # ---- nodes -----------------------------------------------------------------------------------

    def setup(self, state: RunState) -> RunState:
        branch = f"droid-monkey/{state['run_id']}"
        base = git.start_run_branch(self.cfg.app_dir, branch, self.cfg.module)
        build = gradle.build(self.cfg.app_dir, self.cfg.module, self.cfg.variant, java_home=self.cfg.java_home)
        if not build.ok or not build.apk:
            raise RuntimeError(f"the app does not build before any fix:\n{build.log_tail}")
        gradle.install(self.ctx.adb, build.apk, package=self.cfg.package, source_dir=self.cfg.app_dir)
        log = self.note(state, f"setup: branch {branch} from {base[:8]}, built and installed in {build.seconds:.0f}s")
        return {"branch": branch, "last_good": base, "index": 0, "outcomes": [], "log": log}

    def plan(self, state: RunState) -> RunState:
        candidates = scenarios_mod.generate(self.smap)
        items = []
        for name, template in specs_mod.builtin_templates().items():
            best = scenarios_mod.best(candidates, name)
            if best is None:
                continue
            try:
                spec = specs_mod.render(template, best.values(), self.known)
            except specs_mod.SpecError:
                continue
            items.append({"name": name, "reason": best.reason, "spec_yaml": spec.to_yaml()})
        for name, spec in self.saved.items():
            items.append({"name": name, "reason": spec.description, "spec_yaml": spec.to_yaml()})
        if self.only:
            items = [i for i in items if i["name"] in self.only]

        listing = "\n".join(f"- {i['name']}: {i['reason']}" for i in items)
        signals = "\n".join(f"- {s.kind} at {s.file}:{s.line}" for s in self.profile.signals) or "- none"
        prompt = (
            "You plan fault-injection attacks on an Android app; each failing attack is then diagnosed and fixed. "
            "Order these attacks so the ones most likely to expose a real bug come first, using the code risk "
            f"signals.\nAttacks:\n{listing}\nCode risk signals:\n{signals}\n"
            'Reply with ONLY a JSON array: [{"name": "...", "reason": "one sentence: why at this position"}]')
        try:
            ordered = self.ask_json(prompt)
            by_name = {i["name"]: i for i in items}
            queue = [{**by_name[o["name"]], "reason": o.get("reason", by_name[o["name"]]["reason"])}
                     for o in ordered if isinstance(o, dict) and o.get("name") in by_name]
            queue += [i for i in items if i["name"] not in {q["name"] for q in queue}]
        except Exception as e:  # an unusable plan falls back to the generated order
            queue = items
            self.echo(f"plan: model answer unusable ({e}); using the generated order")
        log = self.note(state, "plan:\n" + "\n".join(f"  {n + 1}. {q['name']}: {q['reason']}" for n, q in enumerate(queue)))
        return {"queue": queue, "log": log}

    def pick(self, state: RunState) -> RunState:
        i = state.get("index", 0)
        if i >= len(state["queue"]):
            return {"current": {}}
        item = state["queue"][i]
        return {"current": item, "attempts": 0, "diagnosis": None, "coder_session": None, "coder_cost": 0.0,
                "failure_note": "", "attack_started": time.time(), "diag_tokens": 0, "coder_tokens": 0, "log": self.note(state, f"\n== attack {i + 1}/{len(state['queue'])}: {item['name']}")}

    def attack(self, state: RunState) -> RunState:
        from .driver import NavigationError
        try:
            res, wlog = self.with_watchdog(state, "the attack",
                                           lambda: SpecRunner(self.ctx, self.spec(state["current"])).run())
        except NavigationError as e:  # e.g. an earlier fix changed the UI on the way to this screen
            result = {"attack": state["current"]["name"], "outcome": "unreachable", "reason": str(e)}
            return {"result": result, "log": self.note(state, f"attack {result['attack']}: unreachable ({e})")}
        detail = res.exception or res.reason
        line = f"attack {res.attack}: {res.outcome} ({detail})" + (f" at {res.crash_signature}" if res.crash_signature else "")
        state = {**state, "log": state.get("log", []) + wlog}
        return {"result": asdict(res), "log": self.note(state, line)}

    def decide(self, state: RunState) -> RunState:
        """Model chooses the next step; code checks it is allowed."""
        res, attempts = state["result"], state.get("attempts", 0)
        verifying = state.get("decision") in ("verify_failed", "build_failed")
        if not verifying and res["outcome"] == "unreachable":
            return {"decision": "next", "decision_reason": f"could not reach the attacked screen: {res['reason']}",
                    "log": self.note(state, "decide: next (screen unreachable; re-explore after this run)")}
        if not verifying and res["outcome"] == "survived":
            return {"decision": "next", "decision_reason": "the app survived this attack",
                    "log": self.note(state, "decide: next (survived)")}
        if verifying:
            options = ["retry", "give_up"] if attempts < MAX_FIX_ATTEMPTS else ["give_up"]
            situation = f"Fix attempt {attempts} of {MAX_FIX_ATTEMPTS} failed verification: {state.get('failure_note', '')[:800]}"
        else:
            options = ["diagnose", "skip"]
            situation = f"Attack result: {res['outcome']}: {res.get('exception') or res['reason']} {res.get('crash_signature', '')}"
        prompt = (f"You run an agent that attacks an Android app and fixes what breaks.\nAttack: {state['current']['name']}"
                  f"\n{situation}\nChoose the next step from {options}. Retry only if a revised diagnosis could plausibly"
                  ' succeed. Reply with ONLY JSON: {"decision": "...", "reason": "one sentence"}')
        try:
            d = self.ask_json(prompt)
            decision, reason = d.get("decision"), d.get("reason", "")
        except Exception as e:
            decision, reason = None, f"model answer unusable: {e}"
        if decision not in options:  # code enforces the allowed moves
            decision = options[0]
            reason = f"fallback rule ({reason or 'invalid choice'})"
        return {"decision": decision, "decision_reason": reason, "log": self.note(state, f"decide: {decision}: {reason}")}

    def diagnose(self, state: RunState) -> RunState:
        from .diagnoser import Diagnosis, diagnose
        if self.naive:
            d = Diagnosis(summary=f"survive {state['current']['name'].replace('_', ' ')}",
                          root_cause="Not diagnosed (naive baseline run).", files_to_change=[], fix_approach="",
                          test_plan="Add one regression test that fails before the fix and passes after it.")
            return {"diagnosis": d.to_dict(), "log": self.note(state, "diagnose: skipped (naive baseline)")}
        spec = self.spec(state["current"])
        screen = self.smap.screens.get(spec.screen)
        d = diagnose(self.cfg.app_dir, self.cfg.module, self.cfg.llm, state["result"], spec.__dict__,
                     asdict(screen) if screen else None, previous=state.get("diagnosis"),
                     failure_note=state.get("failure_note", ""))
        log = self.note(state, f"diagnose: {d.root_cause}\n  files: {d.files_to_change}\n  test: {d.test_plan[:160]}")
        out = {"diagnosis": d.to_dict(), "log": log,
               "diag_tokens": state.get("diag_tokens", 0) + d.input_tokens + d.output_tokens}
        if not state.get("diagnosis"):  # first diagnosis of this attack
            out["first_diagnosis"] = {**d.to_dict(), "attack": state["current"]["name"]}
        return out

    def approve(self, state: RunState) -> RunState:
        """Ask the developer before changing their code. Retries of an approved fix are not asked again."""
        if self.auto_fix or state.get("auto_fix_all") or state.get("attempts", 0) > 0:
            return {"approval": "yes"}
        from langgraph.types import interrupt
        from .approval import proposal
        answer = interrupt(proposal(state["current"], state["result"], state["diagnosis"] or {}))
        answer = answer if answer in ("yes", "no", "all") else "no"
        out: RunState = {"approval": answer, "log": self.note(state, f"approve: {answer} (asked the developer)")}
        if answer == "all":
            out["auto_fix_all"] = True
        return out

    def declined(self, state: RunState) -> RunState:
        """The developer said no: keep the diagnosis as a suggestion in the report and move on."""
        from .approval import proposal
        res = state["result"]
        outcome = {"attack": state["current"]["name"], "status": "declined", "before": res["outcome"],
                   "before_detail": res.get("crash_signature") or res["reason"], "diagnosis": state.get("diagnosis"),
                   "suggestion": proposal(state["current"], res, state.get("diagnosis") or {}),
                   "metrics": self.metrics(state)}
        return {"outcomes": state.get("outcomes", []) + [outcome], "index": state["index"] + 1,
                "log": self.note(state, "declined: suggested fix kept in the report, code unchanged")}

    def fix(self, state: RunState) -> RunState:
        from . import coder
        from .diagnoser import Diagnosis
        d = Diagnosis(**state["diagnosis"])
        spec = self.spec(state["current"])
        res = coder.fix(self.cfg.app_dir, self.cfg.module, coder.failure_text(state["result"], spec.__dict__), d,
                        self.coder_model, resume_session=state.get("coder_session"),
                        failure_note=state.get("failure_note", ""), variant=self.cfg.variant,
                        java_home=self.cfg.java_home, compile_tool=self.compile_tool)
        line = (f"fix: claude -p changed {res.changed or 'nothing'} in {res.seconds:.0f}s (${res.cost_usd:.2f} plan usage)"
                + (f"; discarded out-of-bounds edits {res.rejected}" if res.rejected else ""))
        return {"coder_session": res.session_id, "coder_cost": state.get("coder_cost", 0.0) + res.cost_usd,
                "coder_tokens": state.get("coder_tokens", 0) + res.tokens,
                "attempts": state.get("attempts", 0) + 1, "changed": res.changed, "tests": res.test_files,
                "log": self.note(state, line)}

    def verify(self, state: RunState) -> RunState:
        from . import verifier
        if not state.get("changed"):
            return {"decision": "verify_failed", "failure_note": "the coder changed no source files",
                    "log": self.note(state, "verify: nothing to verify, the coder changed no files")}
        from .driver import NavigationError
        from .tools.adb import AdbError
        try:
            v, wlog = self.with_watchdog(state, "verification",
                                         lambda: verifier.verify(self.ctx, self.spec(state["current"]), state.get("tests", [])))
        except (AdbError, NavigationError) as e:  # the harness failed, not the fix: do not spend a retry on it
            return {"decision": "give_up", "failure_note": f"verification could not run: {e}",
                    "log": self.note(state, f"verify: could not run ({str(e)[:200]}); giving up this attack")}
        state = {**state, "log": state.get("log", []) + wlog}
        if v.fixed and self.fail_first_fix and not state.get("forced_failure_done"):
            note = ("FORCED FAILURE (--fail-first-fix, for demonstrating recovery): this verification passed "
                    f"({v.reason}), but it is reported as failed so the retry path runs. Treat the fix as rejected by "
                    "review: re-examine the root cause and make the fix more robust.")
            return {"decision": "verify_failed", "failure_note": note, "forced_failure_done": True,
                    "log": self.note(state, "verify: passed, but --fail-first-fix forces this first verification to fail")}
        if v.fixed:
            return {"decision": "fixed", "failure_note": "", "verify": {"replay": asdict(v.replay), "test": asdict(v.test)},
                    "log": self.note(state, f"verify: FIXED: {v.reason}")}
        if not v.build_ok:
            return {"decision": "build_failed", "failure_note": verifier.build_failure_note(v),
                    "log": self.note(state, "verify: the fix does not build")}
        return {"decision": "verify_failed", "failure_note": v.reason, "log": self.note(state, f"verify: not fixed: {v.reason[:300]}")}

    def commit(self, state: RunState) -> RunState:
        item, d, res = state["current"], state["diagnosis"], state["result"]
        subject_src = (state.get("first_diagnosis") or d) if state.get("forced_failure_done") and state.get(
            "first_diagnosis", {}).get("attack") == item["name"] else d
        subject = _subject(subject_src.get("summary", ""))
        message = (f"fix({item['name']}): {subject}\n\n{d['root_cause']}\n\nFix: {d['fix_approach']}\n\n"
                   f"Attack: {item['name']}: {res['outcome']} ({res.get('exception') or res['reason']})\n"
                   f"Verified: the identical attack was replayed and survived; regression test {', '.join(state.get('tests', []))} passes.\n"
                   f"droid-monkey run {state['run_id']}")
        sha, checkpoint = git.commit(self.cfg.app_dir, state["changed"], message)
        outcome = {"attack": item["name"], "status": "fixed", "before": res["outcome"],
                   "before_detail": res.get("crash_signature") or res["reason"], "commit": sha, "checkpoint": checkpoint,
                   "files": state["changed"], "tests": state.get("tests", []), "attempts": state.get("attempts", 0),
                   "diagnosis": d, "coder_session": state.get("coder_session"), "metrics": self.metrics(state)}
        log = self.note(state, f"commit: {sha[:8]}" + (f" (Entire checkpoint {checkpoint})" if checkpoint else " (no Entire checkpoint)"))
        return {"last_good": sha, "outcomes": state.get("outcomes", []) + [outcome], "index": state["index"] + 1, "log": log}

    def give_up(self, state: RunState) -> RunState:
        """Restore the sources to the last good commit and record the attack as unfixed."""
        git.discard(self.cfg.app_dir, git.dirty_paths(self.cfg.app_dir, f"{self.cfg.module}/src"))
        git.reset_hard(self.cfg.app_dir, state["last_good"])
        build = gradle.build(self.cfg.app_dir, self.cfg.module, self.cfg.variant, java_home=self.cfg.java_home)
        if build.ok and build.apk:
            gradle.install(self.ctx.adb, build.apk, package=self.cfg.package, source_dir=self.cfg.app_dir)
        res = state["result"]
        outcome = {"attack": state["current"]["name"], "status": "unfixed", "before": res["outcome"],
                   "before_detail": res.get("crash_signature") or res["reason"], "attempts": state.get("attempts", 0),
                   "diagnosis": state.get("diagnosis"), "reason": state.get("failure_note", "")[:500],
                   "metrics": self.metrics(state)}
        return {"outcomes": state.get("outcomes", []) + [outcome], "index": state["index"] + 1,
                "log": self.note(state, f"give up: sources restored to {state['last_good'][:8]}")}

    def next_attack(self, state: RunState) -> RunState:
        res = state["result"]
        status = {"survived": "survived", "unreachable": "unreachable"}.get(res["outcome"], "skipped")
        outcome = {"attack": state["current"]["name"], "status": status, "before": res["outcome"],
                   "before_detail": res.get("crash_signature") or res["reason"], "reason": state.get("decision_reason", "")}
        return {"outcomes": state.get("outcomes", []) + [outcome], "index": state["index"] + 1}

    def metrics(self, state: RunState) -> dict:
        return {"seconds": round(time.time() - state.get("attack_started", time.time()), 1),
                "diag_tokens": state.get("diag_tokens", 0), "coder_tokens": state.get("coder_tokens", 0),
                "coder_cost_usd": round(state.get("coder_cost", 0.0), 3), "attempts": state.get("attempts", 0)}

    def report(self, state: RunState) -> RunState:
        out = paths.run_dir(self.cfg.app_dir, state["run_id"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text(json.dumps({
            "run_id": state["run_id"], "branch": state.get("branch"),
            "mode": "naive" if self.naive else "droid-monkey", "diagnoser_model": self.cfg.llm.model,
            "coder_model": self.coder_model, "coder_compile_tool": self.compile_tool,
            "outcomes": state.get("outcomes", [])}, indent=2))
        (out / "log.txt").write_text("\n".join(state.get("log", [])))
        fixed = sum(1 for o in state.get("outcomes", []) if o["status"] == "fixed")
        return {"log": self.note(state, f"\nreport: {fixed} fixed of {len(state.get('outcomes', []))} attacks -> {out}")}

    # ---- graph -----------------------------------------------------------------------------------

    def graph(self):
        from langgraph.graph import END, START, StateGraph
        g = StateGraph(RunState)
        for name in ("setup", "plan", "pick", "attack", "decide", "diagnose", "approve", "declined", "fix", "verify",
                     "commit", "give_up", "next_attack", "report"):
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "setup")
        g.add_edge("setup", "plan")
        g.add_edge("plan", "pick")
        g.add_conditional_edges("pick", lambda s: "attack" if s.get("current") else "report", ["attack", "report"])
        g.add_edge("attack", "decide")
        g.add_conditional_edges("decide", lambda s: {"diagnose": "diagnose", "retry": "diagnose",
                                                      "give_up": "give_up"}.get(s["decision"], "next_attack"),
                                ["diagnose", "give_up", "next_attack"])
        g.add_edge("diagnose", "approve")
        g.add_conditional_edges("approve", lambda s: "fix" if s.get("approval") in ("yes", "all") else "declined",
                                ["fix", "declined"])
        g.add_edge("declined", "pick")
        g.add_edge("fix", "verify")
        g.add_conditional_edges("verify", lambda s: {"fixed": "commit", "give_up": "give_up"}.get(s["decision"], "decide"),
                                ["commit", "decide", "give_up"])
        g.add_edge("commit", "pick")
        g.add_edge("give_up", "pick")
        g.add_edge("next_attack", "pick")
        g.add_edge("report", END)
        return g


def run(ctx: Context, smap: ScreenMap, profile: AppProfile, saved: dict, known: specs_mod.Known, run_id: str,
        only: list[str] | None = None, coder_model: str = "claude-sonnet-5-5", resume: bool = False, echo=print,
        fail_first_fix: bool = False, naive: bool = False, compile_tool: bool = True, auto_fix: bool = False,
        ask=None) -> dict:
    """`ask(proposal) -> "yes" | "no" | "all"` answers the approval question; without it nothing is fixed unasked."""
    tracing.init(tags=[f"run:{run_id}", f"app:{ctx.cfg.package}", f"mode:{'naive' if naive else 'droid-monkey'}"])
    from langgraph.checkpoint.sqlite import SqliteSaver

    loop = Loop(ctx, smap, profile, saved, known, only, coder_model, echo, fail_first_fix=fail_first_fix, naive=naive,
                compile_tool=compile_tool, auto_fix=auto_fix)
    state_db = paths.run_dir(ctx.cfg.app_dir, run_id) / "state.sqlite"
    state_db.parent.mkdir(parents=True, exist_ok=True)

    def until_done(graph, first, config) -> dict:
        from langgraph.types import Command
        out = graph.invoke(first, config, durability="sync")
        while out.get("__interrupt__"):  # paused for approval: ask, then continue from the same node
            answer = ask(out["__interrupt__"][0].value) if ask else "no"
            out = graph.invoke(Command(resume=answer), config, durability="sync")
        return out

    @tracing.span(kind="WORKFLOW", name="droid-monkey run", session_id=run_id)
    def go() -> dict:
        with tracing.identify(ctx.cfg.package, session_id=run_id, app=ctx.cfg.package,
                              mode="naive" if naive else "droid-monkey"), \
                SqliteSaver.from_conn_string(str(state_db)) as saver:
            graph = loop.graph().compile(checkpointer=saver)
            config = {"configurable": {"thread_id": run_id}, "recursion_limit": 200}
            if resume:
                # A resumed run must continue on its own branch, never on whatever is checked out now.
                branch = (graph.get_state(config).values or {}).get("branch")
                if branch and git.current_branch(ctx.cfg.app_dir) != branch:
                    git.checkout(ctx.cfg.app_dir, branch)
                    echo(f"resume: switched the app repo to {branch}")
                return until_done(graph, None, config)
            return until_done(graph, {"run_id": run_id, "log": []}, config)

    final = go()
    tracing.flush()
    return final
