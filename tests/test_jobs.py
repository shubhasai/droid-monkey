import sys
import time

import pytest

from droid_monkey import jobs


def _wait(js, job, until, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if until(js.get(job.id)):
            return js.get(job.id)
        time.sleep(0.05)
    raise AssertionError(f"timed out: {js.get(job.id)}")


def test_commands_come_from_the_cli():
    cmds = jobs.commands()
    assert {"init", "analyze", "explore", "attack", "attacks from-flows", "run", "ux", "pr"} <= set(cmds)
    assert "dashboard" not in cmds and cmds["pr"].publishes
    explore = {p.name: p for p in cmds["explore"].params}
    assert explore["mode"].kind == "choice" and "manual" in explore["mode"].choices
    assert explore["max_screens"].kind == "int" and explore["check_paths"].kind == "flag"


def test_argv_checks_values():
    attack = jobs.commands()["attack"]
    assert jobs.argv(attack, {"name": "rotation", "screens": "1", "diagnose": True}) == \
        ["attack", "--screens", "1", "--diagnose", "rotation"]
    for bad in ({"screens": "1; rm -rf /"}, {"name": "--help"}):
        with pytest.raises(ValueError):
            jobs.argv(attack, bad)
    with pytest.raises(ValueError):
        jobs.argv(jobs.commands()["attacks new"], {})  # its prompt argument is required


def test_a_prompt_is_answered_from_the_page(tmp_path):
    js = jobs.Jobs(tmp_path)
    script = ("import sys\nprint('diagnosis ready', flush=True)\n"
              "sys.stdout.write('Fix it? [y]es / [n]o / [a]ll remaining: '); sys.stdout.flush()\n"
              "a = sys.stdin.readline().strip()\nprint('answer=' + a)\n")
    job = js.start("run", ["run"], cmdline=[sys.executable, "-c", script])
    waiting = _wait(js, job, lambda j: j.status == "waiting")
    assert "Fix it?" in waiting.waiting
    with pytest.raises(RuntimeError):
        js.start("attack", ["attack"])                 # one job at a time
    js.answer(job.id, "n")
    done = _wait(js, job, lambda j: j.status in ("done", "failed"))
    assert done.status == "done" and "answer=n" in js.log(job.id)
    assert (tmp_path / ".droid-monkey" / "jobs" / f"{job.id}.log").exists()
    assert [j.id for j in js.history()] == [job.id]


def test_stop(tmp_path):
    js = jobs.Jobs(tmp_path)
    job = js.start("explore", ["explore"], cmdline=[sys.executable, "-c", "import time\nprint('go',flush=True)\ntime.sleep(60)"])
    _wait(js, job, lambda j: "go" in js.log(job.id) or True)
    time.sleep(0.3)
    js.stop(job.id)
    assert _wait(js, job, lambda j: j.ended is not None).status == "stopped"
