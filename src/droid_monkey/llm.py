"""Bring-your-own model for droid-monkey's own reasoning (Claude Code is used separately for reading and fixing code).

monkey.yaml, all optional:
    llm:
      provider: anthropic            # or openai_compatible
      model: claude-haiku-5-5
      base_url: https://api.anthropic.com
      api_key_env: DROID_MONKEY_API_KEY
      price_in: 1.0                  # USD per million input tokens, for a model droid-monkey has no price for
      price_out: 5.0                 # (e.g. through a LiteLLM proxy); without them its calls show no cost
"""

import os
from dataclasses import dataclass

from . import tracing


@dataclass
class LLMConfig:
    provider: str = "anthropic"
    model: str = "claude-haiku-5-5"
    base_url: str | None = None
    api_key_env: str = "DROID_MONKEY_API_KEY"
    max_tokens: int = 8000
    price_in: float | None = None
    price_out: float | None = None

    @staticmethod
    def from_dict(raw: dict | None) -> "LLMConfig":
        raw = raw or {}
        cfg = LLMConfig(**{k: v for k, v in raw.items() if k in LLMConfig.__dataclass_fields__})
        if cfg.price_in is not None and cfg.price_out is not None:
            from . import pricing
            pricing.register(cfg.model, float(cfg.price_in), float(cfg.price_out))
        return cfg


class LLMConfigError(RuntimeError):
    pass


def chat_model(cfg: LLMConfig):
    tracing.init()
    key = os.environ.get(cfg.api_key_env)
    if not key:
        raise LLMConfigError(f"{cfg.api_key_env} is not set: add it to ~/.droid-monkey/.env (or the environment)")
    from langchain.chat_models import init_chat_model  # imported after tracing.init()

    if cfg.provider == "anthropic":
        return init_chat_model(cfg.model, model_provider="anthropic", api_key=key,
                               base_url=cfg.base_url or "https://api.anthropic.com", max_tokens=cfg.max_tokens)
    if cfg.provider == "openai_compatible":
        if not cfg.base_url:
            raise LLMConfigError("llm.base_url is required for provider openai_compatible")
        return init_chat_model(cfg.model, model_provider="openai", api_key=key, base_url=cfg.base_url,
                               max_tokens=cfg.max_tokens)
    raise LLMConfigError(f"unknown llm.provider {cfg.provider!r}; use anthropic or openai_compatible")
