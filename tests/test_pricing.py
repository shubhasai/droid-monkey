"""neatlogs spend is only as right as these numbers: Anthropic's published per-MTok prices."""

from droid_monkey.pricing import cost_usd


def test_haiku_uses_the_rate_card_for_the_prompt_length():
    assert cost_usd("claude-haiku-5-5", 100_000, 0) == 0.01        # up to 100K prompt tokens: $0.10 / MTok
    assert cost_usd("claude-haiku-5-5", 200_000, 0) == 0.10        # longer prompt: $0.50 / MTok
    assert cost_usd("claude-haiku-5-5", 0, 1_000_000) == 0.50


def test_cache_tokens_and_dated_model_ids():
    assert cost_usd("claude-haiku-5-5-20260901", 0, 0, cache_read=100_000) == 0.001
    assert cost_usd("claude-sonnet-5-5", 0, 0, cache_write=1_000_000) == 2.5


def test_unknown_models_are_not_guessed():
    assert cost_usd("gpt-9", 1000, 1000) is None


def test_llm_spans_get_neatlogs_tokens_and_cost():
    from droid_monkey.tracing import _add_neatlogs_usage

    class Span:  # what OpenInference's LangChain instrumentation leaves on an LLM span
        attributes = {"llm.model_name": "claude-haiku-5-5", "llm.token_count.prompt": 13000,
                      "llm.token_count.completion": 900}

        def set_attribute(self, k, v):
            self.attributes = {**self.attributes, k: v}

    span = Span()
    _add_neatlogs_usage(span)
    a = span.attributes
    assert a["neatlogs.llm.model_name"] == "claude-haiku-5-5"
    assert (a["neatlogs.llm.token_count.prompt"], a["neatlogs.llm.token_count.completion"]) == (13000, 900)
    assert a["neatlogs.llm.cost_usd"] == 0.00175
