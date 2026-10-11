"""MCP server that gives the coder exactly one capability beyond editing files: compile the app and its tests.

The coder has no shell (a shell would let it read docs or git history), so without this it can only find out that
its regression test does not compile when droid-monkey's verifier builds it, which costs a whole retry. This tool
runs fixed Gradle compile tasks and returns only the error lines.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .tools.gradle import failure_excerpt, gradle_env


def compile_task(app_dir: Path, module: str, variant: str, kind: str, java_home: str | None) -> str:
    v = variant[:1].upper() + variant[1:]
    task = {"unit": f":{module}:compile{v}UnitTestKotlin", "instrumented": f":{module}:compile{v}AndroidTestKotlin",
            "main": f":{module}:compile{v}Kotlin"}.get(kind)
    if task is None:
        return "kind must be one of: main, unit, instrumented"
    proc = subprocess.run(["./gradlew", task, "--console=plain", "-q"], cwd=app_dir, capture_output=True, text=True,
                          timeout=900, env=gradle_env(java_home))
    if proc.returncode == 0:
        return f"{task}: compiles"
    return f"{task}: FAILED\n" + failure_excerpt(proc.stdout + proc.stderr, app_dir, module, limit=40)


def serve(app_dir: Path, module: str, variant: str, java_home: str | None) -> None:
    server = MCPServer("build", instructions="Compile the Android app and its tests.")

    @server.tool(description="Compile the app ('main'), its JVM unit tests ('unit', src/test) or its instrumented tests "
                             "('instrumented', src/androidTest). Returns 'compiles' or the compiler errors.")
    def compile(kind: str) -> str:
        return compile_task(app_dir, module, variant, kind, java_home)

    server.run("stdio")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--app-dir", required=True, type=Path)
    p.add_argument("--module", default="app")
    p.add_argument("--variant", default="debug")
    p.add_argument("--java-home", default=None)
    a = p.parse_args(argv)
    serve(a.app_dir, a.module, a.variant, a.java_home)


if __name__ == "__main__":
    main(sys.argv[1:])
