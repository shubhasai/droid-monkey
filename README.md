# droid-monkey

**An agent that attacks any Android app with real-world faults, finds what breaks, and fixes it, with your consent, proving every fix on a running device.**

**▶ [Demo video (3 min)](https://youtu.be/61sALQcY_LY)** · [Landing page](https://shubhasai.github.io/droid-monkey/) · [Submission post](https://x.com/m_shubhasai/status/2109685797603680453) · [Setup guide](SETUP.md) · [Evidence](evidence/README.md)

Real phones are hostile to apps. Android kills them in the background, users rotate the screen mid-task, the network drops, permissions get revoked in Settings, memory runs low, and the CPU is busy. droid-monkey does all of this to your app on an emulator. It measures exactly what broke, finds the root cause in your code and proposes a fix with the evidence. If you say yes, a coding agent makes the fix. droid-monkey then replays the **identical** attack and runs a new regression test, and it commits the fix only if both pass.

> **It found a real bug in Google's official Android sample.** In [`android/architecture-samples`](https://github.com/android/architecture-samples), if you start editing a task and Android kills the app in the background, your edits are gone when you come back. `AddEditTaskViewModel` keeps the title and description only in a `MutableStateFlow`. droid-monkey reproduced the bug on a device, traced it to `AddEditTaskViewModel.kt:59`, and fixed it on the first try with a `SavedStateHandle` and a regression test, using **13× fewer tokens** than plain Claude Code ([details](#architecture-samples-google-one-real-bug-three-approaches)).

Built solo (team **Logd**) for **neatHack 2026**, whose theme was "Build an agent that works, literally".

**Contents:** [Quickstart](#quickstart) · [How it works](#how-it-works) · [Features](#features) · [Attacks](#attacks) · [Commands](#commands) · [Results](#results) · [Tracing and checkpoints](#tracing-and-checkpoints) · [Business plan](#business-plan-cfoai) · [Safety](#safety) · [Limitations](#known-limitations)

---

## Quickstart

```bash
uv tool install git+https://github.com/shubhasai/droid-monkey
```

```bash
cd your-android-app
droid-monkey init        # writes monkey.yaml: package, module, AVD, a JDK that fits your Gradle
droid-monkey analyze     # ANALYSIS.md + flows worth testing
droid-monkey explore     # screen map, with the source files behind each screen
droid-monkey attack builtin --diagnose   # see what breaks, and the suggested fixes (no code changes)
droid-monkey run         # the full loop; asks you before every fix
droid-monkey pr <run-id> # push the run's branch and open a PR with the evidence
```

| You need | For |
|---|---|
| Android SDK with `adb`, the emulator, and one AVD | Running the app and the attacks |
| **One coding agent**: [Claude Code](https://claude.com/claude-code) (`claude`), [Codex](https://github.com/openai/codex) (`codex`), or just an Anthropic API key | Analysis, exploration, fixes |
| An Anthropic API key in `~/.droid-monkey/.env` as `DROID_MONKEY_API_KEY` | droid-monkey's own reasoning (plan, decide, diagnose, design), on Haiku 5.5 by default |
| Optional: `gh` (logged in), [Entire](https://entire.io), `NEATLOGS_API_KEY`, `TYPESAFE_API_KEY` | `pr`, checkpoints, tracing, issue ranking |

**Why setup is quick:**
- **Nothing to change in your app.** There's no SDK to add, no test code and no instrumentation. droid-monkey drives the app from outside through `adb` and reads its source.
- **One install command and one config command.** `droid-monkey init` finds the package and module, picks an AVD and chooses a JDK that fits your Gradle version. It checks `adb`, the emulator, the coding agent and the keys, and tells you exactly what's missing. The result is a short `monkey.yaml` you can read.
- **The same tool for any app.** The three apps in [Results](#results) needed no special code: a demo shop, Google's architecture sample and the Wikipedia app.

Keys live in `~/.droid-monkey/.env`. droid-monkey never exports them globally, and it strips them from the coding agent's environment. For a step-by-step setup from an empty machine, with troubleshooting, see **[SETUP.md](SETUP.md)**.

Everything droid-monkey writes goes to `.droid-monkey/` in your repo: the analysis, the screen map, attack specs, and one folder per run with screen recordings, screenshots, UI dumps, logcat, traces and verdicts. Every `run` and `attack` ends with a **visual report** (`dashboard.html`), and `droid-monkey dashboard` serves a live page over all of it.

## How it works

```
analyze ──► explore ──► run:  plan → attack → decide → diagnose → ask you → fix → verify → commit ──► report / pr
 (code)     (device)          └──────────────── LangGraph, checkpointed, resumable, traced in neatlogs ────────┘
                 │
                 ├──► attacks new / suggest / from-flows   the attack designer agent writes attacks for this app
                 ├──► hunt                                 bugs in normal use, no fault injected
                 └──► ux                                   UX audit: heatmaps, waits, tap targets, suggested changes
                                     all of them ──► issues (one ranked list) ──► fix ──► dashboard
```

| Step | What happens |
|---|---|
| **`analyze`** | A static scan (manifest, permissions, 10 risk patterns). Then a coding agent reads the repo and writes `ANALYSIS.md` and the user flows worth testing |
| **`explore`** | A coding agent works through a **checklist of the screens in the code** and **drives the app** through droid-monkey's device tools, shortest route first. The checklist is `screens.json`, built statically in under a second: every activity, Compose screen, route, fragment and dialog, the code that opens it, and the on-screen labels next to that code. The result is a screen map. Each screen is a node with a replayable path from launch, its actions and timings, and the source files behind it |
| **attack** | Runs attacks built from fixed, verified building blocks (no arbitrary commands). Each attack judges its result in code from the crash/ANR buffer, field diffs, frame stats, live Activities after a forced GC, launch times and a Perfetto trace |
| **decide** | A model orders the attacks and chooses diagnose, retry or give up. **Code** decides every verdict |
| **diagnose** | An agent on Haiku 5.5 reads the code with `read_code`, `search_code`, Entire's code graph and SQL on the Perfetto trace. It returns the root cause with file:line evidence, a fix approach and a test plan |
| **ask you** | droid-monkey **never changes code without consent**. Every fix attempt, retries included, shows the issue, the measured evidence, the root cause and the suggested fix, then waits for **yes / no / all** |
| **fix** | A coding agent (Claude Code, Codex or the API agent) edits only `app/src` on a `droid-monkey/<run>` branch, never yours. It has no shell, can't read docs or git history, and can't change build files. It has one extra tool: `compile` |
| **verify** | Rebuild, replay the identical attack and run the new test. The fix counts only if both pass. Otherwise droid-monkey retries with a revised diagnosis (up to 2 tries) or rolls back |
| **commit** | One commit per fix, carrying the root cause, the fix, the evidence and an Entire checkpoint. At the end you're put back on your own branch, and `pr` opens a pull request with the before/after evidence table (for example [MonkeyMart PR #2](https://github.com/shubhasai/MonkeyMart/pull/2)) |

### Architecture

droid-monkey runs on your machine and works with three things it doesn't own: **your app's repo**, a **coding agent** (Claude Code, Codex or its own API agent) and an **Android emulator**. The coding agent never drives the device directly. It gets droid-monkey's two MCP servers: `device` (drive the app) and `build` (compile). Claude Code and the API agent get no shell, and Codex has one only inside its sandbox. Every fault, screen read and verdict goes through droid-monkey's own code over `adb`.

```
                                            You
                                commands · yes / no · dashboard
                                             │
┌────────────────────────────────────────────▼───────────────────────────────────────────┐
│ droid-monkey  (Python, on your machine)                                                │
│                                                                                        │
│   run loop (LangGraph)     models (Anthropic API)        code only                     │
│   plan · decide ·          diagnose · design attacks ·   spec runner · AppDriver ·     │
│   ask you                  hunt · UX review              verifier · every verdict      │
│                                                                                        │
│   MCP servers for the coding agent:   device  launch, tap, type, scroll, record_screen │
│                                       build   compile                                  │
└──────┬────────────▲─────────────────────────┬─────────────────────────┬─────────▲──────┘
       │ 1          │ 2                       │ 4                       │ 6       │ 7
       ▼            │                         ▼                         ▼         │
┌───────────────────┴──────┐  3   ┌──────────────────────────┐  5   ┌─────────────┴──────────┐
│ Coding agent, one of:    ├─────►│ Your Android app repo    ├─────►│ Android emulator       │
│  Claude Code  claude -p  │      │  app/src                 │      │  your app, debug build │
│  Codex        codex exec │      │  Gradle                  │      │  Android: activity     │
│  API agent    own loop   │      │  droid-monkey/<run>      │      │  manager, settings,    │
│                          │      │  branch + Entire         │      │  network, logcat,      │
│                          │      │  checkpoints             │      │  Perfetto              │
└──────────────────────────┘      └──────────────────────────┘      └────────────────────────┘
```

1. **Tasks for the coding agent**: read the repo (`analyze`), map every screen (`explore`), or make one fix from a brief that names the root cause, the files and the test to write.
2. **MCP tool calls back to droid-monkey**: `device` to drive the app while exploring, `build` to compile while fixing. The agent is never given `adb`.
3. **Edits**: only under `app/src`. Anything else the agent changes is discarded.
4. **droid-monkey on the repo**: the models read and search the code (with Entire's code graph), the verifier builds with Gradle, and a fix is committed on `droid-monkey/<run>` only if it's proven.
5. **The build**: Gradle's APK is installed on the emulator, and droid-monkey records which source it was built from.
6. **Driving the device over `adb`**: replaying paths, taps and form fills, reading screens through uiautomator, and injecting the faults (kill in background, rotate, network off, revoke a permission, trim memory, CPU load, cold starts).
7. **Evidence back**: crash and ANR buffers, logcat, field values, frame stats, live Activities after a forced GC, heap dumps and a Perfetto trace. Code turns these into the verdict.

### One attack, start to fix

```mermaid
sequenceDiagram
    autonumber
    actor You
    participant DM as droid-monkey
    participant AG as Claude Code / Codex
    participant RP as App repo + Gradle
    participant EM as Emulator + your app

    Note over DM,EM: explore (once)
    DM->>AG: map every screen in screens.json
    AG->>DM: device MCP: launch, tap, type, record_screen
    DM->>EM: adb input and uiautomator reads
    EM-->>DM: screen, timings, crashes
    DM->>DM: screen map, a replayable path per screen

    Note over DM,EM: attack
    DM->>EM: replay the path, inject the fault (for example kill in background)
    EM-->>DM: crash buffer, field values, frame stats, Perfetto trace
    DM->>DM: verdict decided in code (crashed, lost_state, froze...)

    Note over DM,RP: diagnose and ask
    DM->>RP: read code, Entire code graph, SQL on the trace (Anthropic API)
    DM->>You: issue, evidence, root cause at file:line, suggested fix. Fix it?
    You-->>DM: yes

    Note over AG,RP: fix
    DM->>AG: fix brief on a droid-monkey branch
    AG->>RP: edit app/src only, no shell
    AG->>DM: build MCP: compile
    DM-->>AG: compile errors, if any

    Note over DM,EM: verify
    DM->>RP: gradle build
    DM->>EM: install, replay the identical attack
    DM->>RP: run the new regression test
    alt replay survives and the test passes
        DM->>RP: commit with an Entire checkpoint
    else either fails
        DM->>DM: revised diagnosis and retry (max 2), else roll back
    end
```

- **Models decide, code judges.** Models plan, explore, diagnose, design attacks and review UX. Every verdict (crashed, leaked, froze, fixed) is measured on the device by code.
- **Any of three coding agents.** **Claude Code** runs as `claude -p` with allow and deny rules and no shell. **Codex** runs as `codex exec` in its sandbox. The **API agent** is droid-monkey's own tool loop, for people with neither CLI. All three get the same brief and the same two MCP servers, and droid-monkey discards any edit outside `app/src` afterwards.
- **A self-managed emulator.** droid-monkey finds, boots, health-checks and restarts the emulator, and a watchdog restarts a hung device. Screens are read through uiautomator, which takes about 30 ms a read with uiautomator2's on-device helper installed.

## Features

**Understand the app**
- **Works on any Android app.** `init` detects the module, applicationId, build variant, AVD and a JDK your Gradle can run on. Nothing app-specific lives in droid-monkey's code.
- **Code analysis** (`analyze`): the manifest, permissions, dependencies and 10 risk patterns with file:line, then a coding agent's read of the repo → `ANALYSIS.md` and the user flows worth testing.
- **Screen map** (`explore`): a replayable path from launch to every screen in the code, with the source files behind it. The modes are `agent`, `fast` (no model), `manual` (you drive and it records) and `rules`, with limits on time, screens and spend.
- **Signed-in apps**: a test account signs in after every launch ([see below](#signed-in-screens-test-accounts)).

**Find what breaks**
- **7 built-in real-world attacks**: process death, rotation, network loss, permission revoke, low memory, CPU load and slow startup, each run on the app's best screens ([Attacks](#attacks)).
- **App-specific attacks**, designed by an agent for this app (`attacks suggest`), built from the analysed user flows (`attacks from-flows`) or described in plain words (`attacks new "…"`). Every spec is built from verified building blocks and tried on the emulator before it's saved.
- **Bugs in normal use** (`hunt`), with no fault injected: crashes and swallowed exceptions in the app's logs, `null`/`NaN`/unfilled placeholders and negative money on screen, and logic bugs across a journey. Those are found by a model that must quote the screen it cites.
- **UX audit** (`ux`): touch targets, unlabelled icons, taps to reach each screen, predicted attention heatmaps, waits measured against response-time limits, and a model's review of every screenshot ([on Wikipedia](#wikipedia-a-large-real-app-and-the-ux-audit)).
- **One ranked list** (`issues`): every open issue from attacks, hunt and the UX audit, ranked by expected impact on users.
- **Verdicts measured in code, not guessed**: crash and ANR buffers, field diffs, frame stats, live Activities after a forced GC, and cold and hot start times.
- **Deep evidence** for every attack: Perfetto traces analysed with trace_processor (what the main thread was doing in each long block), the main thread's stack from Android's own ANR record, and leak reference chains from Shark (LeakCanary's analyser). Each attack also gets a screen recording, screenshots, UI dumps and logcat.

**Fix it, and prove it**
- **Root-cause diagnosis** with file:line evidence, using `read_code`, `search_code`, Entire's code graph and SQL over the Perfetto trace.
- **Never changes code without your yes**: every fix shows the issue, the measured evidence, the root cause and the suggested change, and then waits.
- **Three coding agents**: Claude Code, Codex, or droid-monkey's own agent on the Anthropic API. The fixer edits only the app's sources, has no shell and gets a `compile` tool.
- **Proof on the device**: rebuild, replay the identical attack and run the new regression test. The fix is committed only if both pass.
- **Recovery**: a failed fix goes back into a revised diagnosis in the same coder session (up to 2 tries), or is rolled back without touching your other edits. `run --resume` continues a killed run.
- **Fix hunt and UX issues** (`fix`): the issue is re-checked on the device and committed only if it's gone.
- **One commit per fix** on a `droid-monkey/<run>` branch, each with an Entire checkpoint. `pr` opens a GitHub PR with the evidence table.

**See and share the results**
- **Live dashboard** (`dashboard`): every run, screen, issue, analysis and file in `.droid-monkey/`, refreshed as runs write. You can stream the emulator's screen and run any command from the page.
- **Visual reports** per run. Each attack gets the evidence that fits its failure:
  - the screen recording of the attack, next to the same attack replayed after the fix;
  - a before/after table of lost input;
  - a main-thread timeline from Perfetto;
  - frame-time, launch-time and memory charts;
  - the crash log or ANR record;
  - the fix's code diff.
- **Receipts and comparisons**: time, tokens and cost per step for every run, a score card, a PR body, and before/after tables (`report`, `compare`).
- **Tracing**: every run is one neatlogs trace, with every graph node, model call and coding session.
- **Cost control**: `econ` turns measured costs into the cfo.ai business plan's inputs, and `run --tier` enforces the plan's fix budget.
- **A CLI with character**: ASCII monkey art for every result, and a banana meter for the resilience score.

## Attacks

Built-in attacks are generic templates, filled in per app from the screen map:

| Attack | Fault | Fails as |
|---|---|---|
| `process_death` | Fill a form, kill the app in the background the way the low-memory killer does, then come back | `crashed`, `lost_state` |
| `rotation` | Rotate while work started by a tap is still running, or on a filled form | `crashed`, `lost_state` |
| `network_loss` | Drop wifi and data, then submit or start slow work | `crashed`, `no_feedback` (silent failure) |
| `permission_revoke` | Grant a permission through the app, revoke it the way Settings does, then use the feature again | `crashed` |
| `low_memory` | Recreate the screen 3 times, send critical and background trim signals, then come back | `leaked`: more live Activities after a forced GC than at the start, or the heap grew by half and by 16 MB |
| `performance` | Busy-loop 4 CPU cores, scroll, use the screen's slowest action, and press a key while it runs | `froze`: any frame over 700 ms (the Play vitals "frozen" limit); `anr`: Android declares the app not responding |
| `slow_startup` | 5 cold starts and 3 hot starts, timed by Android (`am start -W`) | `slow_start`: median cold start ≥ 5 s or hot start ≥ 1.5 s (Play vitals) |

Each built-in attack runs on its **best 2 screens** by default (`--screens N` or `screens_per_attack:` in `monkey.yaml`).

- **Leaks**: when droid-monkey finds a leak, it dumps the heap and runs **Shark** (LeakCanary's analyser, `brew install leakcanary-shark`) to get the exact reference chain that holds the screen. If the app ships LeakCanary, its log adds which screens were destroyed.
- **ANRs**: the main thread's stack is pulled from Android's own ANR record.
- **Perfetto** traces the performance, memory and startup attacks. droid-monkey analyses each trace with trace_processor and shows:
  - what the main thread was doing inside each long block (computing, waiting on a lock or sleep, or waiting on disk);
  - blocking calls, binder calls, the busiest threads, garbage collection and memory use over time;
  - for slow launches, a breakdown of the slowest one (`bindApplication`, `activityStart` and the app's own work).

**App-specific attacks** come from the attack designer agent. It composes only verified building blocks, and every spec is validated against the building blocks and the screen map. Each one can be tried on the emulator before it's saved.
- `droid-monkey attacks from-flows` turns every risk the analysis found on a user flow (checkout, login, add task...) into an attack. The attack walks that flow and injects the fault at the moment the analysis named. On MonkeyMart, the first two found a real crash for $0.01, by backgrounding or rotating the app during a 5 s load.
- `droid-monkey attacks suggest`: the designer agent reads the analysis and the code and writes new attacks for this app.
- `droid-monkey attacks new "rotate the phone while the order is being placed"` builds an attack from a description in plain words.

## Commands

| Command | |
|---|---|
| `init` | Detect the module, applicationId, AVD and JDK, write `monkey.yaml` and check prerequisites |
| `analyze [--static-only] [--agent]` | Static profile and the coding agent's read → `ANALYSIS.md`, `flows.json` |
| `explore [--mode agent\|fast\|manual\|rules] [--agent] [--minutes N] [--max-screens N] [--check-paths] [--from-partial]` | Build the screen map. Agent mode stops at 10 min or 60 screens by default and keeps what it mapped (`explore_minutes`, `explore_max_screens`, `explore_max_usd` and `verify_minutes` in `monkey.yaml`). Each path is checked when an attack replays it, and `--check-paths` replays them all right away. `--from-partial` re-checks the paths of an interrupted run's map or of the saved one. In `manual` you drive and droid-monkey records. `rules` is a free tap-everything explorer with no model |
| `attack NAME\|builtin\|custom\|all [--diagnose] [--target --action]` | Run attacks without fixing anything. Builds first if the installed app wasn't built from the current source |
| `run [--attacks a,b] [--screens N] [--agent] [--auto-fix] [--resume ID] [--tier T]` | The full loop. `--auto-fix` skips the question (for CI), `--resume` continues a killed or paused run, and `--tier` enforces the plan's fix budget. Every run ends with a **receipt**: what was found and fixed, the time taken, and tokens and cost per step |
| `hunt [--no-model]` | Find bugs in **normal use**, with no fault injected. It walks every recorded path like a user and checks the app's logs after each step for crashes, exceptions the app caught and only logged, main-thread jank and leaked windows. On every screen it checks for `null`/`NaN`/unfilled placeholders, negative money, blank screens and duplicate rows. With a model reading the screenshots, it also checks each journey's numbers and messages. A model finding must quote text that is really on the screen it cites, or it's dropped → `HUNT.md`, `issues.json` |
| `ux [--no-apply] [--agent api\|claude\|codex] [--review-model M]` | Run a **UX audit** of every mapped screen: predicted attention heatmaps, waits measured against Nielsen's limits, tap targets under 48 dp, unlabelled controls, taps from launch, and a model's review of every screenshot with the code behind it. Each finding is a change (problem, evidence, why it matters, the edit, the files), and findings seen on the device are labelled apart from ones suspected from code only. droid-monkey asks about each finding, and a yes has a coding agent make the change. The change is discarded if the app doesn't build. Otherwise droid-monkey reopens the screen for a before/after screenshot and commits it. `--no-apply` only writes the audit → `UX_AUDIT.md`, `ux-audit.html`, `attention/` |
| `issues [--rerank]` / `fix ISSUE` | List every open issue (attacks, hunt, UX) ranked by expected impact on users × how likely it is to be a real bug, scored by TypeSafe's Jev model. Without `TYPESAFE_API_KEY` it falls back to severity. `fix` repairs one hunt or UX issue with your consent, re-checks it on the device and commits it only if it's gone. Attack failures go through `run --attacks NAME` |
| `attacks list\|show\|suggest\|new\|from-flows` | Built-in and app-specific attack types, the designer agent, and attacks from the analysed user flows |
| `report RUN` / `compare A B` / `pr RUN` | PR body, score card and dashboard / a before-after table / push and open the PR |
| `econ --app-dir DIR` | Turn the measured costs of every run into the business plan's inputs (`business/drivers.csv`) |
| `dashboard [--port 8787]` | A live local dashboard over everything in `.droid-monkey/`: runs with their evidence and reports, screens with attention heatmaps, the analysis, user flows, saved attacks, the UX audit and every file. The open page refreshes when a run writes something. Its **Commands** page runs every droid-monkey command from forms built from the CLI itself. Output streams into the page, consent prompts ("Fix it?") get Yes/No/All buttons, Stop sends Ctrl+C, and only one job runs at a time. Publishing (`pr`) needs an extra confirmation. The server listens on 127.0.0.1 only, and every command request needs the token from its own pages. The **Issues** page has a Fix button on each issue |

### Coding agents

Set the agent with `--agent` or `agent:` in `monkey.yaml`. The default, `auto`, picks the first one that's set up.

| | Claude Code | Codex | API agent |
|---|---|---|---|
| Runs as | `claude -p` | `codex exec --json` | droid-monkey's own tool loop on the Anthropic API |
| Limits enforced | Allow and deny rules, no shell | Read-only or workspace-write sandbox; can't stop it reading docs | The same allow and deny rules, applied by its file tools; no shell |
| droid-monkey's device and compile tools | MCP | MCP | MCP client |
| Cost in neatlogs | Exact, as Claude Code reports it | Tokens only | Priced per call |
| Entire checkpoints | ✅ | With `entire agent add codex` | ✗ (not yet) |

### Signed-in screens (test accounts)

droid-monkey never logs in, signs up, posts or pays on an app's **live** backend: every such tap is refused. To reach the screens behind a login, give it a **test account** and it signs in after every launch:

```yaml
# monkey.yaml
entry_steps: ["tap text:Log in", "type username=${DM_TEST_USER}", "type password=${DM_TEST_PASSWORD}", "tap login_button"]
entry_check: "text:Watchlist"   # something only a signed-in user sees: the run stops if the sign-in failed
live_backend: false             # only when the app points at a test/staging backend
```

The credentials come from your environment or `~/.droid-monkey/.env`, never from `monkey.yaml`. While signed in, droid-monkey refuses to tap Log out or Sign out, so a run keeps its session. The explorer is told to explore the signed-in screens without changing the account. Keep `live_backend: true` (the default) unless the build talks to a test backend. With the default, sign-up, posting and paying stay refused even while signed in.

## Results

droid-monkey was tested on three apps: Google's architecture sample, MonkeyMart (a demo shop with planted bugs) and the Wikipedia app. All results are single runs.

### architecture-samples (Google): one real bug, three approaches
[android/architecture-samples](https://github.com/android/architecture-samples) is Google's reference app for Android architecture (Compose, ViewModels, Hilt). droid-monkey found a real process-death bug in it: a half-typed new task is lost when the OS kills the app in the background. The bug was then fixed three ways from the same base commit, and droid-monkey's device replay judged each approach's fix the same way.

| | Plain Claude Code (generic prompt, no droid-monkey) | droid-monkey, no diagnosis | **droid-monkey** |
|---|---|---|---|
| Process-death bug fixed | ✅ | ✅ first try | ✅ first try |
| Coder tokens | 2,167,816 | 220,546 | **167,297** |
| Cost | $2.46 | $0.43 | **$0.39** |
| Change | 14 files, +424/−36, mostly speculative hardening never checked on a device | 2 files, +56/−4 | **2 files, +55/−5** |

Without a reproduction or a stack trace, plain Claude Code read its way through the repo and used about 13× the tokens. It also changed code for faults that don't happen on the device. droid-monkey's diagnosis pointed the coder straight at `AddEditTaskViewModel.kt:59`.

### MonkeyMart: 4 planted bugs
[MonkeyMart](https://github.com/shubhasai/MonkeyMart) was run with the same 4 attacks from the same base. Each version of droid-monkey fixed a weakness found in the previous version's run data.

| Version | Fixed | Coder tokens | Cost | Time |
|---|---|---|---|---|
| Plain Claude Code (no droid-monkey; judged by droid-monkey afterwards) | 4/4, nothing checked on a device | 828k | $1.35 | 3.1 min |
| v1, no diagnosis | 4/4 | 575k | $1.28 | 8.0 min |
| v2, + diagnosis | 4/4 (1 retry) | 793k | $2.09 | 11.4 min |
| v3, + compile tool | 4/4 (1 retry) | 743k | $1.78 | 11.1 min |
| **v4, + JVM-first test plans** | **4/4, all first try** | **498k** | **$1.27** | 8.8 min |

What each version added, and what it bought:
- **v1 → v2, diagnosis.** The coder gets the root cause from the code and the device evidence (crash, logcat, Perfetto, leak counts) instead of only "it failed". The fixes became specific: compare v1's commit `fix(rotation): survive rotation` with v4's `fix(rotation): tie deal popup coroutine to lifecycle, not GlobalScope`.
- **v2 → v3, a compile tool.** The coder can build the module before it finishes, so broken fixes don't reach the device.
- **v3 → v4, JVM-first test plans.** The diagnosis plans a JVM regression test where possible, instead of a slow instrumented one. Every fix then passed on the first try.

From v2 to v4: **37% fewer coder tokens (793k → 498k), 39% lower cost ($2.09 → $1.27), 23% faster (11.4 → 8.8 min), no retries.**

To be clear about what this shows: on a small app with textbook bugs, plain Claude Code fixes everything by reading code, and does it faster. droid-monkey's value is the part reading can't do:
- finding which faults actually break the app;
- reproducing them on a running device;
- proving each fix by replaying the identical attack;
- leaving an evidence trail.

That advantage grows on bigger codebases and on bugs with no stack trace.

### Wikipedia: a large real app, and the UX audit
The [Wikipedia Android app](https://github.com/wikimedia/apps-android-wikipedia) is a large production codebase. droid-monkey mapped **40 screens** from its code and the device, with no changes to the app. Then it ran `droid-monkey ux` on it ($1.17, Sonnet 5.5). The audit combines three kinds of evidence:

- **Predicted attention heatmaps** for every screen. A saliency model (spectral residual) is combined with a prior from the UI structure. Each screen gets a heatmap and the share of attention each control is likely to get. Five screens were flagged where the main action loses to something else. On the first onboarding screen, "Learn more about Wikipedia" is predicted to draw **16× the attention of the Forward button (18% vs 1%)**. On the interests screen, **Next ranks 7th of 8 controls**.
- **Measured waits** on every route, against Nielsen's limits (0.1 s feels instant, 1 s keeps the flow, 10 s loses attention). Cold start was 1.59 s, and 8 steps broke the flow. For example, opening a file page took 2.3 s.
- **What was observed versus suspected.** There are **9 findings: 7 seen on the device and 2 suspected from the code only**, and the two kinds are labelled differently. The top finding, rated high, is real: a Year in Review promo covers an article the user has just opened.

| First onboarding screen: the eye goes to the headline and "Learn more", not Forward | Interests: Next ranks 7th of 8 |
|---|---|
| <img src="docs/img/wikipedia-heatmap-onboarding.jpg" width="300" alt="Predicted attention heatmap of Wikipedia's first onboarding screen"> | <img src="docs/img/wikipedia-heatmap-interests.jpg" width="300" alt="Predicted attention heatmap of Wikipedia's interests screen"> |

<details>
<summary><b>All 40 heatmaps</b> (the 5 screens where the main action loses come first; on the <a href="https://shubhasai.github.io/droid-monkey/#ux">landing page</a> they're in a carousel with each screen's numbers)</summary>

| | | | | |
|---|---|---|---|---|
| <img src="site/assets/heatmaps/initialonboardingscreen.jpg" width="160" alt="Onboarding 1: welcome"><br><sub>Onboarding 1: welcome</sub> | <img src="site/assets/heatmaps/interestonboardingscreen.jpg" width="160" alt="Onboarding 4: interests"><br><sub>Onboarding 4: interests</sub> | <img src="site/assets/heatmaps/talkreplyactivity.jpg" width="160" alt="Talk reply"><br><sub>Talk reply</sub> | <img src="site/assets/heatmaps/gameshubscreen.jpg" width="160" alt="Games hub"><br><sub>Games hub</sub> | <img src="site/assets/heatmaps/sourceselectionscreen.jpg" width="160" alt="Reading list: source"><br><sub>Reading list: source</sub> |
| <img src="site/assets/heatmaps/initialonboardinglanguagesscreen.jpg" width="160" alt="Onboarding 2: languages"><br><sub>Onboarding 2: languages</sub> | <img src="site/assets/heatmaps/onboardingcuriosityscreen.jpg" width="160" alt="Onboarding 3: curiosity"><br><sub>Onboarding 3: curiosity</sub> | <img src="site/assets/heatmaps/homescreen.jpg" width="160" alt="Home feed"><br><sub>Home feed</sub> | <img src="site/assets/heatmaps/pageactivity.jpg" width="160" alt="Article (after tap)"><br><sub>Article (after tap)</sub> | <img src="site/assets/heatmaps/talktopicactivity.jpg" width="160" alt="Talk topic"><br><sub>Talk topic</sub> |
| <img src="site/assets/heatmaps/edithistorylistactivity.jpg" width="160" alt="Edit history"><br><sub>Edit history</sub> | <img src="site/assets/heatmaps/articleeditdetailsactivity.jpg" width="160" alt="Edit details (diff)"><br><sub>Edit details (diff)</sub> | <img src="site/assets/heatmaps/usercontriblistactivity.jpg" width="160" alt="User contributions"><br><sub>User contributions</sub> | <img src="site/assets/heatmaps/searchfragment.jpg" width="160" alt="Search"><br><sub>Search</sub> | <img src="site/assets/heatmaps/yearinreviewscreen.jpg" width="160" alt="Year in Review"><br><sub>Year in Review</sub> |
| <img src="site/assets/heatmaps/settingsfragment.jpg" width="160" alt="Settings"><br><sub>Settings</sub> | <img src="site/assets/heatmaps/communitymodulesscreen.jpg" width="160" alt="Community modules"><br><sub>Community modules</sub> | <img src="site/assets/heatmaps/donationreminderscreen.jpg" width="160" alt="Donation reminder"><br><sub>Donation reminder</sub> | <img src="site/assets/heatmaps/developersettingsfragment.jpg" width="160" alt="Developer settings"><br><sub>Developer settings</sub> | <img src="site/assets/heatmaps/placesfragment.jpg" width="160" alt="Places"><br><sub>Places</sub> |
| <img src="site/assets/heatmaps/onthisdayfragment.jpg" width="160" alt="On this day"><br><sub>On this day</sub> | <img src="site/assets/heatmaps/filepagefragment.jpg" width="160" alt="File page (Picture of the day)"><br><sub>File page (Picture of the day)</sub> | <img src="site/assets/heatmaps/talktopicsactivity.jpg" width="160" alt="Talk topics"><br><sub>Talk topics</sub> | <img src="site/assets/heatmaps/composelanglinksscreen.jpg" width="160" alt="Article languages"><br><sub>Article languages</sub> | <img src="site/assets/heatmaps/tabactivity.jpg" width="160" alt="Tabs"><br><sub>Tabs</sub> |
| <img src="site/assets/heatmaps/usercontribfilteractivity.jpg" width="160" alt="Contributions filter"><br><sub>Contributions filter</sub> | <img src="site/assets/heatmaps/wikipedialanguagesactivity.jpg" width="160" alt="Wikipedia languages"><br><sub>Wikipedia languages</sub> | <img src="site/assets/heatmaps/languageslistscreen.jpg" width="160" alt="Add a language"><br><sub>Add a language</sub> | <img src="site/assets/heatmaps/installwidgetscreen.jpg" width="160" alt="Install widget"><br><sub>Install widget</sub> | <img src="site/assets/heatmaps/historyfragment.jpg" width="160" alt="History"><br><sub>History</sub> |
| <img src="site/assets/heatmaps/onboardingscreen.jpg" width="160" alt="Feature onboarding"><br><sub>Feature onboarding</sub> | <img src="site/assets/heatmaps/menunavtabdialog.jpg" width="160" alt="Main menu"><br><sub>Main menu</sub> | <img src="site/assets/heatmaps/onthisdaygamemainmenufragment.jpg" width="160" alt="On this day game: menu"><br><sub>On this day game: menu</sub> | <img src="site/assets/heatmaps/onthisdaygameplayfragment.jpg" width="160" alt="On this day game: play"><br><sub>On this day game: play</sub> | <img src="site/assets/heatmaps/homefeedsettingsscreen.jpg" width="160" alt="Home feed settings"><br><sub>Home feed settings</sub> |
| <img src="site/assets/heatmaps/feedconfigurationscreen.jpg" width="160" alt="Feed configuration"><br><sub>Feed configuration</sub> | <img src="site/assets/heatmaps/homefeeddefaultviewscreen.jpg" width="160" alt="Default feed view"><br><sub>Default feed view</sub> | <img src="site/assets/heatmaps/aboutwikipediascreen.jpg" width="160" alt="About Wikipedia"><br><sub>About Wikipedia</sub> | <img src="site/assets/heatmaps/placesfilteractivity.jpg" width="160" alt="Places filter"><br><sub>Places filter</sub> | <img src="site/assets/heatmaps/didyouknowscreen.jpg" width="160" alt="Did you know"><br><sub>Did you know</sub> |
</details>

#### What the agent suggests changing in Wikipedia
These are the 7 findings seen on the device, each with the change the review proposed. They are proposals only: no changes were made to Wikipedia.

| Severity | Seen on the device | Suggested change | Files |
|---|---|---|---|
| **high** | Tapping the featured article waits 1.2 s, then shows a full-screen Year in Review promo instead of the article | Show announcements only on the main screen, not on `PageActivity`; or only after the article has loaded and the user has scrolled | `BaseActivity.kt` |
| medium | Onboarding's only main action is a small arrow: "Learn more" draws 16× its attention, Next ranks 7th of 8, Home is 5 taps away, Skip appears only on screen 3 | A filled, labelled "Continue" button; Skip on every step; a smaller "Learn more" | `InitialOnboardingScreen.kt`, `PersonalizationScreen.kt` |
| medium | TalkBack reads six unnamed "View" switches on Community modules (and more in Settings and the donation reminder); only the small switch takes the tap | Make the whole row one toggleable control with the switch role: "Featured article, switch, on" | `Settings.kt`, `CommunityModulesScreen.kt`, `DonationReminderScreen.kt` |
| medium | Reading-list setup: an unlabelled Forward arrow, last of 4 controls, then 3.7 s with nothing on screen | A full-width "Next" button with progress once tapped; start the fetch when the screen opens | `RecommendedReadingListSourceScreen.kt` |
| medium | Tap targets under 48 dp: a 16 dp-tall contribution row (unlabelled), a 32 dp date picker, 44 dp Map/List toggles, an unlabelled My location button | Content descriptions, full-card rows, 48 dp touch areas | `UserContribItemView.kt`, `EditHistoryItemView.kt`, `PlacesFragment.kt` |
| low | The talk reply box opens with raw `@[[User:SL93\|SL93]]`, 4.4× the attention of Next | Show the mention as a chip or "@SL93"; convert to wikitext only when sending | `TalkReplyActivity.kt` |
| low | Opening the Picture of the day takes 2.3 s with nothing showing it's loading | Show the title and the feed's thumbnail at once, with progress for the details | `FilePageFragment.kt` |

There are also 2 findings suspected from the code only, on screens nobody reached. One example is an inverted check in `EditSectionActivity.kt` that can overwrite an unsaved edit after the Activity is recreated. They don't count as findings until an attack reproduces them.

The heatmaps are predictions, not eye tracking, and each finding says whether it was observed or suspected. The attacks also found a real leak in Wikipedia: `YearInReviewOnboardingActivity` instances stay alive after a forced GC (1 → 3).

#### Why a UX audit
- **It finds problems that never crash.** A user who can't find the way forward, waits 2 s after a tap, or hears "View" from TalkBack just leaves. There's no crash report, no stack trace and nothing in the logs.
- **It shows what users will miss before they miss it.** The heatmap predicts where the eye goes and gives each control a share of attention, so a main button that loses to a link or an image shows up as a number.
- **It measures instead of guessing.** Waits are timed on the device, tap targets measured in dp, labels read from the UI dump and taps counted from launch.
- **It gives a change, not a complaint.** Each finding says what to edit and in which file. It joins the same ranked `issues` list as crashes and leaks, so UX work is prioritised against everything else.
- **It keeps evidence honest.** What was seen on the device and what was only suspected in the code are labelled apart.

### Performance, memory and startup
These attacks were checked on bugs planted on a scratch branch, and on both apps as they are:

| Planted bug | Measured | Diagnosis |
|---|---|---|
| `Thread.sleep(1500)` in a click handler | `froze`: input handler asleep for 1500 of 1501 ms | Pointed to the exact line. Fixed with Claude Code (16 s) and with the API agent (22 s, $0.08) |
| Activity added to a static list | `leaked`: 1 → 7 live Activities after a forced GC | Found the static list and ruled out two other suspects |
| 5.5 s busy loop in `onCreate` | `slow_start`: cold start median 5946 ms | `performCreate` running for 5525 ms; pointed to the loop |

There were no false alarms on either app as it is.

## Tracing and checkpoints

Each partner tool and the evidence for it: [evidence/](evidence/README.md) ([neatlogs](evidence/neatlogs.md) · [Entire](evidence/entire.md) · [cfo.ai](evidence/cfo-ai.md)).

### Entire: why did this line change?
Every fix commit droid-monkey makes carries an `Entire-Checkpoint` trailer that links it to the recorded Claude Code session that wrote it. Open the commit on entire.io to see the brief droid-monkey gave the agent, the diagnosis, every step, the tokens and the diff. For code written by an agent, that answers "why did this line change, and who changed it". The checkpoints are pushed with the repo. MonkeyMart has 32, and the sessions that built droid-monkey itself are attached to this repo. The diagnoser also uses Entire's code graph (`entire graph search`) to find the symbols involved, their callers and the tests that cover them. Details: [evidence/entire.md](evidence/entire.md).

### neatlogs: one trace per run
droid-monkey is an agent that drives other agents. It runs Claude Code sessions for analysis, exploration, diagnosis and fixes, and makes its own Claude calls for planning and decisions. Most of that work happens in separate `claude -p` processes, so droid-monkey's own logs only see what each session reports back. neatlogs puts all of it in one trace per run:

- **One timeline per run.** It covers the LangGraph nodes (plan, attack, decide, diagnose, fix, verify, commit), every API call (LangChain and Anthropic instrumentation), and every Claude Code session as an LLM span.
- **Real cost.** Claude Code sessions carry the tokens and cost Claude Code reports. API calls are priced per call, so a run's spend is visible per step, not just as a total.
- **Per app and per run.** The app's package is the end user and the run id is the session, so the traces for one app or one run are easy to find.
- **Before and after.** The MonkeyMart v1 and v4 runs are side by side (in v4 every fix lands on the first try, with 13% fewer coder tokens), along with the trace of the recovery run: [evidence/neatlogs.md](evidence/neatlogs.md#before-and-after-an-improvement-in-neatlogs).

**It caught a failure we couldn't see.** On 12 Oct, neatlogs raised the incident *Explorer returns empty response without mapping screens* (`981cb137`, medium). It came from one trace: the `explore` session made no tool calls, returned an empty answer and reported zero output tokens. droid-monkey's own logs showed no error for it, and the only traceback in that day's job logs was an unrelated device error. As far as droid-monkey could tell, the session had succeeded.

**The trace pointed at the cause.** An empty LLM span with no tool calls meant the agent had stopped without touching the device. Following that into the code showed two gaps. First, an empty or errored `claude -p` result was treated as a success, and `explore` never checked it. Second, if an older `screen-map.json` was on disk, `explore` returned that map as if this run had made it. So a run that mapped nothing could look like a finished exploration.

**The fix, and how it shows in neatlogs now:**
- A `claude -p` result that is an error, or has no text and no output tokens, is now a failure with a reason. This applies to every Claude Code session droid-monkey runs: analysis, exploration, diagnosis and fixes.
- The reason is set on the span as `claude_code.error`, with ERROR status. A failure like this one now shows up in neatlogs as a named failure, not an empty success.
- `explore` retries once when the agent recorded no screens, then stops and says why.
- A map counts only if this run wrote screens to it, so an earlier map is never passed off as new.
- `tests/test_empty_sessions.py` covers the empty and errored results, the retry and keeping the earlier map.

Every incident neatlogs raises about droid-monkey is handled the same way: read the trace, find the cause, fix it, add a test, and record it here.

| Incident | What the trace showed | Cause in droid-monkey | Fix |
|---|---|---|---|
| Explorer returns empty response without mapping screens (`981cb137`, medium) | `explore` session: no tool calls, empty answer, zero output tokens | Empty or errored `claude -p` results counted as success; `explore` could return an older map as this run's | Empty or errored sessions fail with a reason (`claude_code.error`, ERROR status); explore retries once; only a map this run wrote counts |

## Business plan (cfo.ai)

droid-monkey is designed to sell as a CI add-on for Android teams. The plan is built in cfo.ai from droid-monkey's own measured costs, and the plan in turn sets how much the agent may spend on each run.

**Model (public, read-only): [cfo.ai/s/dcL7ulePdgkkZNCB](https://cfo.ai/s/dcL7ulePdgkkZNCB)** · full write-up: [docs/cfo-plan.md](docs/cfo-plan.md)

1. **Measure:** `droid-monkey econ --app-dir <app> ...` reads every run and writes [`business/drivers.csv`](business/drivers.csv). Each row is a cost or an assumption, with its source and whether it was measured. Attacks cost $0 in model spend (31 s each). The money goes on fixes: $0.56 per fix attempt and $1.12 per verified fix.
2. **Model:** cfo.ai's Ari built the model from `drivers.csv` with three plans (Free, Team at $99 and Scale at $499, each plus $3 per verified fix) and six scenarios over 18 months.
3. **Enforce:** a 70% margin floor sets the fix budget for each tier in `business/unit_economics.json`. `droid-monkey run --tier team` keeps attacking once the budget is spent but stops fixing, and each check is a `budget` GUARDRAIL span in neatlogs.

| Scenario | Scale margin | Team margin | Month-18 cash |
|---|---|---|---|
| Base | 67.1% | 69.8% | $22,618 |
| No budget cap | **−5.6%** | 45.0% | $10,852 |
| Scale repriced ($986.97) | 70.0% | 69.8% | $29,732 |
| Usage pricing | 74.0% | 80.7% | $44,764 |

**What cfo.ai showed:** the fix budget is what keeps droid-monkey profitable. Without it, Scale loses money on every run. At $499, Scale covers only about 52% of expected fix demand, and covering all of it at a 70% margin takes $986.97 a month. None of the six scenarios runs out of cash within 18 months.

## Safety

- **Consent:** no code changes without your explicit yes for each attempt, or `--auto-fix`.
- **Isolation:** analysis and exploration work on a **copy** of your source. Fixes happen on a separate branch, and you're returned to yours at the end.
- **Only the fix is kept:** droid-monkey keeps only the coding agent's edits inside `app/src` and discards anything else.
- **No arbitrary commands:** attack specs are built only from validated building blocks. The coding agent never gets a shell, except Codex inside its sandbox.
- **No live-backend actions:** droid-monkey never logs in, signs up, posts or pays on a live backend ([test accounts](#signed-in-screens-test-accounts)).
- **Same code, same build:** droid-monkey never diagnoses a build that wasn't built from the source it's reading. It rebuilds when they differ.
- **Your keys stay put:** keys are read from `~/.droid-monkey/.env`, never exported globally, and stripped from the coding agent's environment.

## Known limitations

- Codex mode is implemented from OpenAI's docs but hasn't been tested end to end.
- API-agent fixes have no Entire checkpoint yet.
- Measurements come from a debug build on an emulator, which is slower than a release build on a phone, so a pass is conservative.
- All benchmark results above are single runs.

## Building in public

Posts on X during the hackathon ([@m_shubhasai](https://x.com/m_shubhasai), #neatHack):

| When (IST) | Post | |
|---|---|---|
| Oct 11, 20:19 | [The announcement](https://x.com/m_shubhasai/status/2109295434481160496) | An agent that attacks Android apps the way real life does, fixes what breaks, and proves each fix by replaying the same attack. |
| Oct 12, 15:29 | [The CLI](https://x.com/m_shubhasai/status/2109584845097914621) | “No bananas were harmed in making this run.” |
| Oct 12, 18:15 | [A bug hunt found](https://x.com/m_shubhasai/status/2109626631447019842) | “Add ₹-5 more for free delivery.” Found by droid-monkey's hunt before customers found the loophole. |
| Oct 12, 18:50 | [The recovery path](https://x.com/m_shubhasai/status/2109635351279206501) | “Your fix is wrong.” The fix passed every test; droid-monkey revised its diagnosis and rethought the design. |
| Oct 12, 22:11 | [**The submission**](https://x.com/m_shubhasai/status/2109685797603680453) | The demo video: a team of agents that breaks Android apps on purpose and fixes them with proof. |

## Built with

Python 3.12 · LangGraph · Claude (Haiku 5.5 for reasoning, Sonnet 5.5 for code) · Claude Code / Codex · MCP · adb, emulator, Gradle · Perfetto · neatlogs · Entire · cfo.ai

Sample app: [shubhasai/MonkeyMart](https://github.com/shubhasai/MonkeyMart) · License: [MIT](LICENSE)
