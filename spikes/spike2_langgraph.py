"""Block 0 spike 2: LangGraph + Claude Haiku + SqliteSaver resume + neatlogs tracing.

Checks, before any real code depends on them:
  1. a LangGraph run calling Haiku 5.5 shows up in neatlogs as one trace with node and LLM spans
  2. a model-driven conditional edge works (the "decide" step)
  3. a run killed mid-graph resumes from the SQLite checkpoint with the same thread_id
  4. a subprocess call (stand-in for `claude -p`) can be recorded as a TOOL span

Usage:
  uv run python spikes/spike2_langgraph.py --thread t1 --kill-at inject   # dies inside "inject"
  uv run python spikes/spike2_langgraph.py --thread t1                    # resumes, finishes
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

from dotenv import dotenv_values

# Keys: real env vars win, then ~/.droid-monkey/.env, then the .env in this checkout (git-ignored).
for _env in (Path.home() / ".droid-monkey" / ".env", Path(__file__).resolve().parents[1] / ".env"):
    for _k, _v in dotenv_values(_env).items():
        if _v and not os.environ.get(_k):  # empty placeholders never hide real values
            os.environ[_k] = _v

import neatlogs  # noqa: E402  (must init before LangChain/LangGraph/Anthropic are imported)

neatlogs.init(
    workflow_name="droid-monkey-spike",
    instrumentations=["langgraph", "langchain", "anthropic"],
    tags=["spike", "block0"],
)

from langchain.chat_models import init_chat_model  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from neatlogs import span  # noqa: E402

CHECKPOINT_DB = Path(__file__).parent / "spike2-checkpoints.sqlite"

llm = init_chat_model(
    "claude-haiku-5-5",
    model_provider="anthropic",
    api_key=os.environ["DROID_MONKEY_API_KEY"],
    base_url="https://api.anthropic.com",
    max_tokens=512,
)


class State(TypedDict, total=False):
    plan: str
    attempts: int
    outcome: str
    decision: str


def plan(state: State) -> State:
    reply = llm.invoke(
        "You plan fault-injection attacks on an Android shopping app. In one sentence, "
        "say which of these to run first and why: process_death, rotation, network_loss."
    )
    print(f"[plan] {reply.text}")
    return {"plan": reply.text, "attempts": 0}


@span(kind="TOOL", name="adb_inject", tool_name="adb")
def fake_adb() -> str:
    # Stand-in for adb / claude -p: any subprocess becomes a TOOL span.
    return subprocess.run(["echo", "am kill com.shubhasai.monkeymart"], capture_output=True, text=True).stdout.strip()


def inject(state: State) -> State:
    if ARGS.kill_at == "inject":
        print("[inject] simulating a killed run (os._exit) before the node finishes")
        os._exit(1)
    out = fake_adb()
    attempts = state.get("attempts", 0) + 1
    outcome = "crashed" if attempts < 2 else "survived"
    print(f"[inject] ran: {out!r} -> {outcome} (attempt {attempts})")
    return {"attempts": attempts, "outcome": outcome}


def decide(state: State) -> State:
    reply = llm.invoke(
        f"An attack result came back '{state['outcome']}' after {state['attempts']} attempt(s), "
        "and the retry cap is 3. Answer with exactly one word: retry or done."
    )
    decision = "retry" if "retry" in reply.text.lower() else "done"
    print(f"[decide] model said {reply.text!r} -> {decision}")
    return {"decision": decision}


def route(state: State) -> str:
    # Model decides; code enforces the cap.
    if state["decision"] == "retry" and state["attempts"] < 3:
        return "inject"
    return END


def build():
    g = StateGraph(State)
    g.add_node("plan", plan)
    g.add_node("inject", inject)
    g.add_node("decide", decide)
    g.add_edge(START, "plan")
    g.add_edge("plan", "inject")
    g.add_edge("inject", "decide")
    g.add_conditional_edges("decide", route, ["inject", END])
    return g


ARGS = argparse.Namespace(kill_at=None)


@span(kind="WORKFLOW", name="spike2_run")
def run(thread: str) -> dict:
    with SqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        graph = build().compile(checkpointer=saver)
        config = {"configurable": {"thread_id": thread}}
        snapshot = graph.get_state(config)
        if snapshot.next:
            print(f"[resume] thread {thread} has pending nodes {snapshot.next}; continuing from the checkpoint")
            result = graph.invoke(None, config, durability="sync")
        else:
            result = graph.invoke({}, config, durability="sync")
        return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--thread", default="t1")
    p.add_argument("--kill-at", choices=["inject"], default=None)
    ARGS = p.parse_args()
    if not os.environ.get("DROID_MONKEY_API_KEY") or not os.environ.get("NEATLOGS_API_KEY"):
        sys.exit("Set DROID_MONKEY_API_KEY and NEATLOGS_API_KEY in droid-monkey/.env or ~/.droid-monkey/.env first.")
    final = run(ARGS.thread)
    print(f"[done] {final}")
    neatlogs.flush()
    neatlogs.shutdown()
