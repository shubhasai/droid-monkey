# Installation and setup

From nothing to droid-monkey attacking your Android app. About 20 minutes if you already have Android Studio.

Written for macOS (where droid-monkey is developed and tested). On Linux, set `ANDROID_HOME` and the same steps apply.

## 1. What you need

| | Required? | Used for |
|---|---|---|
| Python 3.12+ and [uv](https://docs.astral.sh/uv/) | Yes | Installing and running droid-monkey |
| Android SDK: platform-tools (`adb`), the emulator, one AVD | Yes | Running the app, the attacks and the hunt |
| A JDK that your app's Gradle accepts | Yes | Building the app (Android Studio ships one) |
| An Anthropic API key | Yes | droid-monkey's own reasoning: plan, decide, diagnose, the hunt's journey review, the designer. Haiku 5.5 by default, a few cents per run |
| One coding agent: Claude Code, Codex, or the API key above | Yes | `analyze`, `explore`, and the fixes |
| [Entire](https://entire.io) with its `graph` plugin | Recommended | Checkpoints on every fix commit; the code graph the diagnoser searches |
| `gh`, logged in | Optional | `droid-monkey pr` |
| A neatlogs API key | Optional | Tracing every run |
| `shark-cli` (LeakCanary's analyser) | Optional | Reference chains for memory leaks found by `low_memory` |

Perfetto's `trace_processor` and the fast screen reader (uiautomator2) come with droid-monkey; nothing to install.

## 2. Install droid-monkey

Install uv if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then droid-monkey, as a command available everywhere:

```bash
uv tool install git+https://github.com/shubhasai/droid-monkey
```

Check it:

```bash
droid-monkey --version
```

**From a clone instead** (to change droid-monkey itself):

```bash
git clone https://github.com/shubhasai/droid-monkey && cd droid-monkey && uv sync
```

Then run it as `uv run droid-monkey ...` from the clone, or point it at an app with `--app-dir`. Tests: `uv run pytest`.

## 3. Android SDK and an emulator

1. Install [Android Studio](https://developer.android.com/studio). It brings the SDK, `adb`, the emulator and a JDK.
2. In **Device Manager**, create one virtual device (a recent Pixel with a Google APIs image works well) and note its
   name, e.g. `Pixel_10`.
3. Make `adb` and `emulator` reachable. droid-monkey finds them in `~/Library/Android/sdk` on its own; for your own
   shell (and on Linux), add:

   ```bash
   export ANDROID_HOME="$HOME/Library/Android/sdk"
   export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH"
   ```

4. Check the device list:

   ```bash
   emulator -list-avds
   ```

You don't have to start the emulator yourself: if none is running, droid-monkey boots the AVD named in `monkey.yaml`
(or the first one). Physical phones aren't supported; the attacks need an emulator.

## 4. Keys

All keys live in **`~/.droid-monkey/.env`**, outside every repo. droid-monkey reads them itself, never exports them
to your shell, and strips them from the coding agent's environment.

```bash
mkdir -p ~/.droid-monkey && touch ~/.droid-monkey/.env && chmod 600 ~/.droid-monkey/.env
```

Then open the file and add:

```bash
DROID_MONKEY_API_KEY=sk-ant-...      # required: Anthropic key for droid-monkey's own reasoning
NEATLOGS_API_KEY=...                 # optional: tracing
```

Use `DROID_MONKEY_API_KEY`, **not** `ANTHROPIC_API_KEY`: Claude Code uses `ANTHROPIC_API_KEY` whenever it is set,
which would move your Claude Code sessions from your plan to API billing.

Credentials for a test account (see [Signed-in apps](#signed-in-apps)) go in the same file.

## 5. A coding agent

droid-monkey uses one coding agent to read your code, drive the app while exploring, and make fixes. `auto` (the
default) picks the first one that is set up, in this order:

| Agent | Set up | Notes |
|---|---|---|
| **Claude Code** (recommended) | Install from [claude.com/claude-code](https://claude.com/claude-code), then run `claude` once and log in | Uses your Claude plan. Entire checkpoints every fix |
| Codex | `npm install -g @openai/codex`, then `codex login` | Checkpoints with `entire agent add codex` |
| API agent | Nothing beyond `DROID_MONKEY_API_KEY` | droid-monkey's own tool loop on the Anthropic API |

Pick one explicitly with `agent: claude | codex | api` in `monkey.yaml`, or `--agent` on a command.

## 6. Entire (recommended)

```bash
brew install --cask entireio/tap/entire
entire login
entire plugin install graph
```

Then, once per app repo:

```bash
entire enable --agent claude-code
```

With Entire enabled, every fix commit droid-monkey makes carries an `Entire-Checkpoint` trailer that links it to the
recorded coding session, and the diagnoser searches Entire's code graph. `git push` sends the checkpoints too.

## 7. Optional extras

GitHub CLI, for `droid-monkey pr`:

```bash
brew install gh && gh auth login
```

shark-cli, for memory-leak reference chains:

```bash
brew install leakcanary-shark
```

(or unpack its release zip into `~/.droid-monkey/tools/`). It's a Java program; droid-monkey runs it with
`JAVA_HOME`, or Android Studio's JDK when that's unset.

## 8. Set up your app

From the root of your Android app's repo (where `gradlew` is):

```bash
droid-monkey init
```

`init` detects the app module, its `applicationId`, the build variant, an AVD, and a JDK your Gradle can run on, and
writes **`monkey.yaml`**. It also creates **`.droid-monkey/`**, where everything droid-monkey produces goes (runs are
git-ignored automatically). Then it checks everything above:

```
(^.^) ok      adb
(^.^) ok      emulator running
(^.^) ok      gradlew
(^.^) ok      claude (Claude Code)
(x_x) MISSING entire graph plugin  (entire plugin install graph)
...
```

Fix anything marked `MISSING` and run `droid-monkey init` again (it keeps your `monkey.yaml`; `--force` rewrites it).

### monkey.yaml

Everything app-specific lives here; droid-monkey's code knows nothing about any particular app. `init` fills in the
first block; the rest is optional.

```yaml
package: com.example.shop        # applicationId
module: app
variant: debug
avd: Pixel_10
# java_home: /Library/Java/...    # only when your Gradle needs an older JDK; init sets it

# optional
agent: auto                       # auto | claude | codex | api
screens_per_attack: 2             # each built-in attack runs on its best N screens
explore_minutes: 10               # agent exploration stops here and keeps what it mapped
explore_max_screens: 60
live_backend: true                # see "Safety" below
allow_actions: []                 # labels that are safe to tap, e.g. ["Place order"] on a fake backend
llm:
  provider: anthropic             # or openai_compatible (with base_url)
  model: claude-haiku-5-5
```

## 9. First run

```bash
droid-monkey analyze                      # reads the code: ANALYSIS.md and the user flows worth testing
droid-monkey explore                      # drives the app: the screen map, with the files behind each screen
droid-monkey hunt                         # bugs in normal use, no fault injected
droid-monkey attack builtin --diagnose    # real-world faults; shows what breaks and the suggested fix
droid-monkey run                          # the full loop; asks you before every fix
droid-monkey dashboard                    # a live page over everything in .droid-monkey/
```

The dashboard works right after `init` and refreshes itself as runs write into `.droid-monkey/`. It listens on
`127.0.0.1` only.

droid-monkey **never changes your code without your yes**: every fix shows the issue, the evidence measured on the
device, the root cause and the suggested change, and waits. Fixes go on a `droid-monkey/<run-id>` branch, one commit
per fix, and you're put back on your own branch at the end. `--auto-fix` skips the question (for CI).

## Signed-in apps

To reach screens behind a login, give droid-monkey a **test account**. It signs in after every launch:

```yaml
entry_steps: ["tap text:Log in", "type username=${DM_TEST_USER}", "type password=${DM_TEST_PASSWORD}", "tap login_button"]
entry_check: "text:Your orders"   # something only a signed-in user sees; the run stops if sign-in failed
```

Put `DM_TEST_USER` and `DM_TEST_PASSWORD` in `~/.droid-monkey/.env`, never in `monkey.yaml`.

## Safety

- With `live_backend: true` (the default), droid-monkey refuses to tap anything that would act on a real backend:
  sign up, log in, post, publish, pay, place an order. Set `live_backend: false` only for a build that talks to a test
  or fake backend, or list specific safe labels in `allow_actions`.
- While signed in, it never taps Log out or Sign out.
- The fixing agent may only edit the app's sources (`<module>/src`). It has no shell, can't read docs or git history,
  and can't change build files.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `adb not found` | Install Android platform-tools, or set `ANDROID_HOME` |
| `no monkey.yaml in …` | Run `droid-monkey init` in the app repo's root first |
| The build fails with a Java or Gradle version error | Set `java_home:` in `monkey.yaml` to a JDK 17 or 21 (`/usr/libexec/java_home -V` lists them) |
| `refused to tap 'Buy now' …` | The safety guard: on a fake backend add the label to `allow_actions`, or set `live_backend: false` |
| `the device could not install the test (… MB free)` | The emulator is out of storage: wipe its data in Device Manager, or give the AVD a bigger disk |
| A fix is suggested but not applied when piping output | Approval needs a terminal; run interactively, or pass `--auto-fix` |
| `DROID_MONKEY_API_KEY is not set` | Add it to `~/.droid-monkey/.env` (step 4) |
| `no coding agent available` | Set up one of the agents in step 5 |
| A run was killed | `droid-monkey run --resume <run-id>` continues without redoing finished attacks |
