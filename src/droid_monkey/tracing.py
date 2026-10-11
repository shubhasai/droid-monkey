"""neatlogs tracing. init() must run before LangChain, LangGraph or the Anthropic SDK are imported."""

import functools
import os
from pathlib import Path

from dotenv import dotenv_values

_state = {"ready": False, "enabled": False}


def load_keys() -> None:
    """Keys: real env vars win, then ~/.droid-monkey/.env, then the git-ignored .env in this checkout."""
    for env in (Path.home() / ".droid-monkey" / ".env", Path(__file__).resolve().parents[2] / ".env"):
        if env.exists():
            for k, v in dotenv_values(env).items():
                if v and not os.environ.get(k):
                    os.environ[k] = v


def init(workflow: str = "droid-monkey", tags: list[str] | None = None) -> bool:
    if _state["ready"]:
        return _state["enabled"]
    _state["ready"] = True
    load_keys()
    if not os.environ.get("NEATLOGS_API_KEY"):
        return False
    import neatlogs

    neatlogs.init(workflow_name=workflow, instrumentations=["langgraph", "langchain", "anthropic"], tags=tags or [])
    _state["enabled"] = True
    return True


def span(kind: str, name: str, **kw):
    """neatlogs @span when tracing is on, a no-op otherwise."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*a, **k):
            if _state["enabled"]:
                from neatlogs import span as nspan
                return nspan(kind=kind, name=name, **kw)(fn)(*a, **k)
            return fn(*a, **k)
        return inner
    return wrap


def flush() -> None:
    if _state["enabled"]:
        import neatlogs
        neatlogs.flush()
