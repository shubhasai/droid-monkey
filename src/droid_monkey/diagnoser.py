"""Diagnoser: an attack failure -> a root-cause hypothesis and the files to change.

A tool-using model loop on droid-monkey's own model (Anthropic API). It starts from the evidence (crash and its app
frames, or the fields that were lost), the attack's steps, and the files behind the attacked screen from the screen
graph, then reads and graph-searches the source. On a retry it gets the previous diagnosis and why the fix failed,
and must revise the hypothesis rather than repeat it.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import tracing
from .codetools import CodeTools
from .llm import LLMConfig, chat_model


@dataclass
class Diagnosis:
    summary: str  # one line, used as the commit subject
    root_cause: str
    files_to_change: list[str]
    fix_approach: str
    test_plan: str
    evidence: list[str] = field(default_factory=list)  # file:line references the model relied on
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class DiagnosisError(RuntimeError):
    pass


SYSTEM = """You are the diagnoser inside droid-monkey, which attacks Android apps with real-world faults (process
death, rotation, network loss, revoked permissions, configuration changes, low memory, a loaded CPU) and then fixes
what breaks.

An attack just failed on the app. Find the root cause in the code: the design mistake that makes the app fail under
this fault, not just the line that threw. Use the tools to read the code; cite file:line for every claim.

For 'froze' (frames over 700 ms) and 'leaked' (destroyed Activities still alive after a forced GC) there is no stack
trace: start from the Perfetto analysis. For a freeze, the main thread's state inside the long slice tells you the
kind of work: 'sleeping/blocked' means a blocking call on the main thread (Thread.sleep, a lock, runBlocking, a
synchronous network or database call, a binder call), 'running' means heavy computation, 'blocked on I/O' means
disk access; then find that work in the code behind the slice (input/click handlers, composition, onCreate). For a
leak, look for what outlives the Activity: static or object fields, singletons, listeners or callbacks registered
without being unregistered, coroutines or threads capturing a Context, ViewModels holding Views or Activities.
Use trace_sql for anything the summary does not answer.

Then answer with ONLY this JSON object, no prose around it:
{{"summary": "imperative one-line fix summary under 60 characters, e.g. 'keep checkout form in SavedStateHandle'",
  "root_cause": "2-4 sentences: what the code does, why it fails under this fault",
  "files_to_change": ["repo-relative paths of the source files the fix must edit; as few as possible"],
  "fix_approach": "the idiomatic Android fix in 2-4 sentences (e.g. SavedStateHandle, lifecycle-aware scope,
                   catch and expose an error state, check the permission at use time, move blocking work to
                   Dispatchers.IO/Default, unregister in onDestroy or use applicationContext / WeakReference)",
  "test_plan": "one regression test that fails before the fix and passes after. Use a JVM unit test in src/test:
                it is fast and reliable. If the logic is tangled with Android, make the fix extract the decision
                into testable code (inject a permission checker or a data source, test the ViewModel with a
                SavedStateHandle, test the state holder) and test that. Use an instrumented test in
                src/androidTest only when the behaviour cannot exist outside the Android runtime, and never
                revoke a permission or kill the process inside one (Android kills the test with it).
                Name the test class and what it asserts.",
  "evidence": ["path/File.kt:line - what it shows"]}}

Readable source files: {files}"""


def _failure_text(result: dict, spec: dict, screen: dict | None) -> str:
    lines = [f"attack: {spec.get('name')} ({spec.get('fault_type')}): {spec.get('description')}",
             f"steps: {json.dumps(spec.get('steps'))}",
             f"verdict: {result.get('outcome')}: {result.get('reason')}"]
    if result.get("exception"):
        lines.append(f"crash: {result.get('crash_signature')}")
        lines.append("app stack frames:\n  " + "\n  ".join(result.get("app_frames") or []))
    if result.get("lost_fields"):
        lines.append(f"fields lost (before -> after): {result.get('lost_fields')}")
    metrics = result.get("metrics") or {}
    if metrics.get("frames"):
        lines.append(f"frame stats: {metrics['frames']}")
    if metrics.get("memory_before"):
        lines.append(f"memory after forced GC: before {metrics['memory_before']}, after {metrics.get('memory_after')}")
    if metrics.get("trace"):
        from .tools.perfetto import TraceAnalysis
        lines.append("Perfetto analysis of the attack:\n" + TraceAnalysis(**metrics["trace"]).summary())
    if screen:
        lines.append(f"attacked screen: {screen.get('id')} '{screen.get('title')}': {screen.get('description', '')}")
        if screen.get("files"):
            lines.append(f"files behind that screen (from the screen graph): {screen.get('files')}")
    return "\n".join(lines)


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError("no JSON object in the answer")
    return json.loads(m.group(0))


def diagnose(app_dir, module: str, llm: LLMConfig, result: dict, spec: dict, screen: dict | None,
             previous: dict | None = None, failure_note: str = "", max_turns: int = 16) -> Diagnosis:
    tools_impl = CodeTools(app_dir, module)
    model = chat_model(llm)
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
    from langchain_core.tools import StructuredTool

    tools = [
        StructuredTool.from_function(tools_impl.read_code, name="read_code",
                                     description="Read lines [start, end] of a source file (repo-relative path)."),
        StructuredTool.from_function(tools_impl.search_code, name="search_code",
                                     description="Regex search over the app's source files; returns file:line matches."),
        StructuredTool.from_function(tools_impl.graph_search, name="graph_search",
                                     description="Entire code graph: ranked symbols for a plain-language query, with "
                                                 "callers/callees signals. Source files only."),
    ]
    trace = Path(result.get("evidence_dir") or ".") / "trace.perfetto-trace"
    if result.get("evidence_dir") and trace.exists():
        from .tools import perfetto

        def trace_sql(sql: str) -> str:
            return perfetto.query(trace, sql)

        tools.append(StructuredTool.from_function(
            trace_sql, name="trace_sql",
            description=f"Read-only SQL on the attack's Perfetto trace (trace_processor tables: process, thread, "
                        f"thread_state(utid, ts, dur, state, blocked_function), slice(ts, dur, name, depth, track_id), "
                        f"thread_track(id, utid), counter, process_counter_track). The app is process name "
                        f"'{result.get('package', '')}' and its main thread has tid = pid. Times are in ns."))
    by_name = {t.name: t for t in tools}
    bound = model.bind_tools(tools)
    ask = _failure_text(result, spec, screen)
    if previous:
        ask += ("\n\nA previous fix based on this diagnosis FAILED verification:\n" + json.dumps(previous, indent=1)
                + f"\nWhy it failed: {failure_note}\nRevise the hypothesis: find what the previous diagnosis missed. "
                  "Do not repeat it unchanged.")
    messages = [SystemMessage(SYSTEM.format(files=sorted(tools_impl.files))), HumanMessage(ask)]
    tin = tout = 0

    @tracing.span(kind="WORKFLOW", name="diagnoser")
    def loop() -> Diagnosis:
        nonlocal tin, tout
        for turn in range(max_turns):
            ai = bound.invoke(messages)
            messages.append(ai)
            usage = getattr(ai, "usage_metadata", None) or {}
            tin += usage.get("input_tokens", 0)
            tout += usage.get("output_tokens", 0)
            if ai.tool_calls:
                for call in ai.tool_calls:
                    tool = by_name.get(call["name"])
                    out = tool.invoke(call["args"]) if tool else f"unknown tool {call['name']}"
                    messages.append(ToolMessage(content=str(out), tool_call_id=call["id"]))
                continue
            try:
                data = _parse(ai.text)
                files = [f.lstrip("./") for f in data.get("files_to_change", [])]
                bad = [f for f in files if f not in tools_impl.files]
                if not files or bad:
                    raise ValueError(f"files_to_change must be existing source files; not found: {bad or 'none given'}")
                return Diagnosis(str(data.get("summary") or data["root_cause"])[:72], data["root_cause"], files, data["fix_approach"], data["test_plan"],
                                 list(data.get("evidence", [])), tin, tout)
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                messages.append(HumanMessage(f"Your answer could not be used: {e}. Reply with ONLY the JSON object."))
        raise DiagnosisError(f"no usable diagnosis after {max_turns} turns")

    return loop()
