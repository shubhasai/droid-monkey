"""Git operations on the app repo for a run: its own branch, one commit per verified fix, revert on failure."""

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(app_dir: Path, *args: str, check: bool = True, strip: bool = True) -> str:
    # Detached from the terminal: git hooks (Entire's asks "Link this commit to session context?") must never wait
    # on a prompt the developer cannot see; without a terminal they take their non-interactive default.
    proc = subprocess.run(["git", *args], cwd=app_dir, capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL, start_new_session=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip() if strip else proc.stdout


def parse_porcelain_z(out: str) -> list[str]:
    """Paths from `git status --porcelain=v1 -z`. Entries are 'XY path' separated by NUL; a rename or copy is
    followed by one more NUL-separated field (the old path), which is skipped. Never strip: the status column can
    start with a space, and trimming it shifts every path by one character."""
    paths, fields, i = [], out.split("\0"), 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in status or "C" in status:
            i += 1  # skip the original path of a rename/copy
    return paths


def _status(app_dir: Path, *pathspec: str) -> list[str]:
    out = _git(app_dir, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *pathspec, strip=False)
    return parse_porcelain_z(out)


def head(app_dir: Path) -> str:
    return _git(app_dir, "rev-parse", "HEAD")


def current_branch(app_dir: Path) -> str:
    return _git(app_dir, "rev-parse", "--abbrev-ref", "HEAD")


def dirty_paths(app_dir: Path, under: str) -> list[str]:
    """Changed or untracked files under `under` (e.g. the app module's src)."""
    return _status(app_dir, under)


def start_run_branch(app_dir: Path, name: str, module: str) -> str:
    """Create and switch to the run branch from the current HEAD. Refuses if the app's sources have local edits."""
    dirty = dirty_paths(app_dir, f"{module}/src")
    if dirty:
        raise GitError(f"uncommitted changes in {module}/src ({dirty[:5]}): commit or stash them first")
    exists = _git(app_dir, "rev-parse", "--verify", "--quiet", name, check=False)
    if exists:
        _git(app_dir, "checkout", name)
    else:
        _git(app_dir, "checkout", "-b", name)
    return head(app_dir)


def checkout(app_dir: Path, branch: str) -> None:
    """Switch branches, carrying uncommitted edits along (git refuses if they would conflict)."""
    _git(app_dir, "checkout", branch)
    if current_branch(app_dir) != branch:
        raise GitError(f"could not switch to {branch}")


def changed_files(app_dir: Path) -> list[str]:
    return _status(app_dir)


def file_states(app_dir: Path) -> dict[str, str]:
    """Changed/untracked paths mapped to a content hash, to tell later which files a tool actually touched."""
    states = {}
    for p in _status(app_dir):
        f = app_dir / p
        states[p] = _git(app_dir, "hash-object", "--", p) if f.is_file() else "<deleted>"
    return states


def discard(app_dir: Path, paths: list[str]) -> None:
    """Throw away working-tree changes to these paths (tracked: restore; untracked: delete)."""
    for p in paths:
        tracked = _git(app_dir, "ls-files", "--", p)
        if tracked:
            _git(app_dir, "checkout", "--", p)
        else:
            (app_dir / p).unlink(missing_ok=True)


def commit(app_dir: Path, paths: list[str], message: str) -> tuple[str, str | None]:
    """Commit exactly these paths. Returns (sha, Entire checkpoint id if the commit got one)."""
    _git(app_dir, "add", "--", *paths)
    _git(app_dir, "commit", "-m", message)
    body = _git(app_dir, "log", "-1", "--format=%B")
    checkpoint = next((ln.split(":", 1)[1].strip() for ln in body.splitlines() if ln.startswith("Entire-Checkpoint:")), None)
    return head(app_dir), checkpoint


def reset_hard(app_dir: Path, sha: str) -> None:
    """Back to a known-good commit on the run branch (used when a fix breaks the build)."""
    _git(app_dir, "reset", "--hard", sha)
