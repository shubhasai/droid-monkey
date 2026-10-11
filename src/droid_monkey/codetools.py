"""Code tools for droid-monkey's own agents (diagnoser, designer): read, search and graph-search the app's source.

Only the app module's Kotlin/Java sources are visible. Docs, scripts, build output and droid-monkey's own files are
not: they can be stale, and in a demo app they can describe the very bugs being diagnosed.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

from .profile import source_files


class CodeTools:
    def __init__(self, app_dir: Path, module: str):
        self.app_dir = app_dir
        self.files = {str(f.relative_to(app_dir)): f for f in source_files(app_dir, module)}

    def read_code(self, path: str, start: int = 1, end: int = 200) -> str:
        f = self.files.get(path.lstrip("./"))
        if f is None:
            return f"not readable: {path}. Readable source files: {sorted(self.files)}"
        lines = f.read_text(errors="replace").splitlines()
        start, end = max(1, start), min(len(lines), end, start + 300)
        return "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))

    def search_code(self, pattern: str) -> str:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        hits = []
        for rel, f in self.files.items():
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:160]}")
        return "\n".join(hits[:40]) or "no matches"

    def graph_search(self, query: str) -> str:
        """Entire's code graph (symbols, callers, covering tests), restricted to the app's source files."""
        entire = shutil.which("entire") or "/opt/homebrew/bin/entire"
        try:
            proc = subprocess.run([entire, "graph", "search", "--query", query, "--repo", str(self.app_dir),
                                   "--top-k", "12", "--format", "json", "--profile", "full"],
                                  capture_output=True, text=True, timeout=60)
            data = json.loads(proc.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            return f"graph search unavailable ({e}); use search_code instead"
        out = []
        for r in data.get("results", []):
            path = r.get("file_path", "")
            if path not in self.files:
                continue  # docs, scripts, build files: not evidence about the code
            name = r.get("qualified_name") or r.get("symbol_name") or ""
            out.append(f"{path}:{r.get('start_line')}-{r.get('end_line')} {r.get('kind', '')} {name} "
                       f"[{r.get('signals', '')}]\n{r.get('snippet', '')[:400]}")
        return "\n\n".join(out[:6]) or "no source matches"
