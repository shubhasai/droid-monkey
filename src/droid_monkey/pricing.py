"""What an Anthropic API call cost, from its token usage (USD per million tokens, Anthropic's published prices).

neatlogs records the model and token counts of every call but not a price, so its spend stays $0.00 unless the
cost is set on the span. Claude Code (`claude -p`) reports its own exact cost, so this is only for the API calls
droid-monkey makes itself (plan, decide, diagnose, design). Unknown models return None rather than a guess.
"""

# model prefix -> [(max prompt tokens for this rate card, input $/MTok, output $/MTok)], cache read and write
# rates as multiples of the input rate.
PRICES: dict[str, list[tuple[float, float, float]]] = {
    "claude-haiku-5-5": [(100_000, 0.10, 0.50), (float("inf"), 0.50, 2.50)],
    "claude-sonnet-5-5": [(float("inf"), 2.0, 10.0)],
    "claude-opus-5-5": [(float("inf"), 4.0, 20.0)],
}
CACHE_READ = {"claude-haiku-5-5": 0.1, "claude-sonnet-5-5": 0.1, "claude-opus-5-5": 0.05}
CACHE_WRITE = 1.25  # 5-minute cache writes


def register(model: str, price_in: float, price_out: float, cache_read: float = 0.1) -> None:
    """Prices for a model set in monkey.yaml (llm.price_in / price_out, USD per million tokens), e.g. one reached
    through a proxy under its own name. An exact name wins over the built-in prefixes."""
    PRICES[model] = [(float("inf"), price_in, price_out)]
    CACHE_READ[model] = cache_read


def _family(model: str) -> str | None:
    if model in PRICES:
        return model
    return next((p for p in PRICES if model and model.startswith(p)), None)


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float | None:
    family = _family(model)
    if family is None:
        return None
    prompt = input_tokens + cache_read + cache_write
    _, rate_in, rate_out = next(card for card in PRICES[family] if prompt <= card[0])
    return round((input_tokens * rate_in + cache_read * rate_in * CACHE_READ[family]
                  + cache_write * rate_in * CACHE_WRITE + output_tokens * rate_out) / 1e6, 8)
