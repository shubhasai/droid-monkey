"""Which source the app on the device was built from.

An attack measures the installed APK while the diagnoser reads the repo, so the two must be the same code; otherwise a
trace from one build gets explained with the source of another. Every install droid-monkey makes records a
fingerprint of the source (HEAD plus uncommitted changes, droid-monkey's own files excluded) and the device's install
time for the package; `check` says why the device does not match the current source, or None when it does.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from .paths import data_dir

EXCLUDE = ":(exclude).droid-monkey"


def _git(app_dir: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=app_dir, capture_output=True, stdin=subprocess.DEVNULL,
                          timeout=60).stdout


def source_fingerprint(app_dir: Path) -> str:
    h = hashlib.sha256()
    h.update(_git(app_dir, "rev-parse", "HEAD"))
    h.update(_git(app_dir, "diff", "HEAD", "--", ".", EXCLUDE))
    for name in sorted(_git(app_dir, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", EXCLUDE)
                       .decode(errors="replace").split("\0")):
        f = app_dir / name
        if name and f.is_file():
            h.update(name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def _installed_at(adb, package: str) -> str:
    out = adb.shell(f"dumpsys package {package}", check=False)
    m = re.search(r"lastUpdateTime=([^\r\n]+)", out)
    return m.group(1).strip() if m else ""


def _record_file(app_dir: Path) -> Path:
    return data_dir(app_dir) / "installed.json"


def record(app_dir: Path, adb, package: str) -> None:
    """Call right after installing a build of `app_dir`."""
    path = _record_file(app_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"package": package, "source": source_fingerprint(app_dir),
                                "installed_at": _installed_at(adb, package)}, indent=2))


def check(app_dir: Path, adb, package: str) -> str | None:
    """None if the installed app was built by droid-monkey from the current source; otherwise why not."""
    path = _record_file(app_dir)
    if not path.exists():
        return "droid-monkey has no record of building the installed app"
    rec = json.loads(path.read_text())
    if rec.get("installed_at") != _installed_at(adb, package):
        return "the app on the device was installed by something else since droid-monkey's last build"
    if rec.get("source") != source_fingerprint(app_dir):
        return "the source changed since the installed app was built"
    return None
