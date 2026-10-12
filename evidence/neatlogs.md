# neatlogs: one trace per run, with the cost of every step

## What gets traced

droid-monkey is an agent that drives other agents. Claude Code sessions do the analysis, exploration, diagnosis and fixes; droid-monkey's own Claude calls do the planning and decisions. Most of that work happens in separate `claude -p` processes, so only a trace shows the whole run.

| Where | What | Code |
|---|---|---|
| Setup | `neatlogs.init(workflow_name="droid-monkey", instrumentations=["langgraph", "langchain", "anthropic"])`, tagged `run:<id>`, `app:<package>` and `mode:<droid-monkey or naive>` | [`tracing.py`](../src/droid_monkey/tracing.py) `init`; [`loop.py`](../src/droid_monkey/loop.py) `run` |
| Who and which run | `neatlogs.identify(end_user_id=<app package>, session_id=<run id>)`: the app is the end user and each run is a session | `loop.py` `run` |
| The run | A `droid-monkey run` WORKFLOW span; LangGraph instrumentation adds every node: setup, plan, pick, attack, decide, diagnose, approve, fix, verify, commit, give_up, report | `loop.py` |
| Droid-monkey's own model calls | LangChain/Anthropic LLM spans. droid-monkey copies the token counts onto the `neatlogs.llm.*` attributes and adds the cost from Anthropic's prices, so neatlogs shows real spend instead of $0 | `tracing.py` `_price_api_calls` |
| Claude Code sessions | One LLM span per `claude -p` (`claude-code.explore`, `claude-code.fix.<id>`, …) with Claude Code's exact tokens, cache tokens, cost, turns and duration, plus the prompt and answer | `tracing.py` `ClaudeCodeSpan`; [`tools/claude_code.py`](../src/droid_monkey/tools/claude_code.py) |
| Failures | A failed or empty session sets `claude_code.error` and ERROR status, so it shows up as a named failure | `tracing.py`, `tools/claude_code.py` `failure` |
| Steps | WORKFLOW spans `diagnoser`, `hunt` and `attack_designer:<source>`; a TOOL span `verify`; an AGENT span for the API coding agent | `diagnoser.py`, `hunt.py`, `designer.py`, `verifier.py`, `agents.py` |
| Budget | A `budget` GUARDRAIL span on each check against the cfo.ai plan's fix budget (`run --tier`), with the run's spend, the budget and the tier | `loop.py` `within_budget` |

## How it helped: a silent failure the logs missed

On Oct 12, neatlogs raised the incident **"Explorer returns empty response without mapping screens"** (incident `981cb137-9c13-4a66-b420-15ec3649f224`, medium priority, from trace `298e6539fac684793ec2e36284fe3c99`). The explore session had made no tool calls and ended with an empty answer and zero output tokens.

droid-monkey's own logs showed no error for it. The only traceback in that day's job logs was an unrelated device error. As far as droid-monkey could tell, the session had succeeded.

The trace pointed at the cause: an empty LLM span with no tool calls meant the agent had stopped without touching the device. The code had two gaps:
1. An empty or errored `claude -p` result was treated as a success, and `explore` never checked it.
2. If an earlier `screen-map.json` was on disk, `explore` returned it as if this run had made it.

The fix (commit `08fdebd`):
- Empty or errored sessions fail with a reason, set on the span as `claude_code.error` with ERROR status. This applies to every Claude Code session.
- `explore` retries once when nothing was recorded, then stops and says why.
- A map counts only if this run wrote screens to it.
- `tests/test_empty_sessions.py` covers it.

## Cost numbers in the plan

The costs in [`business/drivers.csv`](../business/drivers.csv) are the same numbers that are on these spans. Claude Code reports its own cost, and droid-monkey prices its API calls the same way for the receipt and for neatlogs. `droid-monkey econ` reads them from the run receipts. With a neatlogs read token (`NEATLOGS_TOKEN`), each row could also cite its trace id, the way FirstRun's plan does.

## Links still to add (from the neatlogs app)

- [ ] A trace from before and after an improvement, with the numbers compared, for example the explore incident before and after `08fdebd`.
- [ ] A trace showing a failed fix and the recovery path (`droid-monkey run --fail-first-fix`).
