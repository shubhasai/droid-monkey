# Evidence: how droid-monkey uses neatlogs, Entire and cfo.ai

Each tool has a job in droid-monkey's loop:

- **neatlogs** watches what the agent does. Every run is one trace, with an exact cost on every step.
- **Entire** records who wrote each line. Every fix links to the Claude Code session that made it, and the diagnoser searches Entire's code graph.
- **cfo.ai** decides how much the agent may spend. The business plan is built from the measured costs and sets a fix budget that `run --tier` enforces.

| | neatlogs | Entire | cfo.ai |
|---|---|---|---|
| **Architecture** | One trace per run: LangGraph nodes, every API call and every Claude Code session as spans. The app is the end user and the run id is the session | `entire enable` on droid-monkey and MonkeyMart; checkpoint refs pushed to GitHub; `entire graph search` as a diagnoser tool | `droid-monkey econ` → `business/drivers.csv` → a cfo.ai model built by Ari → `unit_economics.json` → `run --tier` |
| **Implementation** | [`tracing.py`](../src/droid_monkey/tracing.py): `neatlogs.init` with the langgraph, langchain and anthropic instrumentations; `ClaudeCodeSpan` for `claude -p`; WORKFLOW, TOOL and GUARDRAIL spans | Commit trailers `Entire-Checkpoint: <id>`; [`codetools.py`](../src/droid_monkey/codetools.py) `graph_search`; [`init_cmd.py`](../src/droid_monkey/init_cmd.py) checks Entire and its plugin | [`econ.py`](../src/droid_monkey/econ.py) and the `--tier` budget check in [`loop.py`](../src/droid_monkey/loop.py) |
| **Runtime** | Spans carry exact tokens and cost (Claude Code's own report, or per-call prices for API calls); a failed session sets `claude_code.error` and ERROR status | Each droid-monkey fix commit records the coding agent's session as a checkpoint | Each failure-handling decision first checks the run's spend against the tier budget (a `budget` GUARDRAIL span) |
| **Reasoning** | droid-monkey runs other agents in separate processes. Only a trace shows the whole run and what it cost | A reviewer can see why a line changed and which agent session wrote it, not only the diff | Fixes are where the money goes ($0.56 an attempt, attacks $0), so the plan has to cap fix spend |
| **Result** | Caught a silent failure the logs missed: the empty-explore incident, now fixed ([neatlogs.md](neatlogs.md)) | 35 checkpoints on MonkeyMart (3 of them from today's droid-monkey fixes, the first in PR #2) and 17 on droid-monkey itself ([entire.md](entire.md)) | With no cap, Scale's margin is −5.6%; with the cap, 67.1%. Scale needs $986.97 to cover normal fix demand ([cfo-ai.md](cfo-ai.md)) |

Details: [neatlogs.md](neatlogs.md) · [entire.md](entire.md) · [entire-graph.md](entire-graph.md) (real code-graph output) · [cfo-ai.md](cfo-ai.md) · [building-in-public.md](building-in-public.md) (the posts on X)
