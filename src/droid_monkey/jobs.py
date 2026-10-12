"""Running droid-monkey commands for the dashboard.

The commands a developer can run are read from droid-monkey's own CLI (click): every option with its type, choices,
default and help, so the dashboard's forms always match the CLI. A job is one command run as a child process in
the app's directory, one at a time (they share the emulator). Its output is streamed into memory and into
.droid-monkey/jobs/<id>.log, its state into <id>.json, so a job outlives a page refresh and stays in the history.

Prompts are answered from the page: the child runs with DROID_MONKEY_INTERACTIVE=1 (so `run` and `ux` ask instead
of refusing), and when the output ends in a question ("Fix it?", "Make this change?", "Press Enter") the job is
marked waiting with that question; the page sends the answer to the child's stdin. Stop sends Ctrl+C (SIGINT) to
the child's process group, and SIGKILL if it is still running 15 s later.
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import click

from . import paths

HIDDEN = {"dashboard"}                      # not offered from the dashboard
PUBLISHES = {"pr"}                          # acts outside this machine: the page asks for an explicit confirmation
GROUPS = [("Set up", ["init", "analyze"]), ("Explore", ["explore"]),
          ("Attack", ["attack", "attacks from-flows", "attacks suggest", "attacks new", "attacks list", "attacks show"]),
          ("Hunt", ["hunt"]), ("Fix", ["run"]), ("UX", ["ux"]), ("Reports", ["report", "compare", "pr"])]
PROMPT = re.compile(r"(Fix it\?|Make this change\?|Press Enter|\[y\]es / \[n\]o|\(y/n\)|\[Y/n\]|\[y/N\])[^\n]*$", re.I)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# ---- the commands, from the CLI itself ---------------------------------------------------------------------

@dataclass
class Param:
    name: str
    kind: str              # flag | choice | int | float | text | argument
    label: str             # --screens, or the argument's name
    help: str = ""
    default: object = None
    choices: list[str] = field(default_factory=list)
    required: bool = False
    on: str = ""           # flag: the option that turns it on
    off: str = ""          # flag: the option that turns it off (e.g. --no-build), if any


@dataclass
class Command:
    path: str              # "attack", "attacks from-flows"
    help: str
    params: list[Param]
    publishes: bool = False


def _param(p: click.Parameter) -> Param | None:
    if p.name == "app_dir":
        return None
    default = None if p.default is None or "Sentinel" in type(p.default).__name__ or str(p.default).startswith(
        "Sentinel") else p.default
    if isinstance(p, click.Argument):
        return Param(p.name, "argument", p.name, default=default, required=p.required and default is None)
    opt: click.Option = p  # type: ignore[assignment]
    label = max(opt.opts, key=len)
    if opt.is_flag:
        return Param(p.name, "flag", label, opt.help or "", bool(default), on=label,
                     off=opt.secondary_opts[0] if opt.secondary_opts else "")
    if isinstance(opt.type, click.Choice):
        return Param(p.name, "choice", label, opt.help or "", default, list(opt.type.choices))
    kind = ("int" if isinstance(opt.type, click.types.IntParamType) else
            "float" if isinstance(opt.type, click.types.FloatParamType) else "text")
    return Param(p.name, kind, label, opt.help or "", default)


def commands() -> dict[str, Command]:
    from .cli import main
    out = {}

    def walk(group: click.Group, prefix: str = "") -> None:
        for name, cmd in group.commands.items():
            path = (prefix + " " + name).strip()
            if isinstance(cmd, click.Group):
                walk(cmd, path)
            elif path not in HIDDEN:
                params = [x for x in (_param(p) for p in cmd.params) if x]
                out[path] = Command(path, (cmd.help or "").strip().split("\n\n")[0].replace("\n", " "), params,
                                    publishes=path in PUBLISHES)
    walk(main)
    return out


def argv(cmd: Command, values: dict) -> list[str]:
    """The command line for these form values; only known options, with types checked."""
    args = cmd.path.split()
    positional = []
    for p in cmd.params:
        v = values.get(p.name)
        if p.kind == "flag":
            on = v in (True, "true", "on", "1", 1)
            if on and not p.default:
                args.append(p.on)
            elif not on and p.default:
                if not p.off:
                    raise ValueError(f"{p.label} cannot be turned off")
                args.append(p.off)
            continue
        if v in (None, ""):
            if p.required:
                raise ValueError(f"{p.label} is required")
            continue
        v = str(v).strip()
        if p.kind == "choice" and v not in p.choices:
            raise ValueError(f"{p.label}: choose one of {p.choices}")
        if p.kind == "int" and not re.fullmatch(r"-?\d+", v):
            raise ValueError(f"{p.label}: a whole number")
        if p.kind == "float":
            try:
                float(v)
            except ValueError:
                raise ValueError(f"{p.label}: a number") from None
        if v.startswith("-") and p.kind in ("text", "argument"):
            raise ValueError(f"{p.label}: a value cannot start with '-'")  # it would be read as an option
        if p.kind == "argument":
            positional.append(v)
        else:
            args += [p.label, v]
    return args + positional


# ---- jobs ----------------------------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    command: str
    args: list[str]
    status: str = "running"        # running | waiting | done | failed | stopped
    started: float = 0.0
    ended: float | None = None
    returncode: int | None = None
    waiting: str = ""              # the question the command is asking, when status is waiting

    def public(self) -> dict:
        return asdict(self)


class Jobs:
    def __init__(self, app_dir: Path):
        self.app = app_dir.resolve()
        self.dir = paths.data_dir(self.app) / "jobs"
        self.lock = threading.Lock()
        self.current: Job | None = None
        self.proc: subprocess.Popen | None = None
        self.output = ""

    # history -----------------------------------------------------------------------------------------
    def history(self) -> list[Job]:
        out = []
        for f in sorted(self.dir.glob("*.json"), reverse=True) if self.dir.exists() else []:
            try:
                out.append(Job(**json.loads(f.read_text())))
            except (ValueError, TypeError):
                continue
        return out

    def get(self, job_id: str) -> Job | None:
        if self.current and self.current.id == job_id:
            return self.current
        f = self.dir / f"{job_id}.json"
        return Job(**json.loads(f.read_text())) if f.exists() else None

    def log(self, job_id: str) -> str:
        if self.current and self.current.id == job_id:
            return self.output
        f = self.dir / f"{job_id}.log"
        return f.read_text(errors="replace") if f.exists() else ""

    def _save(self, job: Job) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{job.id}.json").write_text(json.dumps(job.public(), indent=1))

    # running ------------------------------------------------------------------------------------------
    def busy(self) -> Job | None:
        return self.current if self.current and self.current.status in ("running", "waiting") else None

    def start(self, command: str, args: list[str], cmdline: list[str] | None = None) -> Job:
        """Run `droid-monkey <args>` (or, in tests, `cmdline`) in the app's directory."""
        with self.lock:
            if self.busy():
                raise RuntimeError(f"'{self.current.command}' is still running: stop it or wait for it to finish")
            job = Job(id=time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"\W+", "-", command),
                      command=command, args=args, started=time.time())
            env = {**os.environ, "NO_COLOR": "1", "PYTHONUNBUFFERED": "1", "DROID_MONKEY_INTERACTIVE": "1",
                   "DROID_MONKEY_NO_BANNER": "1"}
            self.proc = subprocess.Popen(cmdline or [sys.executable, "-c", "from droid_monkey.cli import main; main()", *args],
                                         cwd=self.app, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, start_new_session=True)
            self.current, self.output = job, ""
            self._save(job)
            threading.Thread(target=self._pump, args=(job, self.proc), daemon=True).start()
            return job

    def _pump(self, job: Job, proc: subprocess.Popen) -> None:
        logf = (self.dir / f"{job.id}.log").open("a")
        fd = proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 4096)  # partial lines too: a prompt has no newline until it is answered
            if not chunk:
                break
            text = ANSI.sub("", chunk.decode(errors="replace")).replace("\r\n", "\n")
            if "neatlogs - INFO" in text:
                text = "\n".join(line for line in text.split("\n") if "neatlogs - INFO" not in line)
            with self.lock:
                self.output += text
                tail = self.output[-400:]
                m = PROMPT.search(tail.rsplit("\n", 1)[-1]) if not tail.endswith("\n") else None
                if m:
                    job.status, job.waiting = "waiting", tail.rsplit("\n", 1)[-1].strip()
                    self._save(job)
            logf.write(text)
            logf.flush()
        code = proc.wait()
        logf.close()
        with self.lock:
            job.returncode, job.ended, job.waiting = code, time.time(), ""
            if job.status != "stopped":
                job.status = "done" if code == 0 else "failed"
            self._save(job)

    def answer(self, job_id: str, text: str) -> None:
        with self.lock:
            job = self.busy()
            if not job or job.id != job_id or not self.proc or not self.proc.stdin:
                raise RuntimeError("that job is not running")
            self.proc.stdin.write((text.strip() + "\n").encode())
            self.proc.stdin.flush()
            self.output += f"{text.strip()}\n"
            job.status, job.waiting = "running", ""
            self._save(job)

    def stop(self, job_id: str) -> None:
        with self.lock:
            job = self.busy()
            if not job or job.id != job_id or not self.proc:
                raise RuntimeError("that job is not running")
            job.status = "stopped"
            self._save(job)
            proc = self.proc
        try:
            os.killpg(proc.pid, signal.SIGINT)  # like Ctrl+C: droid-monkey restores the device on the way out
        except ProcessLookupError:
            return

        def hard_stop():
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        threading.Timer(15, hard_stop).start()


# ---- how the dashboard presents the commands --------------------------------------------------------------

@dataclass
class Guide:
    path: str
    title: str                  # what it does, in plain words
    what: str                   # one or two sentences
    stage: str
    uses: list[str]             # device | model | code | publishes
    cost: str = ""              # rough time and money
    requires: list[str] = field(default_factory=list)  # files in .droid-monkey (or the repo) it needs first
    primary: list[str] = field(default_factory=list)   # options shown up front; the rest under "More options"
    labels: dict[str, str] = field(default_factory=dict)  # option -> plain label


STAGES = ["Set up", "Map", "Find bugs", "Fix", "UX", "Share", "Tools"]
GUIDES = [
    Guide("init", "Connect droid-monkey to this app", "Writes monkey.yaml for the repo and checks the emulator, "
          "Gradle, the coding agent and your keys.", "Set up", [], "seconds", [], ["force"],
          {"force": "Overwrite an existing monkey.yaml"}),
    Guide("analyze", "Read the code", "A coding agent reads the repo and writes the user flows, their risks and "
          "the checklist of screens. Every later step uses it.", "Set up", ["model"], "~5 min · ~$1–4 on a large app",
          ["../monkey.yaml"], ["static_only"], {"static_only": "Free static scan only (no agent)"}),
    Guide("explore", "Map the app's screens", "Drives the app on the emulator and records every screen, how to "
          "reach it and the code behind it. Attacks, hunts and the UX audit replay these paths.", "Map",
          ["device", "model"], "≤10 min by default · ~$2–7", ["../monkey.yaml"], ["mode", "minutes", "max_screens"],
          {"mode": "How to explore", "minutes": "Time limit (minutes)", "max_screens": "Screen limit",
           "check_paths": "Replay every path afterwards", "from_partial": "Don't explore: re-check the saved map"}),
    Guide("hunt", "Find bugs in normal use", "Walks every mapped journey like a user, no fault injected: crashes, "
          "swallowed exceptions, jank, 'null' on screen, and (with a model) numbers that don't add up.", "Find bugs",
          ["device", "model"], "~1 min per journey · cents", ["screen-map.json"], ["use_model"],
          {"use_model": "Also check each journey's numbers with a model", "build": "Rebuild the app first"}),
    Guide("attack", "Attack with real-world faults", "Rotation, process death, network loss, revoked permissions, "
          "low memory, a loaded CPU, cold starts, on the riskiest screens. Finds bugs, changes no code.", "Find bugs",
          ["device"], "~30 s per attack · free (+cents with diagnosis)", ["screen-map.json"],
          ["name", "screens", "diagnose"],
          {"name": "Which attack", "screens": "Screens per attack", "diagnose": "Diagnose each failure (suggests a fix)",
           "target": "Screen id (override)", "action": "Trigger step (override)", "build": "Rebuild the app first",
           "re_explore": "Explore again first"}),
    Guide("attacks from-flows", "Design attacks from the user flows", "Turns the risks the analysis found on each "
          "user flow into an attack, tries it on the device and saves it.", "Find bugs", ["device", "model"],
          "~1–2 min per attack · cents", ["flows.json", "screen-map.json"], ["limit", "all_flows"],
          {"limit": "How many to design", "all_flows": "Include flows the explorer never reached",
           "try_on_device": "Try each one on the emulator before saving"}),
    Guide("run", "Find, fix and verify", "The full loop: attack, diagnose the root cause, ask you before every fix, "
          "let the coding agent fix it, replay the identical attack and commit only if it passes.", "Fix",
          ["device", "model", "code"], "~5–20 min · cents to a few $", ["screen-map.json"], ["only", "screens", "auto_fix"],
          {"only": "Only these attacks (comma-separated)", "screens": "Screens per attack",
           "auto_fix": "Fix without asking (CI only)", "agent": "Coding agent", "resume_id": "Resume a run (its id)"}),
    Guide("fix", "Fix one issue", "Fixes a single hunt or UX issue from the Issues page: asks you first, re-checks it "
          "on the device, commits only if it is gone. Attack failures use Find, fix and verify.", "Fix",
          ["device", "model", "code"], "a few min · cents", ["screen-map.json"], ["issue_id"], {"issue_id": "Issue id"}),
    Guide("issues", "Rank all issues", "Ranks every open issue by user impact with Jev.", "Tools", [], "seconds",
          [], ["rerank"], {"rerank": "Rank every issue again"}),
    Guide("ux", "Audit the UX", "Reviews every screen: touch targets, labels, predicted attention, measured "
          "waits; then asks you about each change.", "UX", ["model", "code"], "~3 min · ~$1",
          ["screen-map.json"], ["apply"], {"apply": "Ask me about each finding and make the ones I accept"}),
    Guide("report", "Build a run's report", "Writes the PR body, score card and visual report for a finished run.",
          "Share", [], "seconds", [], ["run_id"], {"run_id": "Run"}),
    Guide("compare", "Compare two runs", "A before/after table, e.g. a naive baseline against a normal run.",
          "Share", [], "seconds", [], ["run_a", "run_b"], {"run_a": "Run A", "run_b": "Run B"}),
    Guide("pr", "Open a pull request", "Pushes the run's branch with its Entire checkpoints and opens a GitHub PR "
          "with the evidence.", "Share", ["publishes"], "seconds", [], ["run_id", "draft"],
          {"run_id": "Run", "draft": "Open as a draft", "base": "Base branch"}),
    Guide("attacks suggest", "Let the designer invent attacks", "The designer agent writes new attack types for this "
          "app from its analysis and screen map.", "Tools", ["device", "model"], "a few min · cents",
          ["screen-map.json"], ["count"], {"count": "How many"}),
    Guide("attacks new", "Describe an attack in plain words", "The designer agent turns your sentence into a "
          "validated attack and tries it.", "Tools", ["device", "model"], "~1–2 min · cents", ["screen-map.json"],
          ["prompt"], {"prompt": "Describe the attack"}),
    Guide("attacks list", "List attacks", "Built-in attacks and this app's saved ones.", "Tools", [], "seconds"),
    Guide("attacks show", "Show a saved attack", "Prints a saved attack spec.", "Tools", [], "seconds", [],
          ["name"], {"name": "Attack"}),
]
