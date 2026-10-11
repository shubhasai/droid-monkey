"""neatlogs tracing. init() must run before LangChain, LangGraph or the Anthropic SDK are imported."""

import functools
import logging
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

    # Export retries are logged by OpenTelemetry on stderr; keep the console readable if the backend is down.
    logging.getLogger("opentelemetry").setLevel(logging.CRITICAL)
    neatlogs.init(workflow_name=workflow, instrumentations=["langgraph", "langchain", "anthropic"], tags=tags or [])
    _price_api_calls()
    _state["enabled"] = True
    return True


def _price_api_calls() -> None:
    """droid-monkey's model calls go through LangChain, traced by OpenInference's LangChain instrumentation. Those LLM
    spans carry OpenInference's llm.* attributes, while neatlogs prices and counts neatlogs.llm.* ones, so the
    dashboard showed 0 tokens and $0.00. After OpenInference fills a span, copy the model and token counts across
    and add the cost from Anthropic's published prices (pricing.py)."""
    try:
        from openinference.instrumentation.langchain import _tracer as oi
    except ImportError:
        return
    original = getattr(oi, "_update_span", None)
    if original is None or getattr(original, "_droid_monkey", False):
        return

    def update_span(span, run):
        original(span, run)
        try:
            if getattr(run, "run_type", "") == "llm":
                _add_neatlogs_usage(span)
        except Exception:  # pricing is best effort; never break the traced call
            pass

    update_span._droid_monkey = True
    oi._update_span = update_span


def _add_neatlogs_usage(span) -> None:
    from .pricing import cost_usd
    attrs = getattr(span, "attributes", None) or {}
    model = attrs.get("llm.model_name") or ""
    prompt = int(attrs.get("llm.token_count.prompt") or 0)
    out = int(attrs.get("llm.token_count.completion") or 0)
    cache_read = int(attrs.get("llm.token_count.prompt_details.cache_read") or 0)
    cache_write = int(attrs.get("llm.token_count.prompt_details.cache_write") or 0)
    if model:
        span.set_attribute("neatlogs.llm.model_name", model)
    span.set_attribute("neatlogs.llm.token_count.prompt", prompt)
    span.set_attribute("neatlogs.llm.token_count.completion", out)
    span.set_attribute("neatlogs.llm.token_count.total", prompt + out)
    span.set_attribute("neatlogs.llm.token_count.cache_read", cache_read)
    span.set_attribute("neatlogs.llm.token_count.cache_write", cache_write)
    cost = cost_usd(model, max(0, prompt - cache_read - cache_write), out, cache_read, cache_write)
    if cost is not None:
        span.set_attribute("neatlogs.llm.cost_usd", cost)
        span.set_attribute("llm.cost.total", cost)


class ClaudeCodeSpan:
    """An LLM span for one `claude -p` session (a separate process neatlogs cannot see into), carrying the exact
    model, tokens and cost that Claude Code reports, so analysis, exploration and fixes show up in spend."""

    def __init__(self, name: str, model: str | None, prompt: str):
        self.span = None
        if not _state["enabled"]:
            return
        try:
            from neatlogs._wrap_utils import get_provider_tracer
            self.span = get_provider_tracer().start_span(name=f"claude-code.{name}", attributes={
                "neatlogs.span.kind": "llm", "neatlogs.llm.provider": "anthropic",
                "neatlogs.llm.system": "claude-code", "neatlogs.llm.model_name": model or "claude-code",
                "neatlogs.llm.input_messages.0.role": "user",
                "neatlogs.llm.input_messages.0.content": prompt[:4000]})
        except Exception:
            self.span = None

    def end(self, result=None, error: str | None = None) -> None:
        if self.span is None:
            return
        from opentelemetry.trace import StatusCode
        s = self.span
        if result is not None:
            u = result.usage or {}
            prompt = int(u.get("input_tokens") or 0)
            out = int(u.get("output_tokens") or 0)
            s.set_attribute("neatlogs.llm.token_count.prompt", prompt)
            s.set_attribute("neatlogs.llm.token_count.completion", out)
            s.set_attribute("neatlogs.llm.token_count.total", prompt + out)
            s.set_attribute("neatlogs.llm.token_count.cache_read", int(u.get("cache_read_input_tokens") or 0))
            s.set_attribute("neatlogs.llm.token_count.cache_write", int(u.get("cache_creation_input_tokens") or 0))
            s.set_attribute("neatlogs.llm.cost_usd", float(result.cost_usd or 0.0))
            s.set_attribute("neatlogs.llm.metrics.duration_ms", float(result.seconds) * 1000)
            s.set_attribute("neatlogs.llm.output_messages.0.role", "assistant")
            s.set_attribute("neatlogs.llm.output_messages.0.content", (result.text or "")[:4000])
            s.set_attribute("claude_code.turns", int(result.turns or 0))
            s.set_status(StatusCode.OK if result.ok else StatusCode.ERROR)
        else:
            s.set_status(StatusCode.ERROR, (error or "")[:300])
        s.end()


def identify(end_user_id: str, session_id: str | None = None, **metadata):
    """Tag everything inside with the app being attacked (neatlogs 'end user') and the run (session)."""
    import contextlib
    if not _state["enabled"]:
        return contextlib.nullcontext()
    import neatlogs
    return neatlogs.identify(session_id=session_id, end_user_id=end_user_id, end_user_metadata=metadata or None)


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
