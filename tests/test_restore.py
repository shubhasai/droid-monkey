import subprocess
from types import SimpleNamespace

from droid_monkey import loop as loop_mod
from droid_monkey.tools import git


def _sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def test_failed_fix_is_undone_but_developer_edits_outside_src_stay(tmp_path, monkeypatch):
    repo = tmp_path
    (repo / "app" / "src").mkdir(parents=True)
    (repo / "gradle").mkdir()
    (repo / "app" / "src" / "Main.kt").write_text("fun main() {}\n")
    (repo / "gradle" / "libs.versions.toml").write_text("agp = 1\n")
    _sh(repo, "git", "init", "-q")
    _sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    base = git.rev(repo, "HEAD")
    (repo / "gradle" / "libs.versions.toml").write_text("agp = 2\n")      # the developer's own edit
    (repo / "app" / "src" / "Main.kt").write_text("fun main() { fix() }\n")  # the coding agent's failed fix
    (repo / "app" / "src" / "FixTest.kt").write_text("class FixTest\n")    # and its new test file

    monkeypatch.setattr(loop_mod.gradle, "build", lambda *a, **k: SimpleNamespace(ok=False, apk=None))
    runner = loop_mod.Loop.__new__(loop_mod.Loop)
    runner.cfg = SimpleNamespace(app_dir=repo, module="app", variant="debug", java_home=None)
    runner._restore_last_good({"last_good": base})

    assert (repo / "app" / "src" / "Main.kt").read_text() == "fun main() {}\n"
    assert not (repo / "app" / "src" / "FixTest.kt").exists()
    assert (repo / "gradle" / "libs.versions.toml").read_text() == "agp = 2\n"
