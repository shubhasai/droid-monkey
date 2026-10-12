# droid-monkey

**An agent that attacks any Android app with real-world faults, finds what breaks, and fixes it, with your consent, proving every fix on a running device.**

It explores your app on an emulator. Then it hits the app with what real phones do to it: the system killing the app in the background, rotation, losing the network, permissions revoked in Settings, memory pressure, a loaded CPU and cold starts. When something breaks it measures exactly how, diagnoses the root cause in your code, and proposes a fix with the evidence. If you say yes, a coding agent makes the fix. droid-monkey then replays the **identical** attack plus a new regression test, and commits only if both pass.

Built solo for **neatHack 2026** ("Build an agent that works, literally").

> **It found a real bug in Google's official Android sample.** In [`android/architecture-samples`](https://github.com/android/architecture-samples), if you start editing a task and Android kills the app in the background, your edits are gone when you come back. `AddEditTaskViewModel` keeps the title and description only in a `MutableStateFlow`. droid-monkey reproduced the bug on a device, traced it to `AddEditTaskViewModel.kt:59`, and fixed it with a `SavedStateHandle` and a regression test, on the first try.

---

## What it does

```
analyze ──► explore ──► run:  plan → attack → decide → diagnose → ask you → fix → verify → commit ──► report / pr
 (code)     (device)          └──────────────── LangGraph, checkpointed, resumable, traced in neatlogs ────────┘
```

| Step | What happens |
|---|---|
| `analyze` | Static scan (manifest, permissions, 10 risk patterns), then a coding agent reads the repo and writes `ANALYSIS.md` plus the user flows worth testing |
| `explore` | A coding agent works through a **checklist of the screens in the code** (`screens.json`: every activity, Compose screen, route, fragment and dialog, the code that opens it and the on-screen labels next to that code, built statically in under a second) **and drives the app** through droid-monkey's device tools, shortest route first. Result: a screen map where each screen is a node with a replayable path from launch, its actions and timings, and the source files behind it |
| `attack` | Runs attacks built from fixed, verified building blocks (no arbitrary commands). Each attack judges the result in code: crash/ANR buffer, field diffs, frame stats, live Activities after a forced GC, launch times, plus a Perfetto trace |
| `run` | The full loop. A model orders the attacks and decides diagnose / retry / give up; **code** decides every verdict |
| diagnose | An agent on Haiku 5.5 reads the code (with `read_code`, `search_code`, Entire's code graph and SQL on the Perfetto trace) and returns the root cause with file:line evidence, a fix approach and a test plan |
| **ask you** | droid-monkey **never changes code without consent**. Every fix attempt, retries included, shows the issue, the measured evidence, the root cause and the suggested fix, and waits for **yes / no / all** |
| fix | A coding agent (Claude Code, Codex or the API agent) edits only `app/src`. It has no shell, can't read docs or git history, and can't change build files. It has one extra tool: `compile` |
| verify | Rebuild, replay the identical attack, run the new test. Fixed only if both pass; otherwise retry with a revised diagnosis (max 2) or roll back |
| commit | One commit per fix on `droid-monkey/<run>`, with an Entire checkpoint. At the end you're put back on your own branch |

## Features

**Understand the app**
- **Works on any Android app.** `init` detects the module, applicationId, build variant, AVD and a JDK your Gradle can run on; nothing app-specific lives in droid-monkey's code.
- **Code analysis** (`analyze`): manifest, permissions, dependencies and 10 risk patterns with file:line, then a coding agent's read of the repo → `ANALYSIS.md` and the user flows worth testing.
- **Screen map** (`explore`): a coding agent drives the app on the emulator from a checklist of every screen in the code. Each screen gets a replayable path from launch and the source files behind it. Modes: `agent`, `fast` (no model), `manual` (you drive, it records) and `rules`; time, screen and spend limits.
- **Signed-in apps**: a test account signs in after every launch (`entry_steps`, credentials from `~/.droid-monkey/.env`).

**Find what breaks**
- **7 built-in real-world attacks**: process death, rotation, network loss, permission revoke, low memory, CPU load and slow startup, each on the app's best screens (`--screens`).
- **App-specific attacks**: from the analysed user flows (`attacks from-flows`), designed by an agent for this app (`attacks suggest`), or described in plain words (`attacks new "…"`). Every spec is built from verified building blocks and tried on the emulator before it's saved.
- **Bugs in normal use** (`hunt`), with no fault injected: crashes and swallowed exceptions in the app's logs, `null`/`NaN`/unfilled placeholders and negative money on screen, and logic bugs across a journey found by a model that must quote the screen it cites.
- **UX audit** (`ux`): touch targets, unlabelled icons, taps to reach each screen, predicted attention heatmaps, measured waits against response-time limits, and a model's review of every screenshot.
- **One ranked list** (`issues`): every open issue from attacks, hunt and the UX audit, ranked by user impact.
- **Verdicts measured in code, not guessed**: crash and ANR buffers, field diffs, frame stats, live Activities after a forced GC, cold and hot start times.
- **Deep evidence**: Perfetto traces analysed with trace_processor (what the main thread was doing in each long block), the main thread's stack from Android's own ANR record, leak reference chains from Shark (LeakCanary's analyser), screen recordings, screenshots, UI dumps and logcat for every attack.

**Fix it, and prove it**
- **Root-cause diagnosis** with file:line evidence, using `read_code`, `search_code`, Entire's code graph and SQL over the Perfetto trace.
- **Never changes code without your yes**: every fix shows the issue, the measured evidence, the root cause and the suggested change, and waits.
- **Three coding agents**: Claude Code, Codex, or droid-monkey's own agent on the Anthropic API. The fixer edits only the app's sources, has no shell, and gets a `compile` tool.
- **Proof on the device**: rebuild, replay the identical attack, run the new regression test. Committed only if both pass.
- **Recovery**: a failed fix goes back into a revised diagnosis and the same coder session (up to 2 tries), or is rolled back without touching your other edits. An emulator watchdog restarts a hung device, and `run --resume` continues a killed run.
- **Fix hunt and UX issues** (`fix`): the issue is re-checked on the device and committed only if it's gone.
- **One commit per fix** on a `droid-monkey/<run>` branch, each with an Entire checkpoint; `pr` opens a GitHub PR with the evidence table.

**See and share the results**
- **Live dashboard** (`dashboard`): every run, screen, issue, analysis and file in `.droid-monkey/`, refreshing as runs write; stream the emulator's screen and run any command from the page.
- **Reports**: an HTML report per run with recordings and charts, a PR body, a score card, a receipt with time, tokens and cost per step, and before/after comparisons (`report`, `compare`).
- **Tracing**: every run is one neatlogs trace, with every graph node, model call and coding session.
- **Cost control**: `econ` turns measured costs into the cfo.ai business plan's inputs, and `run --tier` enforces the plan's fix budget.
- **A CLI with character**: ASCII monkey art for every result, a banana meter for the resilience score.

## Attacks

Built-in attacks are generic templates, filled per app from the screen map:

| Attack | Fault | Fails as |
|---|---|---|
| `process_death` | Fill a form, the app is killed in the background like the low-memory killer does, come back | `crashed`, `lost_state` |
| `rotation` | Rotate while work started by a tap is still running, or on a filled form | `crashed`, `lost_state` |
| `network_loss` | Drop wifi and data, then submit or start slow work | `crashed`, `no_feedback` (silent failure) |
| `permission_revoke` | Grant a permission through the app, revoke it the way Settings does, use the feature again | `crashed` |
| `low_memory` | Recreate the screen 3 times, send critical and background trim signals, come back | `leaked`: more live Activities after a forced GC than at the start, or the heap grew by half and 16 MB. On a leak, droid-monkey dumps the heap and runs **Shark** (LeakCanary's analyser, `brew install leakcanary-shark`) for the exact reference chain that holds the screen; if the app ships LeakCanary, its log adds which screens were destroyed |
| `performance` | Busy-loop 4 CPU cores, scroll, use the screen's slowest action, press a key while it runs | `froze`: any frame over 700 ms (Play vitals "frozen"); `anr`: Android declares the app not responding (the main thread's stack is pulled from Android's own ANR record) |
| `slow_startup` | 5 cold starts and 3 hot starts, timed by Android (`am start -W`) | `slow_start`: median cold start ≥ 5 s or hot start ≥ 1.5 s (Play vitals) |

Each built-in attack runs on its **best 2 screens** by default (`--screens N` or `screens_per_attack:` in `monkey.yaml`).

Plus **app-specific attacks**:
- `droid-monkey attacks from-flows`: every risk the analysis found on a user flow (checkout, login, add task...) becomes an attack that walks that flow and injects that fault at the moment the analysis named. On MonkeyMart, the first two found a real crash by backgrounding or rotating the app during a 5 s load, for $0.01.
- `droid-monkey attacks suggest`: the designer agent reads the analysis and the code and writes new attacks for this app.
- `droid-monkey attacks new "rotate the phone while the order is being placed"`: describe one in plain words.

Every spec is validated against the building blocks and the screen map, and can be tried on the emulator before it's saved.

**Perfetto** traces the performance, memory and startup attacks. droid-monkey analyses the trace with trace_processor and shows:
- what the main thread was doing inside each long block: computing, waiting on a lock or sleep, or waiting on disk;
- blocking calls, binder calls, the busiest threads, garbage collection, and memory use over time;
- for slow launches, a breakdown of the slowest one (`bindApplication`, `activityStart`, and the app's own work).

## Install

```bash
uv tool install git+https://github.com/shubhasai/droid-monkey
```

| You need | For |
|---|---|
| Android SDK with `adb`, the emulator, and one AVD | Running the app and the attacks |
| **One coding agent**: [Claude Code](https://claude.com/claude-code) (`claude`), or [Codex](https://github.com/openai/codex) (`codex`), or just an Anthropic API key | Analysis, exploration, fixes |
| An Anthropic API key in `~/.droid-monkey/.env` as `DROID_MONKEY_API_KEY` | droid-monkey's own reasoning (plan, decide, diagnose, design), on Haiku 5.5 by default |
| Optional: `gh` (logged in), [Entire](https://entire.io), a `NEATLOGS_API_KEY` | `pr`, checkpoints, tracing |

Keys live in `~/.droid-monkey/.env`. droid-monkey never exports them globally, and strips them from the coding agent's environment.

Step by step, from an empty machine to the first run, with troubleshooting: **[SETUP.md](SETUP.md)**.

## Quickstart

```bash
cd your-android-app
droid-monkey init        # writes monkey.yaml: package, module, AVD, a JDK that fits your Gradle
droid-monkey analyze     # ANALYSIS.md + flows worth testing
droid-monkey explore     # screen map, with the source files behind each screen
droid-monkey attack builtin --diagnose   # see what breaks, and the suggested fixes (no code changes)
droid-monkey run         # the full loop; asks you before every fix
droid-monkey pr <run-id> # push the run's branch and open a PR with the evidence
```

Everything droid-monkey writes goes to `.droid-monkey/` in your repo: analysis, screen map, attack specs, and one folder per run with screen recordings, screenshots, UI dumps, logcat, traces and verdicts.

Every `run` and `attack` ends with a **visual report** (`dashboard.html`). Each attack gets the evidence that fits its failure:
- the **screen recording** of the attack, next to the **same attack replayed after the fix**;
- a before/after table of **lost input**;
- a **main-thread timeline** from Perfetto, with every long block marked;
- **frame-time**, **launch-time** (against the Play vitals limits) and **memory** charts;
- the crash log or Android's **ANR record**;
- the fix's **code diff**.

## Commands

| Command | |
|---|---|
| `init` | Detect module, applicationId, AVD and JDK; write `monkey.yaml`; check prerequisites |
| `analyze [--static-only] [--agent]` | Static profile and the coding agent's read → `ANALYSIS.md`, `flows.json` |
| `dashboard [--port 8787]` | A live local dashboard over everything in `.droid-monkey/`, and a **Commands** page that runs every droid-monkey command from forms built from the CLI itself: output streams into the page, consent prompts ("Fix it?") get Yes/No/All buttons, Stop sends Ctrl+C, one job at a time; publishing (`pr`) needs an extra confirmation. Every command request needs the server's token from its own pages: runs with their evidence and reports, screens with attention heatmaps, the analysis, user flows, saved attacks, the UX audit and every file. It follows the folder: when a run writes something, the open page refreshes. Listens on 127.0.0.1 only |
| `explore [--mode agent\|fast\|manual\|rules] [--agent] [--minutes N] [--max-screens N] [--check-paths] [--from-partial]` | Screen map. Agent mode stops at 10 min / 60 screens by default (`explore_minutes`, `explore_max_screens`, `explore_max_usd`, `verify_minutes` in `monkey.yaml`) and keeps what it mapped; Each path is checked when an attack replays it (`--check-paths` replays them all right away); `--from-partial` re-checks the paths of an interrupted run's map or the saved one. `manual`: you drive, droid-monkey records. `rules` is a free tap-everything explorer with no model |
| `attack NAME\|builtin\|custom\|all [--diagnose] [--target --action]` | Run attacks, no fixing. Builds first if the installed app wasn't built from the current source |
| `issues [--rerank]` / `fix ISSUE` | Every open issue (attacks, hunt, UX) in one list, **ranked by Jev** (TypeSafe): expected impact on users × how likely it is a real bug (severity fallback without a key). `fix` repairs one hunt/UX issue with your consent, re-checks it on the device and commits only if it is gone; attack failures go through `run --attacks NAME`. The dashboard's **Issues** page has a Fix button on each |
| `hunt [--no-model]` | Bugs in **normal use**, no fault injected: walks every recorded path like a user and checks the app's logs after each step (crashes, exceptions the app caught and only logged, main-thread jank, leaked windows), every screen (`null`/`NaN`/unfilled placeholders, negative money, blank screens, duplicate rows) and, with a model reading the screenshots, each journey's numbers and messages. A model finding must quote text that is really on the screen it cites, or it is dropped → `HUNT.md`, `issues.json` |
| `attacks list\|show\|suggest\|new\|from-flows` | Built-in and app-specific attack types; the designer agent; attacks from the analysed user flows |
| `run [--attacks a,b] [--screens N] [--agent] [--auto-fix] [--resume ID]` | The full loop. `--auto-fix` skips the question (CI); `--resume` continues a killed or paused run. Every run ends with a **receipt**: what was found and fixed, time, and tokens and cost per step |
| `report RUN` / `compare A B` / `pr RUN` | PR body, score card and dashboard / before-after table / push and open the PR |

### Coding agents

`--agent` or `agent:` in `monkey.yaml`: `auto` (default) picks the first one that's set up.

| | Claude Code | Codex | API agent |
|---|---|---|---|
| Runs as | `claude -p` | `codex exec --json` | droid-monkey's own tool loop on the Anthropic API |
| Limits enforced | Allow and deny rules, no shell | Read-only or workspace-write sandbox; can't stop it reading docs | The same allow and deny rules, applied by its file tools; no shell |
| droid-monkey's device and compile tools | MCP | MCP | MCP client |
| Cost in neatlogs | Exact, as Claude Code reports it | Tokens only | Priced per call |
| Entire checkpoints | ✅ | With `entire agent add codex` | ✗ (not yet) |


## Signed-in screens (test accounts)

droid-monkey never logs in, signs up, posts or pays on an app's **live** backend: every such tap is refused. To reach
the screens behind a login, give it a **test account** and it signs in after every launch:

```yaml
# monkey.yaml
entry_steps: ["tap text:Log in", "type username=${DM_TEST_USER}", "type password=${DM_TEST_PASSWORD}", "tap login_button"]
entry_check: "text:Watchlist"   # something only a signed-in user sees: the run stops if the sign-in failed
live_backend: false             # only when the app points at a test/staging backend
```

The credentials come from your environment or `~/.droid-monkey/.env`, never from `monkey.yaml`. While signed in,
droid-monkey refuses to tap Log out / Sign out (so a run keeps its session), and the explorer is told to explore the
signed-in screens without changing the account. Keep `live_backend: true` (the default) unless the build talks to a
test backend: then sign-up, posting and paying stay refused even while signed in.

## Results

### architecture-samples (Google): one real bug, three approaches
Same base commit. The fix of each approach was judged the same way, by droid-monkey's device replay.

| | Plain Claude Code (generic prompt, no droid-monkey) | droid-monkey, no diagnosis | **droid-monkey** |
|---|---|---|---|
| Process-death bug fixed | ✅ | ✅ first try | ✅ first try |
| Coder tokens | 2,167,816 | 220,546 | **167,297** |
| Cost | $2.46 | $0.43 | **$0.39** |
| Change | 14 files, +424/−36, mostly speculative hardening never checked on a device | 2 files, +56/−4 | **2 files, +55/−5** |

Without a reproduction or a stack trace, plain Claude Code read its way through the repo: about 13× the tokens. It also changed code for faults that, on the device, don't happen. droid-monkey's diagnosis pointed the coder at `AddEditTaskViewModel.kt:59`.

### MonkeyMart: 4 planted bugs
Same 4 attacks, same base. Each version fixed a weakness found in the previous version's run data.

| Version | Fixed | Coder tokens | Cost | Time |
|---|---|---|---|---|
| Plain Claude Code (no droid-monkey; judged by droid-monkey afterwards) | 4/4, nothing checked on a device | 828k | $1.35 | 3.1 min |
| v1, no diagnosis | 4/4 | 575k | $1.28 | 8.0 min |
| v2, + diagnosis | 4/4 (1 retry) | 793k | $2.09 | 11.4 min |
| v3, + compile tool | 4/4 (1 retry) | 743k | $1.78 | 11.1 min |
| **v4, + JVM-first test plans** | **4/4, all first try** | **498k** | **$1.27** | 8.8 min |

To be clear about what this shows: on a small app with textbook bugs, plain Claude Code fixes everything by reading code, and does it faster. droid-monkey's value is the part reading can't do:
- finding which faults actually break the app;
- reproducing them on a running device;
- proving each fix by replaying the identical attack;
- the evidence trail.

Its advantage grows on bigger codebases and on bugs with no stack trace. All results are single runs.

### Performance, memory and startup
These were checked on bugs planted on a scratch branch, plus both apps as they are:

| Planted bug | Measured | Diagnosis |
|---|---|---|
| `Thread.sleep(1500)` in a click handler | `froze`: input handler asleep for 1500 of 1501 ms | Pointed to the exact line. Fixed with Claude Code (16 s) and with the API agent (22 s, $0.08) |
| Activity added to a static list | `leaked`: 1 → 7 live Activities after a forced GC | Found the static list and ruled out two other suspects |
| 5.5 s busy loop in `onCreate` | `slow_start`: cold start median 5946 ms | `performCreate` running for 5525 ms; pointed to the loop |

No false alarms on either app as it is.

## Tracing and checkpoints

How each partner tool is used, with the evidence: [evidence/](evidence/README.md) ([neatlogs](evidence/neatlogs.md) · [Entire](evidence/entire.md) · [cfo.ai](evidence/cfo-ai.md)).

- **neatlogs:** every run is one trace. It covers every graph node and every model and tool call, and each `claude -p` session is an LLM span with its exact tokens and cost. The app is the "end user" and the run id is the session.
- **Before and after, in neatlogs:** the MonkeyMart v1 and v4 runs side by side (in v4 every fix lands first try, with 13% fewer coder tokens), and the trace of the recovery run: [evidence/neatlogs.md](evidence/neatlogs.md#before-and-after-an-improvement-in-neatlogs).
- **Entire:** the Claude Code session that built droid-monkey itself is attached to this repo as a checkpoint, and every fix commit droid-monkey makes in an app carries an `Entire-Checkpoint` trailer linking it to the recorded Claude Code session that wrote it. The diagnoser also uses Entire's code graph to find the symbols involved.

### How neatlogs helped

droid-monkey is an agent that drives other agents: Claude Code sessions for analysis, exploration, diagnosis and fixes, plus its own Claude calls for planning and decisions. Most of that work happens in separate `claude -p` processes, so droid-monkey's own logs only see what each session reports back. neatlogs puts all of it in one trace per run:

- **One timeline per run.** The LangGraph nodes (plan, attack, decide, diagnose, fix, verify, commit), every API call (LangChain and Anthropic instrumentation), and every Claude Code session as an LLM span.
- **Real cost.** Claude Code sessions carry the tokens and cost Claude Code reports. API calls are priced per call, so a run's spend is visible per step, not just as a total.
- **Per app and per run.** The app's package is the end user and the run id is the session, so the traces for one app, or one run, are easy to find.

**It caught a failure we could not see.** On 12 Oct, neatlogs raised the incident *Explorer returns empty response without mapping screens* (`981cb137`, medium). It came from one trace: the `explore` session made no tool calls, returned an empty answer and reported zero output tokens. droid-monkey's own logs showed no error for it. The only traceback in that day's job logs was an unrelated device error. The session had "succeeded" as far as droid-monkey could tell.

**The trace pointed at the cause.** An empty LLM span with no tool calls meant the agent had stopped without touching the device. Following that into the code showed two gaps. First, an empty or errored `claude -p` result was treated as a success, and `explore` never checked it. Second, if an older `screen-map.json` was on disk, `explore` returned that map as if this run had made it. So a run that mapped nothing could look like a finished exploration.

**The fix, and how it shows in neatlogs now:**
- A `claude -p` result that is an error, or has no text and no output tokens, is a failure with a reason. This applies to every Claude Code session droid-monkey runs: analysis, exploration, diagnosis and fixes.
- The reason is set on the span as `claude_code.error`, with ERROR status. A failure like this one now shows up in neatlogs as a named failure, not an empty success.
- `explore` retries once when the agent recorded no screens, then stops and says why.
- A map only counts if this run wrote screens to it, so an earlier map is never passed off as new.
- `tests/test_empty_sessions.py` covers the empty and errored results, the retry, and keeping the earlier map.

Every incident neatlogs raises about droid-monkey is handled the same way: read the trace, find the cause, fix it, add a test, and record it here.

| Incident | What the trace showed | Cause in droid-monkey | Fix |
|---|---|---|---|
| Explorer returns empty response without mapping screens (`981cb137`, medium) | `explore` session: no tool calls, empty answer, zero output tokens | Empty or errored `claude -p` results counted as success; `explore` could return an older map as this run's | Empty or errored sessions fail with a reason (`claude_code.error`, ERROR status); explore retries once; only a map this run wrote counts |

## Business plan (cfo.ai)

droid-monkey sells as a CI add-on for Android teams. The plan is built in cfo.ai from droid-monkey's own measured costs, and the plan in turn sets how much the agent may spend on each run.

**Model (public, read-only): [cfo.ai/s/dcL7ulePdgkkZNCB](https://cfo.ai/s/dcL7ulePdgkkZNCB)** · full write-up: [docs/cfo-plan.md](docs/cfo-plan.md)

1. **Measure:** `droid-monkey econ --app-dir <app> ...` reads every run and writes [`business/drivers.csv`](business/drivers.csv). Each row is a cost or an assumption, with its source and whether it was measured. Attacks cost $0 in model spend (31 s each). The money goes on fixes: $0.56 per fix attempt, $1.12 per verified fix.
2. **Model:** cfo.ai's Ari built the model from `drivers.csv` with three plans (Free, Team $99, Scale $499, plus $3 per verified fix) and six scenarios over 18 months.
3. **Enforce:** the 70% margin floor sets a fix budget per tier in `business/unit_economics.json`. `droid-monkey run --tier team` keeps attacking once the budget is spent but stops fixing, and each check is a `budget` GUARDRAIL span in neatlogs.

| Scenario | Scale margin | Team margin | Month-18 cash |
|---|---|---|---|
| Base | 67.1% | 69.8% | $22,618 |
| No budget cap | **−5.6%** | 45.0% | $10,852 |
| Scale repriced ($986.97) | 70.0% | 69.8% | $29,732 |
| Usage pricing | 74.0% | 80.7% | $44,764 |

**What cfo.ai showed:** the fix budget is what keeps droid-monkey profitable. Without it, Scale loses money on every run. Scale at $499 covers only about 52% of expected fix demand; covering all of it at a 70% margin takes $986.97 a month. None of the six scenarios runs out of cash within 18 months.

## Safety

- **Consent:** no code changes without your explicit yes for each attempt, or `--auto-fix`.
- **Isolation:** analysis and exploration work on a **copy** of your source. Fixes happen on a separate branch, and you're returned to yours at the end.
- **Only the fix is kept:** droid-monkey keeps only edits inside `app/src` made by the coding agent, and discards anything else.
- **No arbitrary commands:** attack specs are built only from validated building blocks. The coding agent never gets a shell (except Codex, inside its sandbox).
- **Same code, same build:** droid-monkey never diagnoses a build that wasn't built from the source it's reading. It rebuilds when they differ.

## Known limitations

- Codex mode is implemented from OpenAI's docs but hasn't been tested end to end.
- API-agent fixes have no Entire checkpoint yet.
- Measurements come from a debug build on an emulator: slower than release on a phone, so a pass is conservative.

## Built with

Python 3.12 · LangGraph · Claude (Haiku 5.5 for reasoning, Sonnet 5.5 for code) · Claude Code / Codex · MCP · adb, emulator, Gradle · Perfetto · neatlogs · Entire

Sample app: [shubhasai/MonkeyMart](https://github.com/shubhasai/MonkeyMart)

License: [MIT](LICENSE)
