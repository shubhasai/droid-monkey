"""The diagnoser reads the repo while the attack measures the installed app: they must be the same code."""

import subprocess

from droid_monkey import provenance


class FakeAdb:
    def __init__(self):
        self.installed_at = "2026-10-12 02:00:00"

    def shell(self, cmd, check=True, **kw):
        return f"lastUpdateTime={self.installed_at}\n"


def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "Main.kt").write_text("fun main() {}\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    return tmp_path


def test_matches_until_the_source_or_the_install_changes(tmp_path):
    app, adb = repo(tmp_path), FakeAdb()
    assert provenance.check(app, adb, "p") == "droid-monkey has no record of building the installed app"
    provenance.record(app, adb, "p")
    assert provenance.check(app, adb, "p") is None
    (app / ".droid-monkey" / "runs").mkdir(parents=True)
    (app / ".droid-monkey" / "runs" / "x.json").write_text("{}")   # droid-monkey's own output does not count
    assert provenance.check(app, adb, "p") is None
    (app / "Main.kt").write_text("fun main() { Thread.sleep(5500) }\n")
    assert provenance.check(app, adb, "p") == "the source changed since the installed app was built"
    (app / "Main.kt").write_text("fun main() {}\n")
    adb.installed_at = "2026-10-12 03:00:00"
    assert "installed by something else" in provenance.check(app, adb, "p")
